from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from lazy_skill_router_common import codex_home, debug

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows is unsupported
    fcntl = None  # type: ignore[assignment]

DEFAULT_MAX_ENTRIES = 1000
DEFAULT_RETENTION_DAYS = 30
MAX_MAX_ENTRIES = 10000
MAX_RETENTION_DAYS = 365
MEASUREMENT_EVENT_SCHEMA = "lazy-skill-router.measurement-event/v1"
MEASUREMENT_EVENT_TYPES = frozenset({"completion", "decision", "outcome", "policy-feedback"})
ROUTING_OBSERVATION_SCHEMA = "lazy-skill-router.routing-observation/v1"
AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA = "lazy-skill-router.automated-objective-signal/v1"
AUTOMATED_OBJECTIVE_PARSER_REVISION = "deterministic-explicit-skill-reference/v2"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}$")


class RouteLike(Protocol):
    name: str
    primary: str


class RouteMatchLike(Protocol):
    route: RouteLike
    confidence: float
    score: float
    matched_signals: tuple[str, ...]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def hash_identifier(value: Any, namespace: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    payload = f"lazy-skill-router:{namespace}:{value}".encode()
    return hashlib.sha256(payload).hexdigest()[:20]


def config_revision(config: dict[str, Any]) -> str:
    public_config = {key: value for key, value in config.items() if not key.startswith("_")}
    canonical = json.dumps(public_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def policy_version(config: dict[str, Any]) -> str:
    value = config.get("policyVersion")
    if isinstance(value, str) and value:
        return value
    legacy = config.get("version", 1)
    if isinstance(legacy, bool) or not isinstance(legacy, (str, int, float)):
        legacy = 1
    return f"route-v1:{legacy}"


def configured_positive_int(value: Any, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return min(value, maximum)


def record_time(record: dict[str, Any]) -> dt.datetime | None:
    value = record.get("time")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_measurement_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        debug(f"failed to read measurement log: {exc}")
        return []

    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def is_measurement_event(event: dict[str, Any]) -> bool:
    return event.get("schema") == MEASUREMENT_EVENT_SCHEMA and event.get("eventType") in MEASUREMENT_EVENT_TYPES


def existing_records(path: Path, cutoff: dt.datetime) -> list[dict[str, Any]]:
    records = []
    for record in read_measurement_events(path):
        timestamp = record_time(record)
        if timestamp is not None and timestamp >= cutoff:
            records.append(record)
    return records


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temp_path, path)


@contextmanager
def log_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def logging_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("logging")
    return value if isinstance(value, dict) else {}


def measurement_log_path(config: dict[str, Any], explicit_path: Path | None = None) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser()
    configured_path = logging_config(config).get("path")
    if isinstance(configured_path, str) and configured_path:
        return Path(configured_path).expanduser()
    loaded_from = config.get("_loaded_from")
    if isinstance(loaded_from, str):
        route_path = Path(loaded_from)
        if route_path.name == "routes.json" and route_path.parent.name == "lazy-skill-router":
            return route_path.parent.parent / "logs" / "lazy_skill_router.jsonl"
    return codex_home() / "logs" / "lazy_skill_router.jsonl"


def append_measurement_event(
    event: dict[str, Any],
    config: dict[str, Any],
    *,
    explicit_path: Path | None = None,
    force: bool = False,
) -> bool:
    config_value = logging_config(config)
    if not force and config_value.get("enabled") is not True:
        return False

    path = measurement_log_path(config, explicit_path)
    max_entries = configured_positive_int(
        config_value.get("maxEntries"),
        DEFAULT_MAX_ENTRIES,
        MAX_MAX_ENTRIES,
    )
    retention_days = configured_positive_int(
        config_value.get("retentionDays"),
        DEFAULT_RETENTION_DAYS,
        MAX_RETENTION_DAYS,
    )
    record = {
        **event,
        "schema": MEASUREMENT_EVENT_SCHEMA,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with log_lock(path):
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
            records = existing_records(path, cutoff)
            records.append(record)
            write_records(path, records[-max_entries:])
    except OSError as exc:
        debug(f"failed to write measurement log: {exc}")
        return False
    return True


def event_identity(hook_event: dict[str, Any] | None) -> dict[str, str | None]:
    event = hook_event or {}
    return {
        "sessionHash": hash_identifier(event.get("session_id"), "session"),
        "turnHash": hash_identifier(event.get("turn_id"), "turn"),
    }


def candidate_route_ids(candidates: Iterable[RouteMatchLike]) -> list[str]:
    return [candidate.route.name for candidate in candidates]


def candidate_proposal_revisions(candidates: Iterable[RouteMatchLike]) -> dict[str, str]:
    revisions = {}
    for candidate in candidates:
        revision = getattr(candidate.route, "proposal_revision", None)
        if isinstance(revision, str) and revision:
            revisions[candidate.route.name] = revision
    return revisions


def bounded_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        return None
    return value


def bounded_identifiers(values: Any, maximum: int) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    identifiers: list[str] = []
    for value in values:
        if (identifier := bounded_identifier(value)) is not None and identifier not in identifiers:
            identifiers.append(identifier)
        if len(identifiers) >= maximum:
            break
    return identifiers


def routing_observation_v1(
    *,
    retrieval_status: str,
    retrieval_revision: str | None,
    candidate_observations: Iterable[dict[str, Any]],
    retrieval_latency_ms: float | None,
    retrieval_reason_codes: Iterable[str],
    legacy_primary: str | None,
    activation_disposition: str | None,
    injected: bool,
    legacy_selection_observed: bool,
) -> dict[str, Any]:
    candidates = []
    for candidate in candidate_observations:
        if len(candidates) >= 3:
            break
        if not isinstance(candidate, dict):
            continue
        skill_id = bounded_identifier(candidate.get("skillId"))
        if skill_id is None:
            continue
        candidates.append(
            {
                "skillId": skill_id,
                "evidenceIds": bounded_identifiers(candidate.get("evidenceIds"), 8),
            }
        )

    usable_retrieval = retrieval_status in {"matched", "no-match"}
    if usable_retrieval:
        stop_action = "observe-only"
        stop_reason = (
            "lexical_no_match_not_semantic_abstain" if retrieval_status == "no-match" else "ownership_unobserved"
        )
    elif legacy_selection_observed:
        stop_action = "fallback-legacy"
        stop_reason = "retrieval_unusable"
    else:
        stop_action = "stop-shadow"
        stop_reason = "retrieval_unusable_no_legacy_selection"

    return {
        "schema": ROUTING_OBSERVATION_SCHEMA,
        "lane": "capability-retrieval",
        "mode": "shadow",
        "retrieval": {
            "revision": bounded_identifier(retrieval_revision),
            "status": bounded_identifier(retrieval_status) or "degraded",
            "candidates": candidates,
            "latencyMs": (round(max(0.0, retrieval_latency_ms), 3) if retrieval_latency_ms is not None else None),
            "reasonCodes": bounded_identifiers(tuple(retrieval_reason_codes), 8),
        },
        "ownership": {
            "status": "unobserved",
            "primarySkillId": None,
            "reasonCode": "host_ownership_observation_unavailable",
        },
        "activation": {
            "source": (
                "legacy-route-plus-activation-ir"
                if activation_disposition in {"activate", "propose", "abstain"}
                else "unobserved"
            ),
            "disposition": (
                activation_disposition if activation_disposition in {"activate", "propose", "abstain"} else None
            ),
            "legacyPrimarySkillId": bounded_identifier(legacy_primary),
            "injected": injected,
        },
        "stop": {
            "action": stop_action,
            "reasonCode": stop_reason,
            "affectsLegacySelection": False,
        },
        "semantics": {
            "rawPromptStored": False,
            "semanticAbstentionObserved": False,
            "disagreementIsFallbackEvidence": False,
            "automaticPromotion": False,
        },
    }


def automated_objective_signal_v1(expected_skill_ids: Iterable[str]) -> dict[str, Any]:
    expected = bounded_identifiers(tuple(expected_skill_ids), 3)
    return {
        "schema": AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA,
        "kind": "explicit-skill-reference" if expected else "unlabelled",
        "expectedSkillIds": expected,
        "source": "local-deterministic-parser",
        "parserRevision": AUTOMATED_OBJECTIVE_PARSER_REVISION,
        "reasonCode": "deterministic_exact_reference" if expected else "no_exact_reference",
        "rawPromptStored": False,
    }


def log_decision(
    prompt: str,
    match: RouteMatchLike | None,
    config: dict[str, Any],
    *,
    hook_event: dict[str, Any] | None = None,
    mode: str = "direct",
    injected: bool = False,
    candidates: Iterable[RouteMatchLike] = (),
    shadow_candidates: Iterable[RouteMatchLike] = (),
    shadow_would_win: Iterable[str] = (),
    decision_status: str | None = None,
    activation_disposition: str | None = None,
    activation_reason: str | None = None,
    latency_ms: float | None = None,
    catalog_revision: str | None = None,
    runtime_revision: str | None = None,
    capability_index_revision: str | None = None,
    capability_candidate_skill_ids: Iterable[str] = (),
    capability_candidate_observations: Iterable[dict[str, Any]] = (),
    capability_retrieval_latency_ms: float | None = None,
    capability_retrieval_status: str | None = None,
    capability_retrieval_algorithm: str | None = None,
    capability_retrieval_implementation_revision: str | None = None,
    capability_retrieval_reason_codes: Iterable[str] = (),
    retrieval_top1: str | None = None,
    automated_expected_skill_ids: Iterable[str] = (),
) -> None:
    shadow_candidates = tuple(shadow_candidates)
    matched_pattern_ids = list(getattr(match, "matched_pattern_ids", ())) if match is not None else []
    candidate_ids = candidate_route_ids(candidates)
    shadow_candidate_ids = candidate_route_ids(shadow_candidates)
    compiler = config.get("policyCompiler")
    proposal_revision = compiler.get("proposalRevision") if isinstance(compiler, dict) else None
    if not candidate_ids and match is not None:
        candidate_ids = [match.route.name]
    event = {
        "eventType": "decision",
        **event_identity(hook_event),
        "source": "hook" if hook_event is not None else "direct",
        "promptHash": prompt_hash(prompt),
        "mode": mode,
        "decisionStatus": decision_status or ("matched" if match is not None else "no-match"),
        "activationDisposition": activation_disposition,
        "activationReason": activation_reason,
        "shouldInject": match is not None and activation_disposition != "abstain",
        "shouldActivate": activation_disposition == "activate",
        "injected": injected,
        "route": match.route.name if match else None,
        "primary": match.route.primary if match else None,
        "confidence": match.confidence if match else 0.0,
        "matchStrength": match.confidence if match else 0.0,
        "score": match.score if match else 0.0,
        "candidateRouteIds": candidate_ids,
        "shadowCandidateRouteIds": shadow_candidate_ids,
        "shadowCandidateProposalRevisions": candidate_proposal_revisions(shadow_candidates),
        "shadowWouldWinRouteIds": list(shadow_would_win),
        "proposalRevision": proposal_revision if isinstance(proposal_revision, str) else None,
        "matchedPatternIds": matched_pattern_ids,
        "policyVersion": policy_version(config),
        "configRevision": config_revision(config),
        "catalogRevision": catalog_revision,
        "runtimeRevision": runtime_revision,
        "latencyMs": round(max(0.0, latency_ms), 3) if latency_ms is not None else None,
    }
    if capability_retrieval_status is not None:
        event.update(
            {
                "retrievalRevision": capability_index_revision,
                "retrievalStatus": capability_retrieval_status,
                "retrievalAlgorithm": bounded_identifier(capability_retrieval_algorithm),
                "retrievalImplementationRevision": bounded_identifier(capability_retrieval_implementation_revision),
                "capabilityCandidateSkillIds": list(capability_candidate_skill_ids)[:3],
                "capabilityRetrievalLatencyMs": (
                    round(max(0.0, capability_retrieval_latency_ms), 3)
                    if capability_retrieval_latency_ms is not None
                    else None
                ),
                "capabilityRetrievalReasonCodes": list(capability_retrieval_reason_codes)[:8],
                "legacyPrimary": match.route.primary if match is not None else None,
                "retrievalTop1": retrieval_top1,
                "automatedObjectiveSignal": automated_objective_signal_v1(automated_expected_skill_ids),
                "routingObservation": routing_observation_v1(
                    retrieval_status=capability_retrieval_status,
                    retrieval_revision=capability_index_revision,
                    candidate_observations=capability_candidate_observations,
                    retrieval_latency_ms=capability_retrieval_latency_ms,
                    retrieval_reason_codes=capability_retrieval_reason_codes,
                    legacy_primary=match.route.primary if match is not None else None,
                    activation_disposition=activation_disposition,
                    injected=injected,
                    legacy_selection_observed=mode != "off",
                ),
            }
        )
    append_measurement_event(event, config)


def log_completion(
    hook_event: dict[str, Any],
    config: dict[str, Any],
    *,
    runtime_revision: str | None = None,
) -> None:
    append_measurement_event(
        {
            "eventType": "completion",
            **event_identity(hook_event),
            "source": "hook",
            "stopHookActive": hook_event.get("stop_hook_active") is True,
            "policyVersion": policy_version(config),
            "configRevision": config_revision(config),
            "runtimeRevision": runtime_revision,
        },
        config,
    )
