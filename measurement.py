from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final

from lazy_skill_router_core import load_config
from lazy_skill_router_logging import (
    AUTOMATED_OBJECTIVE_PARSER_REVISION,
    AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA,
    ROUTING_OBSERVATION_SCHEMA,
    append_measurement_event,
    bounded_identifier,
    config_revision,
    hash_identifier,
    is_measurement_event,
    measurement_log_path,
    policy_version,
    read_measurement_events,
)

MEASUREMENT_REPORT_SCHEMA = "lazy-skill-router.measurement-report/v1"
AUTOMATED_SHADOW_EVIDENCE_SCHEMA: Final = "lazy-skill-router.automated-shadow-evidence/v1"
OUTCOME_ARMS = ("inject", "native", "oracle", "shadow")
OUTCOME_STATUSES = ("fail", "pass", "unknown")
OUTCOME_SOURCES = ("grader", "human", "objective")
DECISION_CONTEXT_FIELDS = ("policyVersion", "configRevision", "catalogRevision", "runtimeRevision")
OUTCOME_CONTEXT_FIELDS = ("policyVersion", "configRevision")
MAX_ROUTING_OBSERVATION_LATENCY_MS = 86_400_000
ROUTING_OBSERVATION_FIELDS = frozenset(
    {"schema", "lane", "mode", "retrieval", "ownership", "activation", "stop", "semantics"}
)
RETRIEVAL_OBSERVATION_FIELDS = frozenset({"revision", "status", "candidates", "latencyMs", "reasonCodes"})
RETRIEVAL_CANDIDATE_FIELDS = frozenset({"skillId", "evidenceIds"})
OWNERSHIP_OBSERVATION_FIELDS = frozenset({"status", "primarySkillId", "reasonCode"})
ACTIVATION_OBSERVATION_FIELDS = frozenset({"source", "disposition", "legacyPrimarySkillId", "injected"})
STOP_OBSERVATION_FIELDS = frozenset({"action", "reasonCode", "affectsLegacySelection"})
SEMANTICS_OBSERVATION_FIELDS = frozenset(
    {"rawPromptStored", "semanticAbstentionObserved", "disagreementIsFallbackEvidence", "automaticPromotion"}
)
AUTOMATED_OBJECTIVE_SIGNAL_FIELDS = frozenset(
    {"schema", "kind", "expectedSkillIds", "source", "parserRevision", "reasonCode", "rawPromptStored"}
)
AUTOMATED_SHADOW_POLICY: Final = {
    "minUniqueExplicitReferenceCases": 100,
    "minExplicitReferenceRecallAt3": 0.95,
    "minExplicitReferenceTop1Accuracy": 0.90,
    "maxDegradedObservations": 0,
    "maxCandidateP95LatencyMs": 20.0,
    "maxInvalidObservations": 0,
    "maxInvalidObjectiveSignals": 0,
    "maxConflictingExplicitReferenceCases": 0,
    "maxLegacySelectionAffected": 0,
    "maxAutomaticPromotionRequested": 0,
}
AUTOMATED_PROMOTION_BLOCKERS: Final = (
    "explicit_reference_scope_only",
    "independent_holdout_not_proven",
    "independent_adjudication_not_proven",
    "ownership_unobserved",
    "semantic_abstention_unobserved",
    "outcome_runtime_linkage_unavailable",
)
SHA256_REVISION_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
RETRIEVAL_ALGORITHMS: Final = frozenset({"lexical-bm25-char3/v1", "lexical-bm25-char3-anchored/v2"})


def load_measurement_config(config_path: str | None) -> dict[str, Any]:
    if config_path is None:
        return {}
    return load_config(Path(__file__).resolve(), config_path)


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def canonical_revision(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def latency_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(
        float(value)
        for event in events
        if not isinstance((value := event.get("latencyMs")), bool) and isinstance(value, (int, float)) and value >= 0
    )
    if not values:
        return {"count": 0, "mean": None, "p95": None, "max": None}
    p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 3),
        "p95": round(values[p95_index], 3),
        "max": round(values[-1], 3),
    }


def outcome_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in sorted({str(event.get("arm")) for event in events if isinstance(event.get("arm"), str)}):
        arm_events = [event for event in events if event.get("arm") == arm]
        passed = sum(event.get("status") == "pass" for event in arm_events)
        failed = sum(event.get("status") == "fail" for event in arm_events)
        unknown = sum(event.get("status") == "unknown" for event in arm_events)
        by_arm[arm] = {
            "total": len(arm_events),
            "passed": passed,
            "failed": failed,
            "unknown": unknown,
            "successRate": rate(passed, passed + failed),
        }
    return {"byArm": by_arm}


def context_key(event: dict[str, Any], fields: tuple[str, ...]) -> tuple[str | None, ...]:
    return tuple(value if isinstance((value := event.get(field)), str) and value else None for field in fields)


def context_segments(events: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    counts = Counter(context_key(event, fields) for event in events)
    segments = []
    for values, count in sorted(counts.items(), key=lambda item: json.dumps(item[0], sort_keys=True)):
        segments.append({**dict(zip(fields, values)), "events": count})
    return segments


def lifecycle_key(event: dict[str, Any]) -> tuple[str, str] | None:
    session_hash = event.get("sessionHash")
    turn_hash = event.get("turnHash")
    if not isinstance(session_hash, str) or not isinstance(turn_hash, str):
        return None
    return session_hash, turn_hash


def outcome_identity(event: dict[str, Any]) -> tuple[Any, ...] | None:
    replicate = event.get("replicate")
    arm = event.get("arm")
    status = event.get("status")
    source = event.get("source")
    if (
        isinstance(replicate, bool)
        or not isinstance(replicate, int)
        or replicate <= 0
        or arm not in OUTCOME_ARMS
        or status not in OUTCOME_STATUSES
        or source not in OUTCOME_SOURCES
    ):
        return None

    case_hash = event.get("caseHash")
    turn_hash = event.get("turnHash")
    if isinstance(case_hash, str) and case_hash:
        subject: tuple[Any, ...] = ("case", case_hash)
    elif isinstance(turn_hash, str) and turn_hash:
        session_hash = event.get("sessionHash")
        if not isinstance(session_hash, str) or not session_hash:
            return None
        subject = ("turn", session_hash, turn_hash)
    else:
        return None
    return (*context_key(event, OUTCOME_CONTEXT_FIELDS), *subject, replicate, arm)


def normalize_outcomes(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    invalid = 0
    for event in events:
        identity = outcome_identity(event)
        if identity is None:
            invalid += 1
            continue
        grouped.setdefault(identity, []).append(event)

    usable = []
    duplicates = conflicts = conflicting_events = 0
    for group in grouped.values():
        statuses = {event["status"] for event in group}
        if len(statuses) > 1:
            conflicts += 1
            conflicting_events += len(group)
            continue
        usable.append(group[0])
        duplicates += len(group) - 1
    return usable, {
        "total": len(events),
        "usable": len(usable),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "conflictingEvents": conflicting_events,
        "invalid": invalid,
    }


def paired_native_inject(events: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str | None, str | None, str, int], dict[str, str]] = {}
    for event in events:
        case_hash = event.get("caseHash")
        replicate = event.get("replicate")
        arm = event.get("arm")
        status = event.get("status")
        if (
            not isinstance(case_hash, str)
            or isinstance(replicate, bool)
            or not isinstance(replicate, int)
            or arm not in {"native", "inject"}
            or status not in {"pass", "fail"}
        ):
            continue
        context = context_key(event, OUTCOME_CONTEXT_FIELDS)
        grouped.setdefault((*context, case_hash, replicate), {})[str(arm)] = str(status)

    pairs = rescues = harms = both_pass = both_fail = 0
    for arms in grouped.values():
        native = arms.get("native")
        inject = arms.get("inject")
        if native is None or inject is None:
            continue
        pairs += 1
        if native == "fail" and inject == "pass":
            rescues += 1
        elif native == "pass" and inject == "fail":
            harms += 1
        elif native == "pass" and inject == "pass":
            both_pass += 1
        else:
            both_fail += 1
    return {
        "pairs": pairs,
        "rescues": rescues,
        "harms": harms,
        "bothPass": both_pass,
        "bothFail": both_fail,
        "rescueRate": rate(rescues, pairs),
        "harmRate": rate(harms, pairs),
        "netWin": rescues - harms,
    }


def is_bounded_identifier(value: Any) -> bool:
    return isinstance(value, str) and bounded_identifier(value) == value


def is_bounded_identifier_list(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(is_bounded_identifier(item) for item in value)
        and len(value) == len(set(value))
    )


def is_routing_candidate(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == RETRIEVAL_CANDIDATE_FIELDS
        and is_bounded_identifier(value.get("skillId"))
        and is_bounded_identifier_list(value.get("evidenceIds"), 8)
    )


def is_routing_latency(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value < 0 or value > MAX_ROUTING_OBSERVATION_LATENCY_MS:
        return False
    return not isinstance(value, float) or math.isfinite(value)


def is_routing_observation(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != ROUTING_OBSERVATION_FIELDS
        or value.get("schema") != ROUTING_OBSERVATION_SCHEMA
        or value.get("lane") != "capability-retrieval"
        or value.get("mode") != "shadow"
    ):
        return False
    retrieval = value.get("retrieval")
    ownership = value.get("ownership")
    activation = value.get("activation")
    stop = value.get("stop")
    semantics = value.get("semantics")
    retrieval_status = retrieval.get("status") if isinstance(retrieval, dict) else None
    stop_action = stop.get("action") if isinstance(stop, dict) else None
    expected_actions = {
        "matched": "observe-only",
        "no-match": "observe-only",
        "degraded": {"fallback-legacy", "stop-shadow"},
    }
    expected_reasons = {
        ("matched", "observe-only"): "ownership_unobserved",
        ("no-match", "observe-only"): "lexical_no_match_not_semantic_abstain",
        ("degraded", "fallback-legacy"): "retrieval_unusable",
        ("degraded", "stop-shadow"): "retrieval_unusable_no_legacy_selection",
    }
    expected_action = expected_actions.get(retrieval_status) if isinstance(retrieval_status, str) else None
    action_matches = isinstance(stop_action, str) and (
        stop_action in expected_action if isinstance(expected_action, set) else stop_action == expected_action
    )
    activation_source = activation.get("source") if isinstance(activation, dict) else None
    activation_disposition = activation.get("disposition") if isinstance(activation, dict) else None
    injected = activation.get("injected") if isinstance(activation, dict) else None
    activation_matches = (
        activation_source == "legacy-route-plus-activation-ir"
        and isinstance(activation_disposition, str)
        and activation_disposition in {"activate", "propose", "abstain"}
        and isinstance(injected, bool)
        and not (activation_disposition == "abstain" and injected)
    ) or (
        activation_source == "unobserved"
        and activation_disposition is None
        and injected is False
        and activation.get("legacyPrimarySkillId") is None
    )
    stop_matches_activation = not (
        (stop_action == "fallback-legacy" and activation_source != "legacy-route-plus-activation-ir")
        or (stop_action == "stop-shadow" and activation_source != "unobserved")
    )
    retrieval_candidates = retrieval.get("candidates") if isinstance(retrieval, dict) else None
    candidates_match = (
        isinstance(retrieval_candidates, list)
        and len(retrieval_candidates) <= 3
        and all(is_routing_candidate(candidate) for candidate in retrieval_candidates)
        and len({candidate["skillId"] for candidate in retrieval_candidates}) == len(retrieval_candidates)
        and (retrieval_status != "matched" or bool(retrieval_candidates))
        and (retrieval_status == "matched" or not retrieval_candidates)
    )
    retrieval_latency = retrieval.get("latencyMs") if isinstance(retrieval, dict) else None
    latency_matches = retrieval_latency is None or is_routing_latency(retrieval_latency)
    retrieval_revision = retrieval.get("revision") if isinstance(retrieval, dict) else None
    legacy_primary = activation.get("legacyPrimarySkillId") if isinstance(activation, dict) else None
    return (
        isinstance(retrieval, dict)
        and set(retrieval) == RETRIEVAL_OBSERVATION_FIELDS
        and isinstance(retrieval_status, str)
        and retrieval_status in expected_actions
        and (retrieval_revision is None or is_bounded_identifier(retrieval_revision))
        and candidates_match
        and latency_matches
        and is_bounded_identifier_list(retrieval.get("reasonCodes"), 8)
        and isinstance(ownership, dict)
        and set(ownership) == OWNERSHIP_OBSERVATION_FIELDS
        and ownership.get("status") == "unobserved"
        and ownership.get("primarySkillId") is None
        and ownership.get("reasonCode") == "host_ownership_observation_unavailable"
        and isinstance(activation, dict)
        and set(activation) == ACTIVATION_OBSERVATION_FIELDS
        and (legacy_primary is None or is_bounded_identifier(legacy_primary))
        and activation_matches
        and isinstance(stop, dict)
        and set(stop) == STOP_OBSERVATION_FIELDS
        and action_matches
        and stop.get("reasonCode") == expected_reasons.get((retrieval_status, stop_action))
        and stop_matches_activation
        and stop.get("affectsLegacySelection") is False
        and isinstance(semantics, dict)
        and set(semantics) == SEMANTICS_OBSERVATION_FIELDS
        and semantics.get("rawPromptStored") is False
        and semantics.get("semanticAbstentionObserved") is False
        and semantics.get("disagreementIsFallbackEvidence") is False
        and semantics.get("automaticPromotion") is False
    )


def routing_observation_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    current_schema_observations = [
        observation
        for event in decisions
        if isinstance((observation := event.get("routingObservation")), dict)
        and observation.get("schema") == ROUTING_OBSERVATION_SCHEMA
    ]
    observations = [observation for observation in current_schema_observations if is_routing_observation(observation)]
    retrieval_statuses = Counter(
        str(retrieval["status"])
        for observation in observations
        if isinstance((retrieval := observation.get("retrieval")), dict) and isinstance(retrieval.get("status"), str)
    )
    ownership_statuses = Counter(
        str(ownership["status"])
        for observation in observations
        if isinstance((ownership := observation.get("ownership")), dict) and isinstance(ownership.get("status"), str)
    )
    stop_actions = Counter(
        str(stop["action"])
        for observation in observations
        if isinstance((stop := observation.get("stop")), dict) and isinstance(stop.get("action"), str)
    )
    semantics = [value for observation in observations if isinstance((value := observation.get("semantics")), dict)]
    stops = [value for observation in observations if isinstance((value := observation.get("stop")), dict)]
    return {
        "total": len(observations),
        "invalid": len(current_schema_observations) - len(observations),
        "decisionCoverage": rate(len(observations), len(decisions)),
        "byRetrievalStatus": dict(sorted(retrieval_statuses.items())),
        "byOwnershipStatus": dict(sorted(ownership_statuses.items())),
        "byStopAction": dict(sorted(stop_actions.items())),
        "semanticAbstentionObserved": sum(value.get("semanticAbstentionObserved") is True for value in semantics),
        "legacySelectionAffected": sum(value.get("affectsLegacySelection") is True for value in stops),
        "automaticPromotionRequested": sum(value.get("automaticPromotion") is True for value in semantics),
    }


def is_automated_objective_signal(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != AUTOMATED_OBJECTIVE_SIGNAL_FIELDS
        or value.get("schema") != AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA
        or value.get("kind") not in {"explicit-skill-reference", "unlabelled"}
        or value.get("source") != "local-deterministic-parser"
        or value.get("parserRevision") != AUTOMATED_OBJECTIVE_PARSER_REVISION
        or value.get("rawPromptStored") is not False
    ):
        return False
    expected = value.get("expectedSkillIds")
    kind = value.get("kind")
    return (
        is_bounded_identifier_list(expected, 3)
        and bool(expected) == (kind == "explicit-skill-reference")
        and value.get("reasonCode")
        == ("deterministic_exact_reference" if kind == "explicit-skill-reference" else "no_exact_reference")
    )


def valid_prompt_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 16 and all(character in "0123456789abcdef" for character in value)


def valid_sha256_revision(value: Any) -> bool:
    return isinstance(value, str) and SHA256_REVISION_RE.fullmatch(value) is not None


def valid_automated_decision_context(event: dict[str, Any]) -> bool:
    return bounded_identifier(event.get("policyVersion")) is not None and all(
        valid_sha256_revision(event.get(field)) for field in DECISION_CONTEXT_FIELDS[1:]
    )


def retrieval_context(
    event: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[str, str, str] | None:
    retrieval = observation.get("retrieval")
    if not isinstance(retrieval, dict):
        return None
    algorithm = event.get("retrievalAlgorithm")
    implementation_revision = event.get("retrievalImplementationRevision")
    index_revision = retrieval.get("revision")
    if (
        not isinstance(algorithm, str)
        or algorithm not in RETRIEVAL_ALGORITHMS
        or not valid_sha256_revision(implementation_revision)
        or not valid_sha256_revision(index_revision)
    ):
        return None
    return str(algorithm), str(implementation_revision), str(index_revision)


def build_automated_shadow_evidence(events: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [event for event in events if is_measurement_event(event)]
    decisions = [event for event in accepted if event.get("eventType") == "decision"]
    capability_decisions = [
        event
        for event in decisions
        if isinstance(event.get("retrievalStatus"), str)
        or (
            isinstance((observation := event.get("routingObservation")), dict)
            and observation.get("schema") == ROUTING_OBSERVATION_SCHEMA
        )
    ]
    valid_pairs = [
        (event, observation)
        for event in capability_decisions
        if isinstance((observation := event.get("routingObservation")), dict)
        and observation.get("schema") == ROUTING_OBSERVATION_SCHEMA
        and is_routing_observation(observation)
    ]
    valid_context_pairs = [
        (event, observation, context)
        for event, observation in valid_pairs
        if (context := retrieval_context(event, observation)) is not None
    ]
    valid_signals = [
        (event, observation, signal, context)
        for event, observation, context in valid_context_pairs
        if is_automated_objective_signal(signal := event.get("automatedObjectiveSignal"))
    ]
    invalid_observations = len(capability_decisions) - len(valid_pairs)
    invalid_retrieval_contexts = len(valid_pairs) - len(valid_context_pairs)
    invalid_signals = len(valid_context_pairs) - len(valid_signals)

    grouped: dict[tuple[str, str, str, str], list[tuple[tuple[str, ...], tuple[str, ...]]]] = {}
    invalid_labeled_hashes = 0
    for event, observation, signal, context in valid_signals:
        if signal["kind"] != "explicit-skill-reference":
            continue
        prompt_hash_value = event.get("promptHash")
        if not valid_prompt_hash(prompt_hash_value):
            invalid_labeled_hashes += 1
            continue
        retrieval = observation["retrieval"]
        expected = tuple(signal["expectedSkillIds"])
        candidates = tuple(candidate["skillId"] for candidate in retrieval["candidates"])
        grouped.setdefault((str(prompt_hash_value), *context), []).append((expected, candidates))

    grouped_sets = {key: set(values) for key, values in grouped.items()}
    conflicts = sum(len(values) > 1 for values in grouped_sets.values())
    unique_cases = [next(iter(values)) for values in grouped_sets.values() if len(values) == 1]
    duplicates = sum(len(grouped[key]) - 1 for key, values in grouped_sets.items() if len(values) == 1)
    expected_references = sum(len(expected) for expected, _ in unique_cases)
    reference_hits = sum(len(set(expected) & set(candidates[:3])) for expected, candidates in unique_cases)
    top1_correct = sum(bool(candidates) and candidates[0] in expected for expected, candidates in unique_cases)
    explicit_recall = rate(reference_hits, expected_references)
    explicit_top1 = rate(top1_correct, len(unique_cases))

    retrieval_revisions = sorted(
        {
            str(retrieval["revision"])
            for _, observation in valid_pairs
            if isinstance((retrieval := observation.get("retrieval")), dict)
            and valid_sha256_revision(retrieval.get("revision"))
        }
    )
    invalid_retrieval_revisions = sum(
        not valid_sha256_revision(observation["retrieval"].get("revision")) for _, observation in valid_pairs
    )
    retrieval_contexts = sorted({context for _, _, context in valid_context_pairs})
    decision_contexts = context_segments([event for event, _ in valid_pairs], DECISION_CONTEXT_FIELDS)
    invalid_decision_contexts = sum(not valid_automated_decision_context(event) for event, _ in valid_pairs)
    valid_context_events = [event for event, _ in valid_pairs if valid_automated_decision_context(event)]
    parser_revisions = sorted(
        {
            str(signal["parserRevision"])
            for _, _, signal, _ in valid_signals
            if isinstance(signal.get("parserRevision"), str)
        }
    )
    degraded = sum(observation["retrieval"]["status"] == "degraded" for _, observation in valid_pairs)
    latency = latency_summary(
        [
            {"latencyMs": observation["retrieval"]["latencyMs"]}
            for _, observation in valid_pairs
            if observation["retrieval"]["latencyMs"] is not None
        ]
    )
    semantics = [observation["semantics"] for _, observation in valid_pairs]
    stops = [observation["stop"] for _, observation in valid_pairs]
    legacy_selection_affected = sum(stop["affectsLegacySelection"] is True for stop in stops)
    automatic_promotion_requested = sum(value["automaticPromotion"] is True for value in semantics)

    collection_blockers: list[str] = []
    if not valid_pairs:
        collection_blockers.append("no_current_routing_observations")
    if invalid_observations > AUTOMATED_SHADOW_POLICY["maxInvalidObservations"]:
        collection_blockers.append("invalid_routing_observations")
    if invalid_signals + invalid_labeled_hashes > AUTOMATED_SHADOW_POLICY["maxInvalidObjectiveSignals"]:
        collection_blockers.append("invalid_automated_objective_signals")
    if invalid_retrieval_revisions:
        collection_blockers.append("retrieval_revision_missing_or_invalid")
    if len(retrieval_revisions) > 1:
        collection_blockers.append("mixed_retrieval_revisions")
    if invalid_retrieval_contexts:
        collection_blockers.append("retrieval_context_missing_or_invalid")
    if len(retrieval_contexts) > 1:
        collection_blockers.append("mixed_retrieval_contexts")
    if invalid_decision_contexts:
        collection_blockers.append("decision_context_missing_or_invalid")
    if len(decision_contexts) > 1:
        collection_blockers.append("mixed_decision_contexts")
    if len(parser_revisions) != 1:
        collection_blockers.append("parser_revision_not_singular")
    if len(unique_cases) < AUTOMATED_SHADOW_POLICY["minUniqueExplicitReferenceCases"]:
        collection_blockers.append("insufficient_explicit_reference_cases")
    if explicit_recall is None:
        collection_blockers.append("explicit_reference_recall_missing")
    elif explicit_recall < AUTOMATED_SHADOW_POLICY["minExplicitReferenceRecallAt3"]:
        collection_blockers.append("explicit_reference_recall_below_minimum")
    if explicit_top1 is None:
        collection_blockers.append("explicit_reference_top1_missing")
    elif explicit_top1 < AUTOMATED_SHADOW_POLICY["minExplicitReferenceTop1Accuracy"]:
        collection_blockers.append("explicit_reference_top1_below_minimum")
    if degraded > AUTOMATED_SHADOW_POLICY["maxDegradedObservations"]:
        collection_blockers.append("degraded_observation")
    if latency["p95"] is None:
        collection_blockers.append("candidate_latency_missing")
    elif latency["p95"] > AUTOMATED_SHADOW_POLICY["maxCandidateP95LatencyMs"]:
        collection_blockers.append("candidate_latency_exceeded")
    if conflicts > AUTOMATED_SHADOW_POLICY["maxConflictingExplicitReferenceCases"]:
        collection_blockers.append("conflicting_explicit_reference_cases")
    if legacy_selection_affected > AUTOMATED_SHADOW_POLICY["maxLegacySelectionAffected"]:
        collection_blockers.append("legacy_selection_affected")
    if automatic_promotion_requested > AUTOMATED_SHADOW_POLICY["maxAutomaticPromotionRequested"]:
        collection_blockers.append("automatic_promotion_requested")

    if not capability_decisions:
        collection_status = "no-data"
    elif collection_blockers:
        collection_status = "blocked"
    else:
        collection_status = "ready-for-automated-shadow-review"
    policy_revision = canonical_revision(AUTOMATED_SHADOW_POLICY)
    payload = {
        "schema": AUTOMATED_SHADOW_EVIDENCE_SCHEMA,
        "collectionStatus": collection_status,
        "promotionStatus": "blocked",
        "authority": "none",
        "autoPromote": False,
        "scope": "prospective-explicit-skill-reference-only",
        "provesIndependence": False,
        "provesQuality": False,
        "provesSemanticOwnership": False,
        "provesSemanticAbstention": False,
        "policyRevision": policy_revision,
        "policy": AUTOMATED_SHADOW_POLICY,
        "observed": {
            "capabilityDecisions": len(capability_decisions),
            "validRoutingObservations": len(valid_pairs),
            "invalidRoutingObservations": invalid_observations,
            "validObjectiveSignals": len(valid_signals),
            "invalidObjectiveSignals": invalid_signals + invalid_labeled_hashes,
            "uniqueExplicitReferenceCases": len(unique_cases),
            "duplicateExplicitReferenceCases": duplicates,
            "conflictingExplicitReferenceCases": conflicts,
            "expectedReferences": expected_references,
            "explicitReferenceHitsAt3": reference_hits,
            "explicitReferenceRecallAt3": explicit_recall,
            "explicitReferenceTop1Accuracy": explicit_top1,
            "retrievalRevisionCount": len(retrieval_revisions),
            "retrievalRevisions": retrieval_revisions,
            "invalidRetrievalRevisions": invalid_retrieval_revisions,
            "retrievalContextCount": len(retrieval_contexts),
            "retrievalContexts": [
                {
                    "algorithm": algorithm,
                    "implementationRevision": implementation_revision,
                    "indexRevision": index_revision,
                }
                for algorithm, implementation_revision, index_revision in retrieval_contexts
            ],
            "invalidRetrievalContexts": invalid_retrieval_contexts,
            "decisionContextCount": len(decision_contexts),
            "decisionContextRevision": canonical_revision(decision_contexts),
            "invalidDecisionContexts": invalid_decision_contexts,
            "configRevisions": sorted({str(event["configRevision"]) for event in valid_context_events}),
            "catalogRevisions": sorted({str(event["catalogRevision"]) for event in valid_context_events}),
            "runtimeRevisions": sorted({str(event["runtimeRevision"]) for event in valid_context_events}),
            "policyContextRevision": canonical_revision(
                sorted({str(event["policyVersion"]) for event in valid_context_events})
            ),
            "parserRevisions": parser_revisions,
            "degradedObservations": degraded,
            "candidateLatencyMs": latency,
            "legacySelectionAffected": legacy_selection_affected,
            "automaticPromotionRequested": automatic_promotion_requested,
        },
        "collectionBlockers": collection_blockers,
        "promotionBlockers": list(AUTOMATED_PROMOTION_BLOCKERS),
    }
    payload["revision"] = canonical_revision(payload)
    return payload


def build_measurement_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    observed_events = len(events)
    accepted_events = [event for event in events if is_measurement_event(event)]
    ignored_events = observed_events - len(accepted_events)
    decisions = [event for event in accepted_events if event.get("eventType") == "decision"]
    completions = [event for event in accepted_events if event.get("eventType") == "completion"]
    outcomes = [event for event in accepted_events if event.get("eventType") == "outcome"]
    policy_feedback = [event for event in accepted_events if event.get("eventType") == "policy-feedback"]
    usable_outcomes, outcome_quality = normalize_outcomes(outcomes)
    decision_turns = {key for event in decisions if (key := lifecycle_key(event)) is not None}
    completion_turns = {key for event in completions if (key := lifecycle_key(event)) is not None}
    correlated = len(decision_turns & completion_turns)
    by_mode = Counter(str(event["mode"]) for event in decisions if isinstance(event.get("mode"), str))
    by_route = Counter(str(event.get("route")) for event in decisions if isinstance(event.get("route"), str))
    by_activation = Counter(
        str(event["activationDisposition"])
        for event in decisions
        if event.get("activationDisposition") in {"activate", "propose", "abstain"}
    )
    matched = sum(event.get("decisionStatus") == "matched" for event in decisions)
    no_match = sum(event.get("decisionStatus") == "no-match" for event in decisions)
    shadow_only = sum(event.get("decisionStatus") == "shadow-match" for event in decisions)
    shadowed = sum(
        event.get("mode") == "shadow" or event.get("decisionStatus") == "shadow-match" for event in decisions
    )
    injected = sum(event.get("injected") is True for event in decisions)
    activation_coverage = sum(by_activation.values())
    decision_contexts = context_segments(decisions, DECISION_CONTEXT_FIELDS)
    comparable_outcomes = [event for event in outcomes if outcome_identity(event) is not None]
    outcome_contexts = context_segments(comparable_outcomes, OUTCOME_CONTEXT_FIELDS)
    mixed_decision_contexts = len(decision_contexts) > 1
    mixed_outcome_contexts = len(outcome_contexts) > 1
    unversioned_outcomes = any(
        any(value is None for value in context_key(event, OUTCOME_CONTEXT_FIELDS)) for event in comparable_outcomes
    )
    outcome_aggregate_comparable = (
        bool(comparable_outcomes)
        and not mixed_outcome_contexts
        and not unversioned_outcomes
        and outcome_quality["conflicts"] == 0
        and outcome_quality["invalid"] == 0
        and ignored_events == 0
    )
    routing_observations = routing_observation_summary(decisions)
    warnings = []
    if ignored_events:
        warnings.append("ignored-events")
    if mixed_decision_contexts:
        warnings.append("mixed-decision-contexts")
    if mixed_outcome_contexts:
        warnings.append("mixed-outcome-contexts")
    if unversioned_outcomes:
        warnings.append("unversioned-outcomes")
    if outcome_quality["duplicates"]:
        warnings.append("duplicate-outcomes")
    if outcome_quality["conflicts"]:
        warnings.append("conflicting-outcomes")
    if outcome_quality["invalid"]:
        warnings.append("invalid-outcomes")
    if routing_observations["invalid"]:
        warnings.append("invalid-routing-observations")

    outcome_report = {**outcome_quality, **outcome_summary(usable_outcomes)}

    return {
        "schema": MEASUREMENT_REPORT_SCHEMA,
        "observedEvents": observed_events,
        "events": len(accepted_events),
        "ignoredEvents": ignored_events,
        "decisions": {
            "total": len(decisions),
            "matched": matched,
            "noMatch": no_match,
            "injected": injected,
            "shadowed": shadowed,
            "shadowOnly": shadow_only,
            "abstentionRate": rate(no_match + shadow_only, len(decisions)),
            "injectionRate": rate(injected, len(decisions)),
            "activated": by_activation["activate"],
            "proposed": by_activation["propose"],
            "activationAbstained": by_activation["abstain"],
            "activationDecisionCoverage": rate(activation_coverage, len(decisions)),
            "activationRate": rate(by_activation["activate"], activation_coverage),
            "proposalRate": rate(by_activation["propose"], activation_coverage),
            "byActivationDisposition": dict(sorted(by_activation.items())),
            "latencyMs": latency_summary(decisions),
            "byMode": dict(sorted(by_mode.items())),
            "byRoute": dict(sorted(by_route.items())),
        },
        "completions": {
            "total": len(completions),
            "decisionTurns": len(decision_turns),
            "uniqueTurns": len(completion_turns),
            "correlatedTurns": correlated,
            "uncorrelatableDecisions": sum(lifecycle_key(event) is None for event in decisions),
            "uncorrelatableCompletions": sum(lifecycle_key(event) is None for event in completions),
            "completionRate": rate(correlated, len(decision_turns)),
        },
        "routingObservations": routing_observations,
        "outcomes": outcome_report,
        "policyFeedback": {
            "total": len(policy_feedback),
            "byVerdict": dict(
                sorted(
                    Counter(
                        str(event["verdict"]) for event in policy_feedback if isinstance(event.get("verdict"), str)
                    ).items()
                )
            ),
            "byRoute": dict(
                sorted(
                    Counter(
                        str(event["route"]) for event in policy_feedback if isinstance(event.get("route"), str)
                    ).items()
                )
            ),
        },
        "pairedNativeInject": paired_native_inject(usable_outcomes),
        "comparability": {
            "decisionContexts": decision_contexts,
            "outcomeContexts": outcome_contexts,
            "mixedDecisionContexts": mixed_decision_contexts,
            "mixedOutcomeContexts": mixed_outcome_contexts,
            "unversionedOutcomes": unversioned_outcomes,
            "outcomeAggregateComparable": outcome_aggregate_comparable,
        },
        "warnings": warnings,
    }


def outcome_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router outcome",
        description="Append a pseudonymous objective, human, or grader outcome label.",
    )
    parser.add_argument(
        "--config",
        help="Routes config used to record revision context and resolve the default measurement log path.",
    )
    parser.add_argument("--log", help="Measurement JSONL path. Overrides the config path.")
    parser.add_argument("--case-id", help="Stable experiment case identifier; stored only as a hash.")
    parser.add_argument("--replicate", type=int, default=1, help="Positive replicate number for paired comparisons.")
    parser.add_argument("--arm", required=True, choices=OUTCOME_ARMS)
    parser.add_argument("--status", required=True, choices=OUTCOME_STATUSES)
    parser.add_argument("--source", required=True, choices=OUTCOME_SOURCES)
    parser.add_argument(
        "--session-id",
        help="Codex session id; stored only as a hash and required with --turn-id when --case-id is omitted.",
    )
    parser.add_argument(
        "--turn-id",
        help="Codex turn id; stored only as a hash and required with --session-id when --case-id is omitted.",
    )
    args = parser.parse_args(argv)
    if not args.case_id and not (args.session_id and args.turn_id):
        parser.error("--case-id or both --session-id and --turn-id are required")
    if args.replicate <= 0:
        parser.error("--replicate must be a positive integer")

    config = load_measurement_config(args.config)
    path = measurement_log_path(config, Path(args.log) if args.log else None)
    versioned_config = args.config is not None
    event = {
        "eventType": "outcome",
        "caseHash": hash_identifier(args.case_id, "case"),
        "replicate": args.replicate,
        "arm": args.arm,
        "status": args.status,
        "source": args.source,
        "sessionHash": hash_identifier(args.session_id, "session"),
        "turnHash": hash_identifier(args.turn_id, "turn"),
        "policyVersion": policy_version(config) if versioned_config else None,
        "configRevision": config_revision(config) if versioned_config else None,
    }
    if not append_measurement_event(event, config, explicit_path=path, force=True):
        print(f"ERROR: failed to append outcome to {path}", file=sys.stderr)
        return 1
    print(f"Recorded {args.arm} outcome: {args.status}")
    return 0


def print_text_report(report: dict[str, Any]) -> None:
    decisions = report["decisions"]
    completions = report["completions"]
    outcomes = report["outcomes"]
    paired = report["pairedNativeInject"]
    policy_feedback = report["policyFeedback"]
    comparability = report["comparability"]
    observations = report["routingObservations"]
    latency = decisions["latencyMs"]
    print("lazy-skill-router measurement report")
    print(
        f"Events: {report['events']} accepted / {report['observedEvents']} observed (ignored {report['ignoredEvents']})"
    )
    print(
        f"Decisions: {decisions['total']} "
        f"(matched {decisions['matched']}, no-match {decisions['noMatch']}, injected {decisions['injected']}, "
        f"abstention rate {decisions['abstentionRate']}, injection rate {decisions['injectionRate']})"
    )
    print(
        f"Activation: activate {decisions['activated']}, propose {decisions['proposed']}, "
        f"abstain {decisions['activationAbstained']} "
        f"(coverage {decisions['activationDecisionCoverage']}, activation rate {decisions['activationRate']})"
    )
    print(
        f"Decision latency ms: mean {latency['mean']}, p95 {latency['p95']}, "
        f"max {latency['max']} (n {latency['count']})"
    )
    print(
        f"Completions: {completions['correlatedTurns']}/{completions['decisionTurns']} correlated "
        f"(rate {completions['completionRate']})"
    )
    print(
        f"Routing observations: {observations['total']} "
        f"(coverage {observations['decisionCoverage']}, ownership {observations['byOwnershipStatus']}, "
        f"stop actions {observations['byStopAction']})"
    )
    print(
        f"Outcomes: {outcomes['usable']}/{outcomes['total']} usable "
        f"(duplicates {outcomes['duplicates']}, conflicts {outcomes['conflicts']}, invalid {outcomes['invalid']})"
    )
    for arm, summary in outcomes["byArm"].items():
        print(
            f"- {arm}: pass {summary['passed']}, fail {summary['failed']}, unknown {summary['unknown']} "
            f"(success rate {summary['successRate']})"
        )
    print(
        f"Native/inject pairs: {paired['pairs']} "
        f"(rescues {paired['rescues']}, harms {paired['harms']}, net win {paired['netWin']})"
    )
    print(
        f"Policy feedback: {policy_feedback['total']} "
        f"(by verdict {policy_feedback['byVerdict']}, by route {policy_feedback['byRoute']})"
    )
    print(
        f"Comparable outcome aggregate: {str(comparability['outcomeAggregateComparable']).lower()} "
        f"(decision contexts {len(comparability['decisionContexts'])}, "
        f"outcome contexts {len(comparability['outcomeContexts'])})"
    )
    if report["warnings"]:
        print("Warnings: " + ", ".join(report["warnings"]))


def report_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router report",
        description="Summarize accumulated routing, completion, and outcome events.",
    )
    parser.add_argument("--config", help="Routes config used to resolve the default measurement log path.")
    parser.add_argument("--log", help="Measurement JSONL path. Overrides the config path.")
    parser.add_argument("--json", action="store_true", help="Print the versioned report as JSON.")
    args = parser.parse_args(argv)
    config = load_measurement_config(args.config)
    path = measurement_log_path(config, Path(args.log) if args.log else None)
    report = build_measurement_report(read_measurement_events(path))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
    return 0


def print_text_shadow_evidence(evidence: dict[str, Any]) -> None:
    observed = evidence["observed"]
    print("lazy-skill-router automated shadow evidence")
    print(f"Collection status: {evidence['collectionStatus']}")
    print(f"Promotion status: {evidence['promotionStatus']} (authority={evidence['authority']})")
    print(
        f"Routing observations: {observed['validRoutingObservations']} valid, "
        f"{observed['invalidRoutingObservations']} invalid"
    )
    print(
        f"Explicit references: {observed['uniqueExplicitReferenceCases']} unique cases, "
        f"Recall@3 {observed['explicitReferenceRecallAt3']}, "
        f"Top-1 {observed['explicitReferenceTop1Accuracy']}"
    )
    if evidence["collectionBlockers"]:
        print("Collection blockers: " + ", ".join(evidence["collectionBlockers"]))
    print("Promotion blockers: " + ", ".join(evidence["promotionBlockers"]))


def shadow_evidence_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router shadow-evidence",
        description="Build authority-free automated evidence from prospective redacted shadow observations.",
    )
    parser.add_argument("--config", help="Routes config used to resolve the default measurement log path.")
    parser.add_argument("--log", help="Measurement JSONL path. Overrides the config path.")
    parser.add_argument("--json", action="store_true", help="Print AutomatedShadowEvidenceV1 as JSON.")
    parser.add_argument("--output", help="Write AutomatedShadowEvidenceV1 to this path.")
    args = parser.parse_args(argv)
    config = load_measurement_config(args.config)
    path = measurement_log_path(config, Path(args.log) if args.log else None)
    evidence = build_automated_shadow_evidence(read_measurement_events(path))
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    if args.json:
        print(encoded, end="")
    elif not args.output:
        print_text_shadow_evidence(evidence)
    return 0
