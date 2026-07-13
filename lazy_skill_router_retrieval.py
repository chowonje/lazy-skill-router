from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Final

from lazy_skill_router_capability_index import (
    MAX_PROMPT_CHARS,
    WORD_RE,
    CapabilityIndexSnapshot,
    capability_index_path,
    lexical_features,
    load_capability_index,
    normalized_text,
)
from lazy_skill_router_common import debug
from lazy_skill_router_inventory import InventorySnapshot

RETRIEVAL_RESULT_SCHEMA: Final = "lazy-skill-router.retrieval-result/v1"
RETRIEVAL_ALGORITHM: Final = "lexical-bm25-char3/v1"
DEFAULT_MAX_CANDIDATES: Final = 3
MAX_CANDIDATES: Final = 3
BM25_K1: Final = 1.2
BM25_B: Final = 0.75
CONFIGURED_NAME_BOOST: Final = 16.0


def retrieval_settings(config: dict[str, Any]) -> tuple[str, int, tuple[str, ...]]:
    value = config.get("capabilityRetrieval")
    if value is None:
        return "off", DEFAULT_MAX_CANDIDATES, ()
    if not isinstance(value, dict):
        return "off", DEFAULT_MAX_CANDIDATES, ("retrieval_config_invalid",)
    mode = value.get("mode", "off")
    max_candidates = value.get("maxCandidates", DEFAULT_MAX_CANDIDATES)
    reasons: list[str] = []
    if set(value) - {"mode", "maxCandidates"}:
        reasons.append("retrieval_config_fields_unknown")
    if mode not in {"off", "shadow"}:
        mode = "off"
        reasons.append("retrieval_mode_invalid")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 3:
        max_candidates = DEFAULT_MAX_CANDIDATES
        reasons.append("retrieval_max_candidates_invalid")
    return str(mode), max_candidates, tuple(reasons)


def retrieval_enabled(config: dict[str, Any]) -> bool:
    mode, _, reasons = retrieval_settings(config)
    return mode == "shadow" and not reasons


def result_payload(
    *,
    mode: str,
    status: str,
    index: CapabilityIndexSnapshot | None = None,
    candidates: list[dict[str, Any]] | None = None,
    legacy_route: str | None = None,
    legacy_primary: str | None = None,
    reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    bounded_candidates = candidates or []
    retrieval_top1 = bounded_candidates[0]["skillRef"]["configuredName"] if bounded_candidates else None
    return {
        "schema": RETRIEVAL_RESULT_SCHEMA,
        "mode": mode,
        "status": status,
        "algorithm": RETRIEVAL_ALGORITHM,
        "indexRevision": index.revision if index is not None else None,
        "inventoryRevision": index.inventory_revision if index is not None else None,
        "scoreSemantics": "not_probability",
        "candidates": bounded_candidates,
        "legacy": {"routeId": legacy_route, "primaryConfiguredName": legacy_primary},
        "comparison": {
            "top1SameSkill": bool(legacy_primary is not None and retrieval_top1 == legacy_primary),
        },
        "reasonCodes": list(reason_codes),
        "semantics": {
            "shadowOnly": True,
            "authority": "none",
            "noMatchScope": "lexical-retrieval-only",
            "ownsSemanticAbstention": False,
            "requiresHostOwnershipDecision": bool(bounded_candidates),
            "affectsLegacySelection": False,
            "affectsActivation": False,
            "runtimeLlmCalls": False,
            "executionRequested": False,
        },
    }


def feature_weight(feature: str) -> float:
    return 2.5 if feature.startswith("w:") else 0.35


def name_match(prompt_tokens: frozenset[str], configured_name: str) -> bool:
    normalized_name = normalized_text(configured_name)
    if not normalized_name:
        return False
    if normalized_name in prompt_tokens:
        return True
    leaf_name = normalized_name.split(":", 1)[-1]
    return len(leaf_name) >= 4 and leaf_name in prompt_tokens


def exact_word_features(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(f"w:{token}" for value in values for token in WORD_RE.findall(normalized_text(value)))


def word_evidence_ids(query_features: dict[str, int], entry: dict[str, Any]) -> list[str]:
    query_words = frozenset(feature for feature in query_features if feature.startswith("w:"))
    matched_words = query_words & frozenset(entry["feature_counts"])
    if not matched_words:
        return []

    sources = (
        ("metadata.configured-name.word", (str(entry["configured_name"]),)),
        ("metadata.alias.word", tuple(entry["aliases"])),
        ("metadata.capability.word", tuple(entry["capabilities"])),
        ("metadata.phase.word", tuple(entry["phases"])),
    )
    evidence_ids: list[str] = []
    attributed_words: set[str] = set()
    for evidence_id, values in sources:
        source_words = exact_word_features(values)
        attributed_words.update(source_words)
        if matched_words & source_words:
            evidence_ids.append(evidence_id)
    if matched_words - attributed_words:
        evidence_ids.append("metadata.description.word")
    return evidence_ids


def entry_score(
    prompt_tokens: frozenset[str],
    query_features: dict[str, int],
    entry: dict[str, Any],
    index: CapabilityIndexSnapshot,
) -> tuple[float, list[str]]:
    document_features = entry["feature_counts"]
    document_length = float(entry["document_length"])
    average_length = max(index.average_document_length, 1.0)
    document_count = max(len(index.entries), 1)
    score = 0.0
    has_word_evidence = False
    has_trigram_evidence = False
    for feature, query_count in query_features.items():
        term_frequency = document_features.get(feature, 0)
        if term_frequency <= 0:
            continue
        document_frequency = index.document_frequency.get(feature, 0)
        inverse_frequency = math.log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
        length_factor = term_frequency + BM25_K1 * (1.0 - BM25_B + BM25_B * document_length / average_length)
        score += (
            feature_weight(feature)
            * min(query_count, 3)
            * inverse_frequency
            * (term_frequency * (BM25_K1 + 1.0) / length_factor)
        )
        has_word_evidence = has_word_evidence or feature.startswith("w:")
        has_trigram_evidence = has_trigram_evidence or feature.startswith("g:")

    evidence_ids: list[str] = []
    if name_match(prompt_tokens, str(entry["configured_name"])):
        score += CONFIGURED_NAME_BOOST
        evidence_ids.append("configured_name.lexical")
    if has_word_evidence:
        evidence_ids.append("metadata.word")
        evidence_ids.extend(word_evidence_ids(query_features, entry))
    if has_trigram_evidence:
        evidence_ids.append("metadata.char3")
    return score, evidence_ids


def rank_candidates(
    prompt: str,
    index: CapabilityIndexSnapshot,
    max_candidates: int,
) -> list[dict[str, Any]]:
    query_features = lexical_features((prompt,), max_features=256)
    prompt_tokens = frozenset(WORD_RE.findall(normalized_text(prompt)))
    ranked: list[tuple[float, dict[str, Any], list[str]]] = []
    for entry in index.entries:
        score, evidence_ids = entry_score(prompt_tokens, query_features, entry, index)
        if score > 0.0:
            ranked.append((score, entry, evidence_ids))
    ranked.sort(key=lambda item: (-item[0], item[1]["configured_name"], item[1]["canonical_id"]))
    bounded = ranked[:max_candidates]
    candidates: list[dict[str, Any]] = []
    for offset, (score, entry, evidence_ids) in enumerate(bounded):
        next_score = bounded[offset + 1][0] if offset + 1 < len(bounded) else None
        candidates.append(
            {
                "rank": offset + 1,
                "skillRef": {
                    "canonicalId": entry["canonical_id"],
                    "configuredName": entry["configured_name"],
                },
                "score": round(score, 4),
                "scoreMargin": round(max(0.0, score - next_score), 4) if next_score is not None else None,
                "evidenceIds": evidence_ids,
                "availabilityStatus": entry["availability_status"],
            }
        )
    return candidates


def retrieve_capabilities(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot | None,
    *,
    explicit_index: str | None = None,
    force: bool = False,
    legacy_route: str | None = None,
    legacy_primary: str | None = None,
) -> dict[str, Any]:
    mode, max_candidates, config_reasons = retrieval_settings(config)
    effective_mode = "shadow" if force else mode
    if config_reasons:
        return result_payload(
            mode="off",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=config_reasons,
        )
    if effective_mode != "shadow":
        return result_payload(
            mode="off",
            status="skipped",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("retrieval_disabled",),
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("prompt_too_long",),
        )
    if inventory is None or inventory.state != "available" or not isinstance(inventory.revision, str):
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("inventory_unavailable",),
        )
    index_path = capability_index_path(config, explicit_index)
    if index_path is None:
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_index_path_unavailable",),
        )
    index = load_capability_index(Path(index_path))
    if index.state != "available":
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=index.reason_codes or ("capability_index_unavailable",),
        )
    if index.inventory_revision != inventory.revision:
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_index_stale",),
        )
    try:
        candidates = rank_candidates(prompt, index, max_candidates)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        debug(f"capability retrieval failed open: {exc}")
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_retrieval_failed",),
        )
    return result_payload(
        mode="shadow",
        status="matched" if candidates else "no-match",
        index=index,
        candidates=candidates,
        legacy_route=legacy_route,
        legacy_primary=legacy_primary,
    )
