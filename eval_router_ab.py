from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from lazy_skill_router_capability_index import (
    CAPABILITY_INDEX_SCHEMA_V1,
    CAPABILITY_INDEX_SCHEMA_V2,
    CapabilityIndexSnapshot,
    load_capability_index,
)
from lazy_skill_router_core import activation_for_prompt
from lazy_skill_router_inventory import InventorySnapshot, load_inventory_manifest
from lazy_skill_router_retrieval import (
    RETRIEVAL_ALGORITHM,
    SUPPORTED_RETRIEVAL_ALGORITHMS,
    retrieve_capabilities,
)
from release_checksums import safe_manifest_path

EXPERIMENT_MANIFEST_SCHEMA: Final = "lazy-skill-router.router-ab-manifest/v1"
EXPERIMENT_REPORT_SCHEMA: Final = "lazy-skill-router.router-ab-report/v1"
EXPERIMENT_EVIDENCE_SCHEMA: Final = "lazy-skill-router.experiment-evidence/v1"
PROMOTION_GATE_SCHEMA: Final = "lazy-skill-router.promotion-gate/v1"
CONTENT_REVISION_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
TOP_K: Final = 3
MANIFEST_REQUIRED_FIELDS: Final = frozenset({"schema", "frozen", "cases"})
MANIFEST_FIELDS: Final = MANIFEST_REQUIRED_FIELDS | {"evidence"}
FROZEN_REQUIRED_FIELDS: Final = frozenset(
    {
        "configRevision",
        "inventoryRevision",
        "indexRevision",
        "retrievalAlgorithm",
        "experimentCodeRevision",
        "maxCandidates",
    }
)
FROZEN_FIELDS: Final = FROZEN_REQUIRED_FIELDS | {"indexSchema"}
CASE_FIELDS: Final = frozenset({"id", "category", "language", "risk", "prompt", "gold"})
GOLD_FIELDS: Final = frozenset({"primary", "acceptableCandidates", "expectedAbstain", "forbiddenCandidates"})
EVIDENCE_REQUIRED_FIELDS: Final = frozenset(
    {
        "schema",
        "corpusProvenance",
        "metadataProvenance",
        "independentHoldoutRevision",
        "independentAdjudicationRevision",
        "ownershipObservationRevision",
        "activationObservationRevision",
        "outcomeObservationRevision",
    }
)
EVIDENCE_FIELDS: Final = EVIDENCE_REQUIRED_FIELDS | {"artifactPaths"}
EVIDENCE_ARTIFACT_TYPES: Final = (
    ("independentHoldout", "independent_holdout_revision"),
    ("independentAdjudication", "independent_adjudication_revision"),
    ("ownershipObservation", "ownership_observation_revision"),
    ("activationObservation", "activation_observation_revision"),
    ("outcomeObservation", "outcome_observation_revision"),
)
EVIDENCE_ARTIFACT_PATH_FIELDS: Final = frozenset(name for name, _ in EVIDENCE_ARTIFACT_TYPES)
MAX_EVIDENCE_ARTIFACT_BYTES: Final = 16 * 1024 * 1024
CORPUS_PROVENANCE: Final = frozenset({"unspecified", "synthetic-calibration", "independent-holdout"})
METADATA_PROVENANCE: Final = frozenset(
    {"unspecified", "active-catalog", "corpus-informed-calibration", "independent-catalog"}
)
RISK_LEVELS: Final = frozenset({"low", "medium", "high"})
PROMOTION_POLICY: Final = {
    "minCandidateRecallAt3": 0.95,
    "minCandidateTop1": 0.90,
    "minCandidateMrr": 0.75,
    "minCandidateMeanPrecisionAt3": 0.30,
    "minExpectedAbstainLexicalNoMatchRecall": 0.95,
    "maxForbiddenTop1Hits": 0,
    "maxHighRiskForbiddenTop3Hits": 0,
    "candidateRecallAt3CiLowerBoundExclusive": 0.0,
    "maxCandidateP95LatencyMs": 20.0,
    "maxInventoryIneligibleHits": 0,
    "maxOperationalFailures": 0,
    "requiresArtifactVerification": True,
    "requiresIndependenceVerification": True,
}
VOLATILE_CONFIG_FIELDS: Final = frozenset({"_loaded_from", "_config_trust"})
EXPERIMENT_CODE_FILES: Final = (
    "eval_router_ab.py",
    "lazy_skill_router_activation.py",
    "lazy_skill_router_capability_index.py",
    "lazy_skill_router_common.py",
    "lazy_skill_router_core.py",
    "lazy_skill_router_inventory.py",
    "lazy_skill_router_logging.py",
    "lazy_skill_router_policy_ir.py",
    "lazy_skill_router_retrieval.py",
    "lazy_skill_router_scoring.py",
    "materialize_router_ab_manifest.py",
    "release_checksums.py",
)


@dataclass(frozen=True)
class FrozenInputs:
    config_revision: str
    inventory_revision: str
    index_revision: str
    retrieval_algorithm: str
    experiment_code_revision: str
    max_candidates: int
    index_schema: str = CAPABILITY_INDEX_SCHEMA_V1


@dataclass(frozen=True)
class GoldLabel:
    primary: str | None
    acceptable_candidates: tuple[str, ...]
    expected_abstain: bool
    forbidden_candidates: tuple[str, ...]


@dataclass(frozen=True)
class ABCase:
    case_id: str
    category: str
    language: str
    risk: str
    prompt: str
    gold: GoldLabel


@dataclass(frozen=True)
class ExperimentEvidence:
    corpus_provenance: str = "unspecified"
    metadata_provenance: str = "unspecified"
    independent_holdout_revision: str | None = None
    independent_adjudication_revision: str | None = None
    ownership_observation_revision: str | None = None
    activation_observation_revision: str | None = None
    outcome_observation_revision: str | None = None
    artifact_paths: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class ExperimentManifest:
    frozen: FrozenInputs
    cases: tuple[ABCase, ...]
    revision: str
    evidence: ExperimentEvidence = ExperimentEvidence()


@dataclass(frozen=True)
class VerifiedInputs:
    config: dict[str, Any]
    inventory: InventorySnapshot
    index: CapabilityIndexSnapshot
    index_path: Path
    frozen: FrozenInputs


@dataclass(frozen=True)
class SystemOutcome:
    candidates: tuple[str, ...]
    abstained: bool
    status: str
    latency_ms: float = 0.0
    operational_failure: bool = False

    @property
    def top1(self) -> str | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True)
class CaseEvaluation:
    case: ABCase
    legacy: SystemOutcome
    retrieval: SystemOutcome


class EvidenceArtifactTooLargeError(Exception):
    pass


def canonical_revision(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stable_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key not in VOLATILE_CONFIG_FIELDS}


def config_revision(config: dict[str, Any]) -> str:
    return canonical_revision(stable_config(config))


def experiment_code_revision(root: Path | None = None) -> str:
    source_root = root or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for relative_path in EXPERIMENT_CODE_FILES:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((source_root / relative_path).read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    return value


def require_exact_fields(value: dict[str, Any], expected: frozenset[str], location: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ValueError(f"{location} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")


def require_fields(
    value: dict[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ValueError(f"{location} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty string")
    return value


def require_optional_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return require_string(value, location)


def require_string_list(value: Any, location: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty " if allow_empty else "non-empty "
        raise ValueError(f"{location} must be a {qualifier}string array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{location} must be a string array")
    if len(set(value)) != len(value):
        raise ValueError(f"{location} must not contain duplicates")
    return tuple(value)


def parse_frozen(value: Any) -> FrozenInputs:
    raw = require_object(value, "frozen")
    require_fields(raw, required=FROZEN_REQUIRED_FIELDS, allowed=FROZEN_FIELDS, location="frozen")
    max_candidates = raw["maxCandidates"]
    if isinstance(max_candidates, bool) or max_candidates != TOP_K:
        raise ValueError(f"frozen.maxCandidates must be {TOP_K}")
    index_schema = raw.get("indexSchema", CAPABILITY_INDEX_SCHEMA_V1)
    if index_schema not in {CAPABILITY_INDEX_SCHEMA_V1, CAPABILITY_INDEX_SCHEMA_V2}:
        raise ValueError("frozen.indexSchema is unsupported")
    return FrozenInputs(
        require_string(raw["configRevision"], "frozen.configRevision"),
        require_string(raw["inventoryRevision"], "frozen.inventoryRevision"),
        require_string(raw["indexRevision"], "frozen.indexRevision"),
        require_string(raw["retrievalAlgorithm"], "frozen.retrievalAlgorithm"),
        require_string(raw["experimentCodeRevision"], "frozen.experimentCodeRevision"),
        max_candidates,
        index_schema,
    )


def parse_gold(value: Any, location: str) -> GoldLabel:
    raw = require_object(value, location)
    require_exact_fields(raw, GOLD_FIELDS, location)
    expected_abstain = raw["expectedAbstain"]
    if not isinstance(expected_abstain, bool):
        raise ValueError(f"{location}.expectedAbstain must be a boolean")
    primary = require_optional_string(raw["primary"], f"{location}.primary")
    acceptable = require_string_list(
        raw["acceptableCandidates"],
        f"{location}.acceptableCandidates",
        allow_empty=expected_abstain,
    )
    forbidden = require_string_list(
        raw["forbiddenCandidates"],
        f"{location}.forbiddenCandidates",
        allow_empty=True,
    )
    if expected_abstain:
        if primary is not None or acceptable:
            raise ValueError(f"{location} abstain labels must have null primary and no acceptable candidates")
    elif primary is None or primary not in acceptable:
        raise ValueError(f"{location}.acceptableCandidates must include the non-null primary")
    overlap = sorted(set(acceptable) & set(forbidden))
    if overlap:
        raise ValueError(f"{location} candidates cannot be both acceptable and forbidden: {', '.join(overlap)}")
    return GoldLabel(primary, acceptable, expected_abstain, forbidden)


def parse_case(value: Any, offset: int) -> ABCase:
    location = f"cases[{offset}]"
    raw = require_object(value, location)
    require_exact_fields(raw, CASE_FIELDS, location)
    risk = require_string(raw["risk"], f"{location}.risk")
    if risk not in RISK_LEVELS:
        raise ValueError(f"{location}.risk is unsupported")
    return ABCase(
        require_string(raw["id"], f"{location}.id"),
        require_string(raw["category"], f"{location}.category"),
        require_string(raw["language"], f"{location}.language"),
        risk,
        require_string(raw["prompt"], f"{location}.prompt"),
        parse_gold(raw["gold"], f"{location}.gold"),
    )


def parse_artifact_paths(value: Any) -> tuple[tuple[str, str | None], ...]:
    if value is None:
        return ()
    raw = require_object(value, "evidence.artifactPaths")
    require_exact_fields(raw, EVIDENCE_ARTIFACT_PATH_FIELDS, "evidence.artifactPaths")
    return tuple(
        (evidence_type, require_optional_string(raw[evidence_type], f"evidence.artifactPaths.{evidence_type}"))
        for evidence_type, _ in EVIDENCE_ARTIFACT_TYPES
    )


def parse_evidence(value: Any) -> ExperimentEvidence:
    if value is None:
        return ExperimentEvidence()
    raw = require_object(value, "evidence")
    require_fields(
        raw,
        required=EVIDENCE_REQUIRED_FIELDS,
        allowed=EVIDENCE_FIELDS,
        location="evidence",
    )
    if raw["schema"] != EXPERIMENT_EVIDENCE_SCHEMA:
        raise ValueError(f"evidence.schema must be {EXPERIMENT_EVIDENCE_SCHEMA!r}")
    corpus_provenance = require_string(raw["corpusProvenance"], "evidence.corpusProvenance")
    metadata_provenance = require_string(raw["metadataProvenance"], "evidence.metadataProvenance")
    if corpus_provenance not in CORPUS_PROVENANCE:
        raise ValueError("evidence.corpusProvenance is unsupported")
    if metadata_provenance not in METADATA_PROVENANCE:
        raise ValueError("evidence.metadataProvenance is unsupported")
    return ExperimentEvidence(
        corpus_provenance,
        metadata_provenance,
        require_optional_string(raw["independentHoldoutRevision"], "evidence.independentHoldoutRevision"),
        require_optional_string(raw["independentAdjudicationRevision"], "evidence.independentAdjudicationRevision"),
        require_optional_string(raw["ownershipObservationRevision"], "evidence.ownershipObservationRevision"),
        require_optional_string(raw["activationObservationRevision"], "evidence.activationObservationRevision"),
        require_optional_string(raw["outcomeObservationRevision"], "evidence.outcomeObservationRevision"),
        parse_artifact_paths(raw.get("artifactPaths")),
    )


def parse_manifest(value: Any) -> ExperimentManifest:
    raw = require_object(value, "manifest")
    require_fields(raw, required=MANIFEST_REQUIRED_FIELDS, allowed=MANIFEST_FIELDS, location="manifest")
    if raw["schema"] != EXPERIMENT_MANIFEST_SCHEMA:
        raise ValueError(f"manifest.schema must be {EXPERIMENT_MANIFEST_SCHEMA!r}")
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest.cases must be a non-empty array")
    cases = tuple(parse_case(item, offset) for offset, item in enumerate(raw_cases))
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("manifest case ids must be unique")
    return ExperimentManifest(
        parse_frozen(raw["frozen"]),
        cases,
        canonical_revision(raw),
        parse_evidence(raw.get("evidence")),
    )


def load_manifest(path: Path) -> ExperimentManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"experiment manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"experiment manifest is unreadable: {path}") from exc
    return parse_manifest(raw)


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"route config not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"route config is unreadable: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("route config root must be an object")
    return stable_config(raw)


def verify_inputs(
    config: dict[str, Any],
    inventory_path: Path,
    index_path: Path,
    frozen: FrozenInputs,
) -> VerifiedInputs:
    inventory = load_inventory_manifest(inventory_path)
    if inventory.state != "available" or not inventory.revision:
        reasons = ", ".join(inventory.reason_codes) or inventory.state
        raise ValueError(f"skill inventory is unavailable: {reasons}")
    index = load_capability_index(index_path, frozen_replay=True)
    if index.state != "available" or not index.revision:
        reasons = ", ".join(index.reason_codes) or index.state
        raise ValueError(f"capability index is unavailable: {reasons}")
    if index.inventory_revision != inventory.revision:
        raise ValueError("capability index is stale for the selected inventory")

    actual = FrozenInputs(
        config_revision(config),
        inventory.revision,
        index.revision,
        (
            frozen.retrieval_algorithm
            if frozen.retrieval_algorithm in SUPPORTED_RETRIEVAL_ALGORITHMS
            else RETRIEVAL_ALGORITHM
        ),
        experiment_code_revision(),
        TOP_K,
        index.schema or CAPABILITY_INDEX_SCHEMA_V1,
    )
    labels = (
        ("configRevision", frozen.config_revision, actual.config_revision),
        ("inventoryRevision", frozen.inventory_revision, actual.inventory_revision),
        ("indexRevision", frozen.index_revision, actual.index_revision),
        ("indexSchema", frozen.index_schema, actual.index_schema),
        ("retrievalAlgorithm", frozen.retrieval_algorithm, actual.retrieval_algorithm),
        ("experimentCodeRevision", frozen.experiment_code_revision, actual.experiment_code_revision),
        ("maxCandidates", frozen.max_candidates, actual.max_candidates),
    )
    mismatches = [name for name, expected, observed in labels if expected != observed]
    if mismatches:
        raise ValueError(f"frozen input mismatch: {', '.join(mismatches)}")
    return VerifiedInputs(config, inventory, index, index_path, actual)


def legacy_outcome(prompt: str, config: dict[str, Any], inventory: InventorySnapshot) -> SystemOutcome:
    activation = activation_for_prompt(prompt, config, inventory)
    primary = next(
        (
            skill.configured_name
            for skill in (*activation.activated_skills, *activation.deferred_skills)
            if skill.role == "primary"
        ),
        None,
    )
    candidates = (primary,) if primary is not None else ()
    return SystemOutcome(candidates, activation.disposition == "abstain", activation.disposition)


def retrieval_outcome(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot,
    index_path: Path,
    retrieval_algorithm: str,
) -> SystemOutcome:
    retrieval_config = dict(config)
    retrieval_config["capabilityRetrieval"] = {
        "mode": "shadow",
        "maxCandidates": TOP_K,
        "algorithm": retrieval_algorithm,
    }
    result = retrieve_capabilities(
        prompt,
        retrieval_config,
        inventory,
        explicit_index=str(index_path),
        force=True,
        algorithm=retrieval_algorithm,
        frozen_replay=True,
    )
    names: list[str] = []
    candidates = result.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates[:TOP_K]:
            skill_ref = candidate.get("skillRef") if isinstance(candidate, dict) else None
            name = skill_ref.get("configuredName") if isinstance(skill_ref, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
    status = str(result.get("status", "invalid"))
    return SystemOutcome(
        tuple(names),
        False,
        status,
        operational_failure=status not in {"matched", "no-match"},
    )


def timed_call(clock_ns: Callable[[], int], call: Callable[[], SystemOutcome]) -> SystemOutcome:
    started = clock_ns()
    outcome = call()
    elapsed_ms = max(0, clock_ns() - started) / 1_000_000
    return replace(outcome, latency_ms=elapsed_ms)


def evaluate_case(
    case: ABCase,
    inputs: VerifiedInputs,
    *,
    retrieval_first: bool,
    clock_ns: Callable[[], int],
) -> CaseEvaluation:
    def legacy_call() -> SystemOutcome:
        return legacy_outcome(case.prompt, inputs.config, inputs.inventory)

    def retrieval_call() -> SystemOutcome:
        return retrieval_outcome(
            case.prompt,
            inputs.config,
            inputs.inventory,
            inputs.index_path,
            inputs.frozen.retrieval_algorithm,
        )

    try:
        if retrieval_first:
            retrieval = timed_call(clock_ns, retrieval_call)
            legacy = timed_call(clock_ns, legacy_call)
        else:
            legacy = timed_call(clock_ns, legacy_call)
            retrieval = timed_call(clock_ns, retrieval_call)
    except Exception as exc:
        raise ValueError(f"case {case.case_id!r} evaluation failed ({type(exc).__name__})") from exc
    return CaseEvaluation(case, legacy, retrieval)


def evaluate_cases(
    cases: tuple[ABCase, ...],
    inputs: VerifiedInputs,
    *,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> tuple[CaseEvaluation, ...]:
    return tuple(
        evaluate_case(case, inputs, retrieval_first=bool(offset % 2), clock_ns=clock_ns)
        for offset, case in enumerate(cases)
    )


def top1_correct(case: ABCase, outcome: SystemOutcome) -> bool:
    if outcome.operational_failure:
        return False
    if case.gold.expected_abstain:
        return outcome.abstained
    return not outcome.abstained and outcome.top1 == case.gold.primary


def ranking_values(case: ABCase, outcome: SystemOutcome) -> tuple[float, float, float] | None:
    if case.gold.expected_abstain:
        return None
    candidates = outcome.candidates[:TOP_K]
    acceptable = set(case.gold.acceptable_candidates)
    relevant_hits = sum(candidate in acceptable for candidate in candidates)
    recall = relevant_hits / len(acceptable)
    precision = relevant_hits / len(candidates) if candidates else 0.0
    try:
        primary_rank = candidates.index(str(case.gold.primary)) + 1
    except ValueError:
        reciprocal_rank = 0.0
    else:
        reciprocal_rank = 1.0 / primary_rank
    return recall, reciprocal_rank, precision


def labelled_conflict_hits(case: ABCase, outcome: SystemOutcome) -> tuple[str, ...]:
    forbidden = set(case.gold.forbidden_candidates)
    return tuple(candidate for candidate in outcome.candidates[:TOP_K] if candidate in forbidden)


def inventory_ineligible_hits(
    outcome: SystemOutcome,
    inventory: InventorySnapshot,
) -> tuple[str, ...]:
    return tuple(candidate for candidate in outcome.candidates[:TOP_K] if inventory.resolve(candidate) is None)


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def mean(values: Iterable[float]) -> float | None:
    materialized = tuple(values)
    return round(sum(materialized) / len(materialized), 6) if materialized else None


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 4)
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(interpolated, 4)


def latency_summary(evaluations: tuple[CaseEvaluation, ...], attribute: str) -> dict[str, float | None]:
    values = [getattr(evaluation, attribute).latency_ms for evaluation in evaluations]
    return {
        "p50Ms": percentile(values, 0.50),
        "p95Ms": percentile(values, 0.95),
        "p99Ms": percentile(values, 0.99),
    }


def system_summary(
    evaluations: tuple[CaseEvaluation, ...],
    attribute: str,
    inventory: InventorySnapshot,
) -> dict[str, Any]:
    outcomes = [getattr(evaluation, attribute) for evaluation in evaluations]
    correct = sum(top1_correct(evaluation.case, outcome) for evaluation, outcome in zip(evaluations, outcomes))
    candidate_pairs = [
        (evaluation.case, outcome)
        for evaluation, outcome in zip(evaluations, outcomes)
        if not evaluation.case.gold.expected_abstain
    ]
    candidate_correct = sum(top1_correct(case, outcome) for case, outcome in candidate_pairs)
    expected_abstain = sum(evaluation.case.gold.expected_abstain for evaluation in evaluations)
    actual_abstain = sum(outcome.abstained for outcome in outcomes)
    true_abstain = sum(
        evaluation.case.gold.expected_abstain and outcome.abstained
        for evaluation, outcome in zip(evaluations, outcomes)
    )
    false_abstain = sum(
        not evaluation.case.gold.expected_abstain and outcome.abstained
        for evaluation, outcome in zip(evaluations, outcomes)
    )
    ranking = [
        values
        for evaluation, outcome in zip(evaluations, outcomes)
        if (values := ranking_values(evaluation.case, outcome)) is not None
    ]
    all_conflicts = [
        labelled_conflict_hits(evaluation.case, outcome) for evaluation, outcome in zip(evaluations, outcomes)
    ]
    high_risk_conflicts = [
        hits for evaluation, hits in zip(evaluations, all_conflicts) if evaluation.case.risk == "high"
    ]
    all_ineligible = [inventory_ineligible_hits(outcome, inventory) for outcome in outcomes]
    top1_conflicts = sum(
        outcome.top1 is not None and outcome.top1 in evaluation.case.gold.forbidden_candidates
        for evaluation, outcome in zip(evaluations, outcomes)
    )
    lexical_no_matches = [outcome.status == "no-match" for outcome in outcomes]
    expected_lexical_no_match = [evaluation.case.gold.expected_abstain for evaluation in evaluations]
    correct_lexical_no_match = sum(
        expected and observed for expected, observed in zip(expected_lexical_no_match, lexical_no_matches)
    )
    statuses = Counter(outcome.status for outcome in outcomes)
    return {
        "top1Accuracy": {"correct": correct, "total": len(evaluations), "rate": ratio(correct, len(evaluations))},
        "candidateTop1Accuracy": {
            "correct": candidate_correct,
            "total": len(candidate_pairs),
            "rate": ratio(candidate_correct, len(candidate_pairs)),
        },
        "abstention": {
            "expected": expected_abstain,
            "actual": actual_abstain,
            "correct": true_abstain,
            "falseAbstain": false_abstain,
            "missedAbstain": expected_abstain - true_abstain,
            "precision": ratio(true_abstain, actual_abstain),
            "recall": ratio(true_abstain, expected_abstain),
        },
        "expectedAbstainLexicalNoMatch": {
            "expected": expected_abstain,
            "actual": sum(lexical_no_matches),
            "correct": correct_lexical_no_match,
            "recall": ratio(correct_lexical_no_match, expected_abstain),
        },
        "recallAt3": {"eligibleCases": len(ranking), "mean": mean(values[0] for values in ranking)},
        "mrr": {"eligibleCases": len(ranking), "mean": mean(values[1] for values in ranking)},
        "precisionAt3": {"eligibleCases": len(ranking), "mean": mean(values[2] for values in ranking)},
        "labelledCandidateConflicts": {
            "top1Hits": top1_conflicts,
            "topKHits": sum(len(hits) for hits in all_conflicts),
            "topKAffectedCases": sum(bool(hits) for hits in all_conflicts),
            "highRiskTopKHits": sum(len(hits) for hits in high_risk_conflicts),
            "highRiskTopKAffectedCases": sum(bool(hits) for hits in high_risk_conflicts),
        },
        "inventoryIneligibleCandidates": {
            "hits": sum(len(hits) for hits in all_ineligible),
            "affectedCases": sum(bool(hits) for hits in all_ineligible),
        },
        "operationalFailures": sum(outcome.operational_failure for outcome in outcomes),
        "statuses": dict(sorted(statuses.items())),
    }


def comparison_summary(evaluations: tuple[CaseEvaluation, ...]) -> dict[str, int]:
    pairs = [
        (top1_correct(evaluation.case, evaluation.legacy), top1_correct(evaluation.case, evaluation.retrieval))
        for evaluation in evaluations
    ]
    rescue = sum(not legacy and retrieval for legacy, retrieval in pairs)
    harm = sum(legacy and not retrieval for legacy, retrieval in pairs)
    return {
        "rescue": rescue,
        "harm": harm,
        "netWin": rescue - harm,
        "bothCorrect": sum(legacy and retrieval for legacy, retrieval in pairs),
        "bothWrong": sum(not legacy and not retrieval for legacy, retrieval in pairs),
    }


def paired_statistics(evaluations: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    comparison = comparison_summary(evaluations)
    rescue = comparison["rescue"]
    harm = comparison["harm"]
    total = len(evaluations)
    discordant = rescue + harm
    if discordant:
        tail = sum(math.comb(discordant, offset) for offset in range(min(rescue, harm) + 1))
        exact_mcnemar_p = min(1.0, 2.0 * tail / (2**discordant))
    else:
        exact_mcnemar_p = 1.0

    deltas = [
        int(top1_correct(evaluation.case, evaluation.retrieval)) - int(top1_correct(evaluation.case, evaluation.legacy))
        for evaluation in evaluations
    ]
    net_win_rate = sum(deltas) / total if total else 0.0
    if total > 1:
        variance = sum((delta - net_win_rate) ** 2 for delta in deltas) / (total - 1)
        margin = 1.959963984540054 * math.sqrt(variance / total)
        normal_ci = [round(net_win_rate - margin, 6), round(net_win_rate + margin, 6)]
    else:
        normal_ci = None
    return {
        "discordant": discordant,
        "netWinRate": round(net_win_rate, 6),
        "exactMcNemarTwoSidedP": round(exact_mcnemar_p, 9),
        "pairedNormalApprox95Ci": normal_ci,
    }


def paired_recall_at_3_statistics(evaluations: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    deltas: list[float] = []
    for evaluation in evaluations:
        legacy_values = ranking_values(evaluation.case, evaluation.legacy)
        retrieval_values = ranking_values(evaluation.case, evaluation.retrieval)
        if legacy_values is None or retrieval_values is None:
            continue
        deltas.append(retrieval_values[0] - legacy_values[0])
    mean_uplift = sum(deltas) / len(deltas) if deltas else 0.0
    if len(deltas) > 1:
        variance = sum((delta - mean_uplift) ** 2 for delta in deltas) / (len(deltas) - 1)
        margin = 1.959963984540054 * math.sqrt(variance / len(deltas))
        normal_ci = [round(mean_uplift - margin, 6), round(mean_uplift + margin, 6)]
    else:
        normal_ci = None
    return {
        "pairs": len(deltas),
        "meanUplift": round(mean_uplift, 6),
        "pairedNormalApprox95Ci": normal_ci,
    }


def grouped_summaries(
    evaluations: tuple[CaseEvaluation, ...],
    field: str,
    inventory: InventorySnapshot,
) -> dict[str, Any]:
    grouped: dict[str, list[CaseEvaluation]] = {}
    for evaluation in evaluations:
        grouped.setdefault(str(getattr(evaluation.case, field)), []).append(evaluation)
    summaries: dict[str, Any] = {}
    for name, group in sorted(grouped.items()):
        candidate_only = tuple(evaluation for evaluation in group if not evaluation.case.gold.expected_abstain)
        summaries[name] = {
            "total": len(group),
            "a": system_summary(tuple(group), "legacy", inventory),
            "b": system_summary(tuple(group), "retrieval", inventory),
            "comparison": comparison_summary(candidate_only),
        }
    return summaries


def frozen_payload(frozen: FrozenInputs) -> dict[str, Any]:
    return {
        "configRevision": frozen.config_revision,
        "inventoryRevision": frozen.inventory_revision,
        "indexRevision": frozen.index_revision,
        "indexSchema": frozen.index_schema,
        "retrievalAlgorithm": frozen.retrieval_algorithm,
        "experimentCodeRevision": frozen.experiment_code_revision,
        "maxCandidates": frozen.max_candidates,
    }


def evidence_payload(evidence: ExperimentEvidence) -> dict[str, Any]:
    return {
        "schema": EXPERIMENT_EVIDENCE_SCHEMA,
        "corpusProvenance": evidence.corpus_provenance,
        "metadataProvenance": evidence.metadata_provenance,
        "independentHoldoutRevision": evidence.independent_holdout_revision,
        "independentAdjudicationRevision": evidence.independent_adjudication_revision,
        "ownershipObservationRevision": evidence.ownership_observation_revision,
        "activationObservationRevision": evidence.activation_observation_revision,
        "outcomeObservationRevision": evidence.outcome_observation_revision,
    }


def is_content_revision(value: Any) -> bool:
    return isinstance(value, str) and CONTENT_REVISION_RE.fullmatch(value) is not None


def is_finite_metric(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if minimum is not None and value < minimum:
        return False
    return maximum is None or value <= maximum


def is_nonnegative_count(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def evidence_verification_failure(evidence_type: str, reason: str) -> dict[str, str]:
    return {"type": evidence_type, "reason": reason}


def unavailable_evidence_verification() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "scope": "content-identity-only",
        "verifiedArtifactRevisions": [],
        "failures": [evidence_verification_failure("bundle", "artifact_root_not_configured")],
        "provesIndependence": False,
        "provesQuality": False,
    }


def evidence_artifact_open_supported() -> bool:
    return (
        os.open in os.supports_dir_fd
        and isinstance(getattr(os, "O_NOFOLLOW", None), int)
        and isinstance(getattr(os, "O_DIRECTORY", None), int)
        and isinstance(getattr(os, "O_NONBLOCK", None), int)
    )


def open_confined_evidence_artifact(root_fd: int, relative_path: str) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    parts = Path(relative_path).parts
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(parts[-1], file_flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def digest_file_descriptor(file_fd: int) -> str:
    hasher = hashlib.sha256()
    total_bytes = 0
    while True:
        try:
            chunk = os.read(file_fd, min(1024 * 1024, MAX_EVIDENCE_ARTIFACT_BYTES - total_bytes + 1))
        except InterruptedError:
            continue
        if not chunk:
            return hasher.hexdigest()
        total_bytes += len(chunk)
        if total_bytes > MAX_EVIDENCE_ARTIFACT_BYTES:
            raise EvidenceArtifactTooLargeError
        hasher.update(chunk)


def file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def file_read_fingerprint(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def verify_evidence_artifacts(
    evidence: ExperimentEvidence,
    artifact_root: Path | None,
) -> dict[str, Any]:
    if artifact_root is None:
        return unavailable_evidence_verification()
    if artifact_root.is_symlink():
        return {
            **unavailable_evidence_verification(),
            "status": "failed",
            "failures": [evidence_verification_failure("bundle", "artifact_root_invalid")],
        }
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return {
            **unavailable_evidence_verification(),
            "status": "failed",
            "failures": [evidence_verification_failure("bundle", "artifact_root_invalid")],
        }
    if not root.is_dir():
        return {
            **unavailable_evidence_verification(),
            "status": "failed",
            "failures": [evidence_verification_failure("bundle", "artifact_root_invalid")],
        }
    if not evidence_artifact_open_supported():
        return {
            **unavailable_evidence_verification(),
            "status": "failed",
            "failures": [evidence_verification_failure("bundle", "artifact_open_unsupported")],
        }

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, root_flags)
    except OSError:
        return {
            **unavailable_evidence_verification(),
            "status": "failed",
            "failures": [evidence_verification_failure("bundle", "artifact_root_invalid")],
        }

    paths = dict(evidence.artifact_paths)
    verified: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    validated: list[tuple[str, str, int, tuple[int, int], tuple[int, int, int, int, int, int]]] = []
    try:
        for evidence_type, revision_field in EVIDENCE_ARTIFACT_TYPES:
            expected_revision = getattr(evidence, revision_field)
            relative_path = paths.get(evidence_type)
            if not is_content_revision(expected_revision):
                failures.append(evidence_verification_failure(evidence_type, "revision_invalid"))
                continue
            if relative_path is None:
                failures.append(evidence_verification_failure(evidence_type, "path_missing"))
                continue
            try:
                safe_manifest_path(root, relative_path)
            except ValueError:
                failures.append(evidence_verification_failure(evidence_type, "path_unsafe"))
                continue
            try:
                file_fd = open_confined_evidence_artifact(root_fd, relative_path)
            except OSError:
                failures.append(evidence_verification_failure(evidence_type, "file_unavailable"))
                continue
            try:
                file_stat = os.fstat(file_fd)
            except OSError:
                os.close(file_fd)
                failures.append(evidence_verification_failure(evidence_type, "file_unreadable"))
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                failures.append(evidence_verification_failure(evidence_type, "file_not_regular"))
                continue
            if file_stat.st_size <= 0:
                os.close(file_fd)
                failures.append(evidence_verification_failure(evidence_type, "file_empty"))
                continue
            if file_stat.st_size > MAX_EVIDENCE_ARTIFACT_BYTES:
                os.close(file_fd)
                failures.append(evidence_verification_failure(evidence_type, "file_too_large"))
                continue
            validated.append(
                (
                    evidence_type,
                    expected_revision,
                    file_fd,
                    file_identity(file_stat),
                    file_read_fingerprint(file_stat),
                )
            )

        identity_counts = Counter(identity for _, _, _, identity, _ in validated)
        for evidence_type, expected_revision, file_fd, identity, initial_fingerprint in validated:
            if identity_counts[identity] > 1:
                failures.append(evidence_verification_failure(evidence_type, "path_reused"))
                continue
            try:
                observed_revision = "sha256:" + digest_file_descriptor(file_fd)
                final_fingerprint = file_read_fingerprint(os.fstat(file_fd))
            except EvidenceArtifactTooLargeError:
                failures.append(evidence_verification_failure(evidence_type, "file_too_large"))
                continue
            except OSError:
                failures.append(evidence_verification_failure(evidence_type, "file_unreadable"))
                continue
            if final_fingerprint != initial_fingerprint:
                failures.append(evidence_verification_failure(evidence_type, "file_changed_during_read"))
                continue
            if observed_revision != expected_revision:
                failures.append(evidence_verification_failure(evidence_type, "digest_mismatch"))
                continue
            verified.append({"type": evidence_type, "revision": observed_revision})
    finally:
        for _, _, file_fd, _, _ in validated:
            try:
                os.close(file_fd)
            except OSError:
                pass
        os.close(root_fd)

    return {
        "status": "passed" if not failures and len(verified) == len(EVIDENCE_ARTIFACT_TYPES) else "failed",
        "scope": "content-identity-only",
        "verifiedArtifactRevisions": verified,
        "failures": failures,
        "provesIndependence": False,
        "provesQuality": False,
    }


def report_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from report_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from report_keys(child)


def report_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from report_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from report_strings(child)
    elif isinstance(value, str):
        yield value


def stable_evaluation_payload(report: dict[str, Any]) -> dict[str, Any]:
    stable = json.loads(json.dumps(report, ensure_ascii=False))
    stable.pop("environment", None)
    for label in ("a", "b"):
        system = stable.get(label)
        if isinstance(system, dict):
            system.pop("latency", None)
    cases = stable.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict):
                continue
            for label in ("a", "b"):
                outcome = case.get(label)
                if isinstance(outcome, dict):
                    outcome.pop("latencyMs", None)
    return stable


def privacy_verification(report: dict[str, Any], manifest: ExperimentManifest) -> dict[str, Any]:
    forbidden = {"prompt", "rawprompt", "lastassistantmessage"}
    forbidden_keys = sorted({key for key in report_keys(report) if re.sub(r"[^a-z]", "", key.lower()) in forbidden})
    strings = tuple(report_strings(report))
    leaked_case_ids = sorted(case.case_id for case in manifest.cases if any(case.prompt in value for value in strings))
    raw_prompt_flag = report.get("protocol", {}).get("rawPromptsEmitted") is False
    result = {
        "status": "passed" if raw_prompt_flag and not forbidden_keys and not leaked_case_ids else "failed",
        "rawPromptsEmittedFlagFalse": raw_prompt_flag,
        "forbiddenKeys": forbidden_keys,
        "leakedCaseIds": leaked_case_ids,
        "minimumPromptLeakScanChars": 1,
    }
    result["revision"] = canonical_revision({"reportRevision": report.get("reportRevision"), **result})
    return result


def promotion_gate_v1(
    report: dict[str, Any],
    manifest: ExperimentManifest,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    evidence = manifest.evidence
    artifact_verification = verify_evidence_artifacts(evidence, artifact_root)
    independence_verified = artifact_verification.get("provesIndependence") is True
    privacy = privacy_verification(report, manifest)
    candidate_recall = report["b"]["recallAt3"]["mean"]
    candidate_top1 = report.get("b", {}).get("candidateTop1Accuracy", {}).get("rate")
    candidate_mrr = report.get("b", {}).get("mrr", {}).get("mean")
    candidate_precision_at_3 = report.get("b", {}).get("precisionAt3", {}).get("mean")
    expected_abstain_lexical_no_match = report.get("b", {}).get("expectedAbstainLexicalNoMatch", {}).get("recall")
    conflicts = report.get("b", {}).get("labelledCandidateConflicts", {})
    forbidden_top1_hits = conflicts.get("top1Hits") if isinstance(conflicts, dict) else None
    high_risk_forbidden_top_3_hits = conflicts.get("highRiskTopKHits") if isinstance(conflicts, dict) else None
    recall_statistics = report.get("candidateRecallAt3PairedStatistics", {})
    ci = recall_statistics.get("pairedNormalApprox95Ci") if isinstance(recall_statistics, dict) else None
    ci_valid = (
        isinstance(ci, list)
        and len(ci) == 2
        and all(is_finite_metric(value, minimum=-1.0, maximum=1.0) for value in ci)
        and ci[0] <= ci[1]
    )
    ci_lower = ci[0] if ci_valid else None
    ineligible_hits = report["b"]["inventoryIneligibleCandidates"]["hits"]
    operational_failures = report["b"]["operationalFailures"]
    p95_latency_ms = report["b"]["latency"]["p95Ms"]

    blockers: list[str] = []
    if artifact_verification["status"] == "unavailable":
        blockers.append("evidence_artifact_verifier_unavailable")
    elif artifact_verification["status"] != "passed":
        blockers.append("evidence_artifact_verification_failed")
    if not independence_verified:
        blockers.append("independent_evidence_unverified")
    if evidence.metadata_provenance == "corpus-informed-calibration":
        blockers.append("metadata_corpus_informed")
    elif evidence.metadata_provenance != "independent-catalog":
        blockers.append("independent_metadata_missing")
    if evidence.corpus_provenance != "independent-holdout" or not is_content_revision(
        evidence.independent_holdout_revision
    ):
        blockers.append("independent_holdout_missing")
    if not is_content_revision(evidence.independent_adjudication_revision):
        blockers.append("independent_adjudication_missing")
    if not is_content_revision(evidence.ownership_observation_revision):
        blockers.append("ownership_observation_missing")
    if not is_content_revision(evidence.activation_observation_revision):
        blockers.append("activation_observation_missing")
    if not is_content_revision(evidence.outcome_observation_revision):
        blockers.append("outcome_observation_missing")
    if privacy["status"] != "passed":
        blockers.append("privacy_verification_failed")
    candidate_recall_valid = is_finite_metric(candidate_recall, minimum=0.0, maximum=1.0)
    if not candidate_recall_valid:
        blockers.append("candidate_recall_missing")
    elif candidate_recall < PROMOTION_POLICY["minCandidateRecallAt3"]:
        blockers.append("candidate_recall_below_minimum")
    candidate_top1_valid = is_finite_metric(candidate_top1, minimum=0.0, maximum=1.0)
    if not candidate_top1_valid:
        blockers.append("candidate_top1_missing")
    elif candidate_top1 < PROMOTION_POLICY["minCandidateTop1"]:
        blockers.append("candidate_top1_below_minimum")
    candidate_mrr_valid = is_finite_metric(candidate_mrr, minimum=0.0, maximum=1.0)
    if not candidate_mrr_valid:
        blockers.append("candidate_mrr_missing")
    elif candidate_mrr < PROMOTION_POLICY["minCandidateMrr"]:
        blockers.append("candidate_mrr_below_minimum")
    candidate_precision_valid = is_finite_metric(candidate_precision_at_3, minimum=0.0, maximum=1.0)
    if not candidate_precision_valid:
        blockers.append("candidate_precision_at_3_missing")
    elif candidate_precision_at_3 < PROMOTION_POLICY["minCandidateMeanPrecisionAt3"]:
        blockers.append("candidate_precision_at_3_below_minimum")
    lexical_no_match_valid = is_finite_metric(
        expected_abstain_lexical_no_match,
        minimum=0.0,
        maximum=1.0,
    )
    if not lexical_no_match_valid:
        blockers.append("expected_abstain_lexical_no_match_missing")
    elif expected_abstain_lexical_no_match < PROMOTION_POLICY["minExpectedAbstainLexicalNoMatchRecall"]:
        blockers.append("expected_abstain_lexical_no_match_below_minimum")
    forbidden_top1_valid = is_nonnegative_count(forbidden_top1_hits)
    if not forbidden_top1_valid:
        blockers.append("forbidden_top1_count_missing")
    elif forbidden_top1_hits > PROMOTION_POLICY["maxForbiddenTop1Hits"]:
        blockers.append("forbidden_top1_candidate")
    high_risk_forbidden_valid = is_nonnegative_count(high_risk_forbidden_top_3_hits)
    if not high_risk_forbidden_valid:
        blockers.append("high_risk_forbidden_top3_count_missing")
    elif high_risk_forbidden_top_3_hits > PROMOTION_POLICY["maxHighRiskForbiddenTop3Hits"]:
        blockers.append("high_risk_forbidden_top3_candidate")
    ineligible_hits_valid = is_nonnegative_count(ineligible_hits)
    if not ineligible_hits_valid:
        blockers.append("inventory_eligibility_missing")
    elif ineligible_hits > PROMOTION_POLICY["maxInventoryIneligibleHits"]:
        blockers.append("inventory_ineligible_candidate")
    operational_failures_valid = is_nonnegative_count(operational_failures)
    if not operational_failures_valid:
        blockers.append("operational_failure_count_missing")
    elif operational_failures > PROMOTION_POLICY["maxOperationalFailures"]:
        blockers.append("operational_failure")
    if not ci_valid:
        blockers.append("candidate_recall_at_3_ci_missing")
    elif ci_lower <= PROMOTION_POLICY["candidateRecallAt3CiLowerBoundExclusive"]:
        blockers.append("candidate_recall_at_3_ci_nonpositive")
    p95_latency_valid = is_finite_metric(p95_latency_ms, minimum=0.0)
    if not p95_latency_valid:
        blockers.append("latency_budget_missing")
    elif p95_latency_ms > PROMOTION_POLICY["maxCandidateP95LatencyMs"]:
        blockers.append("latency_budget_exceeded")

    checks = {
        "artifactEvidence": "passed" if artifact_verification["status"] == "passed" else "blocked",
        "independentEvidence": "passed" if independence_verified else "blocked",
        "privacy": privacy["status"],
        "candidateRecallAt3": (
            "passed"
            if candidate_recall_valid and candidate_recall >= PROMOTION_POLICY["minCandidateRecallAt3"]
            else "blocked"
        ),
        "candidateTop1": (
            "passed" if candidate_top1_valid and candidate_top1 >= PROMOTION_POLICY["minCandidateTop1"] else "blocked"
        ),
        "candidateMrr": (
            "passed" if candidate_mrr_valid and candidate_mrr >= PROMOTION_POLICY["minCandidateMrr"] else "blocked"
        ),
        "candidatePrecisionAt3": (
            "passed"
            if candidate_precision_valid
            and candidate_precision_at_3 >= PROMOTION_POLICY["minCandidateMeanPrecisionAt3"]
            else "blocked"
        ),
        "expectedAbstainLexicalNoMatch": (
            "passed"
            if lexical_no_match_valid
            and expected_abstain_lexical_no_match >= PROMOTION_POLICY["minExpectedAbstainLexicalNoMatchRecall"]
            else "blocked"
        ),
        "forbiddenTop1": (
            "passed"
            if forbidden_top1_valid and forbidden_top1_hits <= PROMOTION_POLICY["maxForbiddenTop1Hits"]
            else "blocked"
        ),
        "highRiskForbiddenTop3": (
            "passed"
            if high_risk_forbidden_valid
            and high_risk_forbidden_top_3_hits <= PROMOTION_POLICY["maxHighRiskForbiddenTop3Hits"]
            else "blocked"
        ),
        "inventoryEligibility": (
            "passed"
            if ineligible_hits_valid and ineligible_hits <= PROMOTION_POLICY["maxInventoryIneligibleHits"]
            else "blocked"
        ),
        "operationalReliability": (
            "passed"
            if operational_failures_valid and operational_failures <= PROMOTION_POLICY["maxOperationalFailures"]
            else "blocked"
        ),
        "candidateRecallAt3Ci": (
            "passed"
            if ci_valid and ci_lower > PROMOTION_POLICY["candidateRecallAt3CiLowerBoundExclusive"]
            else "blocked"
        ),
        "latencyBudget": (
            "passed"
            if p95_latency_valid and p95_latency_ms <= PROMOTION_POLICY["maxCandidateP95LatencyMs"]
            else "blocked"
        ),
    }
    checks_passed = all(value == "passed" for value in checks.values())
    if not checks_passed and not blockers:
        blockers.append("gate_check_incomplete")
    policy_revision = canonical_revision(PROMOTION_POLICY)
    evidence_value = evidence_payload(evidence)
    status = "eligible-for-human-review" if not blockers and checks_passed else "blocked"
    decision_payload = {
        "schema": PROMOTION_GATE_SCHEMA,
        "status": status,
        "authority": "none",
        "autoPromote": False,
        "reportRevision": report["reportRevision"],
        "policyRevision": policy_revision,
        "evidence": evidence_value,
        "checks": checks,
        "blockers": blockers,
    }
    payload = {
        "schema": PROMOTION_GATE_SCHEMA,
        "status": status,
        "authority": "none",
        "autoPromote": False,
        "reportRevision": report["reportRevision"],
        "benchmarkRunRevision": report["runRevision"],
        "policyRevision": policy_revision,
        "policy": dict(PROMOTION_POLICY),
        "evidence": evidence_value,
        "evidenceVerification": artifact_verification,
        "checks": checks,
        "observed": {
            "candidateRecallAt3": candidate_recall,
            "candidateTop1": candidate_top1,
            "candidateMrr": candidate_mrr,
            "candidatePrecisionAt3": candidate_precision_at_3,
            "expectedAbstainLexicalNoMatchRecall": expected_abstain_lexical_no_match,
            "forbiddenTop1Hits": forbidden_top1_hits,
            "highRiskForbiddenTop3Hits": high_risk_forbidden_top_3_hits,
            "candidateRecallAt3CiLowerBound": ci_lower,
            "inventoryIneligibleHits": ineligible_hits,
            "operationalFailures": operational_failures,
            "candidateP95LatencyMs": p95_latency_ms,
            "privacyVerification": privacy,
            "independenceVerified": independence_verified,
        },
        "blockers": blockers,
    }
    payload["gateRevision"] = canonical_revision(decision_payload)
    payload["runRevision"] = canonical_revision(payload)
    return payload


def outcome_payload(
    case: ABCase,
    outcome: SystemOutcome,
    inventory: InventorySnapshot,
) -> dict[str, Any]:
    return {
        "top1": outcome.top1,
        "candidates": list(outcome.candidates[:TOP_K]),
        "abstained": outcome.abstained,
        "status": outcome.status,
        "operationalFailure": outcome.operational_failure,
        "correct": top1_correct(case, outcome),
        "labelledConflictHits": list(labelled_conflict_hits(case, outcome)),
        "inventoryIneligibleHits": list(inventory_ineligible_hits(outcome, inventory)),
        "latencyMs": round(outcome.latency_ms, 4),
    }


def report_payload(
    manifest: ExperimentManifest,
    inputs: VerifiedInputs,
    evaluations: tuple[CaseEvaluation, ...],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    candidate_only = tuple(evaluation for evaluation in evaluations if not evaluation.case.gold.expected_abstain)
    report = {
        "schema": EXPERIMENT_REPORT_SCHEMA,
        "manifestRevision": manifest.revision,
        "frozenInputs": frozen_payload(inputs.frozen),
        "environment": {
            "pythonVersion": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "protocol": {
            "paired": True,
            "variantA": "legacy-route-plus-activation-ir",
            "variantB": "capability-top-k-shadow",
            "variantACandidateSet": "activation-ir-primary-only",
            "executionOrder": "alternating-ab-ba",
            "topK": TOP_K,
            "rankingMetricsExcludeGoldAbstainCases": True,
            "pairedComparisonsExcludeGoldAbstainCases": True,
            "precisionDenominator": "returned-candidates-up-to-top-k",
            "variantBProducesCandidatesNotFinalOwnership": True,
            "retrievalNoMatchIsNotSemanticAbstention": True,
            "labelledCandidateConflictsAreNotInventoryEligibility": True,
            "rawPromptsEmitted": False,
            "automaticPromotion": False,
        },
        "total": len(evaluations),
        "a": {
            **system_summary(evaluations, "legacy", inputs.inventory),
            "latency": latency_summary(evaluations, "legacy"),
        },
        "b": {
            **system_summary(evaluations, "retrieval", inputs.inventory),
            "latency": latency_summary(evaluations, "retrieval"),
        },
        "comparison": comparison_summary(candidate_only),
        "pairedStatistics": paired_statistics(candidate_only),
        "candidateOnlyComparison": comparison_summary(candidate_only),
        "candidateOnlyPairedStatistics": paired_statistics(candidate_only),
        "candidateRecallAt3PairedStatistics": paired_recall_at_3_statistics(candidate_only),
        "slices": {
            "category": grouped_summaries(evaluations, "category", inputs.inventory),
            "language": grouped_summaries(evaluations, "language", inputs.inventory),
            "risk": grouped_summaries(evaluations, "risk", inputs.inventory),
        },
        "cases": [
            {
                "id": evaluation.case.case_id,
                "category": evaluation.case.category,
                "language": evaluation.case.language,
                "risk": evaluation.case.risk,
                "gold": {
                    "primary": evaluation.case.gold.primary,
                    "expectedAbstain": evaluation.case.gold.expected_abstain,
                },
                "a": outcome_payload(evaluation.case, evaluation.legacy, inputs.inventory),
                "b": outcome_payload(evaluation.case, evaluation.retrieval, inputs.inventory),
            }
            for evaluation in evaluations
        ],
    }
    report["reportRevision"] = canonical_revision(stable_evaluation_payload(report))
    report["runRevision"] = canonical_revision(report)
    report["promotionGate"] = promotion_gate_v1(report, manifest, artifact_root=artifact_root)
    return report


def print_text_report(report: dict[str, Any]) -> None:
    print(f"Paired router A/B: {report['total']} cases")
    print(f"Frozen config: {report['frozenInputs']['configRevision']}")
    print(f"Frozen inventory: {report['frozenInputs']['inventoryRevision']}")
    print(f"Frozen index: {report['frozenInputs']['indexRevision']}")
    for label in ("a", "b"):
        system = report[label]
        top1 = system["candidateTop1Accuracy"]
        latency = system["latency"]
        lexical_no_match = system["expectedAbstainLexicalNoMatch"]
        print(
            f"{label.upper()} candidate top1: {top1['correct']}/{top1['total']} ({top1['rate']}); "
            f"Recall@3: {system['recallAt3']['mean']}; MRR: {system['mrr']['mean']}; "
            f"Precision@3: {system['precisionAt3']['mean']}; "
            f"expected-abstain lexical no-match: {lexical_no_match['recall']}; "
            f"p95: {latency['p95Ms']} ms; "
            f"labelled conflicts Top1/TopK: "
            f"{system['labelledCandidateConflicts']['top1Hits']}/"
            f"{system['labelledCandidateConflicts']['topKHits']}; "
            f"ineligible: {system['inventoryIneligibleCandidates']['hits']}"
        )
    comparison = report["comparison"]
    print(f"B vs A: rescue={comparison['rescue']} harm={comparison['harm']} netWin={comparison['netWin']}")
    statistics = report["pairedStatistics"]
    print(
        f"Paired effect: netWinRate={statistics['netWinRate']}; "
        f"95% CI={statistics['pairedNormalApprox95Ci']}; "
        f"exact McNemar p={statistics['exactMcNemarTwoSidedP']}"
    )
    gate = report["promotionGate"]
    print(
        f"Promotion gate: {gate['status']} (authority={gate['authority']}, autoPromote={gate['autoPromote']}); "
        f"blockers={gate['blockers']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a frozen paired offline A/B evaluation of router variants.")
    parser.add_argument("manifest", help="Pre-labelled experiment manifest JSON path.")
    parser.add_argument("--config", required=True, help="Frozen legacy route config JSON path.")
    parser.add_argument("--inventory", required=True, help="Frozen skill inventory manifest path.")
    parser.add_argument("--index", required=True, help="Frozen capability index path.")
    parser.add_argument(
        "--artifact-root",
        help="Explicit local root for evidence artifactPaths; omitted evidence remains unverified.",
    )
    parser.add_argument("--json", action="store_true", help="Print the redacted machine-readable report.")
    parser.add_argument("--output", help="Write the redacted JSON report to this path instead of stdout.")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(Path(args.manifest))
        config = load_config(Path(args.config))
        inputs = verify_inputs(config, Path(args.inventory), Path(args.index), manifest.frozen)
        evaluations = evaluate_cases(manifest.cases, inputs)
        report = report_payload(
            manifest,
            inputs,
            evaluations,
            artifact_root=Path(args.artifact_root) if args.artifact_root else None,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.output:
        try:
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: failed to write report: {exc}")
            return 2
    elif args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
