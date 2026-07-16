from __future__ import annotations

import hashlib
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
RETRIEVAL_ALGORITHM_V1: Final = "lexical-bm25-char3/v1"
RETRIEVAL_ALGORITHM_V2: Final = "lexical-bm25-char3-anchored/v2"
PRODUCT_PREVIEW_ALGORITHM: Final = RETRIEVAL_ALGORITHM_V2
RETRIEVAL_ALGORITHM: Final = PRODUCT_PREVIEW_ALGORITHM
SUPPORTED_RETRIEVAL_ALGORITHMS: Final = frozenset({RETRIEVAL_ALGORITHM_V1, RETRIEVAL_ALGORITHM_V2})
PRODUCT_RETRIEVAL_ALGORITHMS: Final = frozenset({PRODUCT_PREVIEW_ALGORITHM})
RETRIEVAL_IMPLEMENTATION_FILES: Final = (
    "lazy_skill_router_capability_index.py",
    "lazy_skill_router_retrieval.py",
)
RETRIEVAL_QUERY_STOPWORDS: Final = frozenset(
    """
    a an and are as at be by for from how i in is it of on or
    that the this to what when which who with you your
    """.split()
)
DEFAULT_MAX_CANDIDATES: Final = 3
MAX_CANDIDATES: Final = 3
BM25_K1: Final = 1.2
BM25_B: Final = 0.75
CONFIGURED_NAME_BOOST: Final = 16.0


def retrieval_implementation_revision(root: Path | None = None) -> str | None:
    source_root = root or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        for relative_name in RETRIEVAL_IMPLEMENTATION_FILES:
            digest.update(relative_name.encode())
            digest.update(b"\0")
            digest.update((source_root / relative_name).read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


RETRIEVAL_IMPLEMENTATION_REVISION: Final = retrieval_implementation_revision()


def retrieval_settings(
    config: dict[str, Any],
    *,
    frozen_replay: bool = False,
) -> tuple[str, int, str, tuple[str, ...]]:
    value = config.get("capabilityRetrieval")
    if value is None:
        return "off", DEFAULT_MAX_CANDIDATES, RETRIEVAL_ALGORITHM, ()
    if not isinstance(value, dict):
        return "off", DEFAULT_MAX_CANDIDATES, RETRIEVAL_ALGORITHM, ("retrieval_config_invalid",)
    mode = value.get("mode", "off")
    max_candidates = value.get("maxCandidates", DEFAULT_MAX_CANDIDATES)
    algorithm = value.get("algorithm", RETRIEVAL_ALGORITHM)
    reasons: list[str] = []
    if set(value) - {"mode", "maxCandidates", "algorithm"}:
        reasons.append("retrieval_config_fields_unknown")
    if mode not in {"off", "shadow"}:
        mode = "off"
        reasons.append("retrieval_mode_invalid")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 3:
        max_candidates = DEFAULT_MAX_CANDIDATES
        reasons.append("retrieval_max_candidates_invalid")
    allowed_algorithms = SUPPORTED_RETRIEVAL_ALGORITHMS if frozen_replay else PRODUCT_RETRIEVAL_ALGORITHMS
    if algorithm == RETRIEVAL_ALGORITHM_V1 and not frozen_replay:
        algorithm = RETRIEVAL_ALGORITHM
        reasons.append("retrieval_algorithm_v1_replay_only")
    elif not isinstance(algorithm, str) or algorithm not in allowed_algorithms:
        algorithm = RETRIEVAL_ALGORITHM
        reasons.append("retrieval_algorithm_unsupported")
    return str(mode), max_candidates, str(algorithm), tuple(reasons)


def retrieval_enabled(config: dict[str, Any]) -> bool:
    mode, _, _, reasons = retrieval_settings(config)
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
    algorithm: str = RETRIEVAL_ALGORITHM,
) -> dict[str, Any]:
    bounded_candidates = candidates or []
    retrieval_top1 = bounded_candidates[0]["skillRef"]["configuredName"] if bounded_candidates else None
    return {
        "schema": RETRIEVAL_RESULT_SCHEMA,
        "mode": mode,
        "status": status,
        "algorithm": algorithm,
        "implementationRevision": RETRIEVAL_IMPLEMENTATION_REVISION,
        "indexRevision": index.revision if index is not None else None,
        "inventoryRevision": index.inventory_revision if index is not None else None,
        "indexSchema": index.schema if index is not None else None,
        "featureExtractor": index.feature_extractor if index is not None else None,
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
    *,
    algorithm: str = RETRIEVAL_ALGORITHM,
) -> list[dict[str, Any]]:
    if algorithm not in SUPPORTED_RETRIEVAL_ALGORITHMS:
        raise ValueError("unsupported retrieval algorithm")
    if algorithm == RETRIEVAL_ALGORITHM_V2:
        query_tokens = []
        for raw_token in WORD_RE.findall(normalized_text(prompt)):
            stopword_token = raw_token.rstrip("._:+-")
            if stopword_token and stopword_token not in RETRIEVAL_QUERY_STOPWORDS:
                query_tokens.append(raw_token)
        query_features = lexical_features((" ".join(query_tokens),), max_features=256)
    else:
        query_features = lexical_features((prompt,), max_features=256)
    raw_prompt_tokens = tuple(WORD_RE.findall(normalized_text(prompt)))
    if algorithm == RETRIEVAL_ALGORITHM_V2:
        prompt_tokens = frozenset(
            token for raw_token in raw_prompt_tokens for token in (raw_token, raw_token.rstrip("._:+-")) if token
        )
    else:
        prompt_tokens = frozenset(raw_prompt_tokens)
    prompt_has_hangul = any("가" <= character <= "힣" for character in normalized_text(prompt))
    ranked: list[tuple[float, dict[str, Any], list[str]]] = []
    for entry in index.entries:
        score, evidence_ids = entry_score(prompt_tokens, query_features, entry, index)
        has_word_anchor = "configured_name.lexical" in evidence_ids or "metadata.word" in evidence_ids
        if score > 0.0 and (algorithm == RETRIEVAL_ALGORITHM_V1 or prompt_has_hangul or has_word_anchor):
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
    algorithm: str | None = None,
    frozen_replay: bool = False,
) -> dict[str, Any]:
    mode, max_candidates, configured_algorithm, config_reasons = retrieval_settings(
        config,
        frozen_replay=frozen_replay,
    )
    effective_algorithm = configured_algorithm if algorithm is None else algorithm
    allowed_algorithms = SUPPORTED_RETRIEVAL_ALGORITHMS if frozen_replay else PRODUCT_RETRIEVAL_ALGORITHMS
    if effective_algorithm == RETRIEVAL_ALGORITHM_V1 and not frozen_replay:
        config_reasons = tuple(dict.fromkeys((*config_reasons, "retrieval_algorithm_v1_replay_only")))
        effective_algorithm = RETRIEVAL_ALGORITHM
    elif not isinstance(effective_algorithm, str) or effective_algorithm not in allowed_algorithms:
        if "retrieval_algorithm_unsupported" not in config_reasons:
            config_reasons = (*config_reasons, "retrieval_algorithm_unsupported")
        effective_algorithm = RETRIEVAL_ALGORITHM
    effective_mode = "shadow" if force else mode
    if config_reasons:
        return result_payload(
            mode="off",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=config_reasons,
            algorithm=effective_algorithm,
        )
    if effective_mode != "shadow":
        return result_payload(
            mode="off",
            status="skipped",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("retrieval_disabled",),
            algorithm=effective_algorithm,
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("prompt_too_long",),
            algorithm=effective_algorithm,
        )
    if inventory is None or inventory.state != "available" or not isinstance(inventory.revision, str):
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("inventory_unavailable",),
            algorithm=effective_algorithm,
        )
    index_path = capability_index_path(config, explicit_index)
    if index_path is None:
        return result_payload(
            mode="shadow",
            status="degraded",
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_index_path_unavailable",),
            algorithm=effective_algorithm,
        )
    index = load_capability_index(Path(index_path), frozen_replay=frozen_replay)
    if index.state != "available":
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=index.reason_codes or ("capability_index_unavailable",),
            algorithm=effective_algorithm,
        )
    if index.inventory_revision != inventory.revision:
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_index_stale",),
            algorithm=effective_algorithm,
        )
    try:
        candidates = rank_candidates(prompt, index, max_candidates, algorithm=effective_algorithm)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        debug(f"capability retrieval failed open: {exc}")
        return result_payload(
            mode="shadow",
            status="degraded",
            index=index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
            reason_codes=("capability_retrieval_failed",),
            algorithm=effective_algorithm,
        )
    return result_payload(
        mode="shadow",
        status="matched" if candidates else "no-match",
        index=index,
        candidates=candidates,
        legacy_route=legacy_route,
        legacy_primary=legacy_primary,
        algorithm=effective_algorithm,
    )
