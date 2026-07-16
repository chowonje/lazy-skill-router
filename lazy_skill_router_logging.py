from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from lazy_skill_router_common import _open_confined_parent, codex_home, confined_ensure_managed_root, debug

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


def _regular_file_fingerprint(metadata: os.stat_result, label: str) -> tuple[int, int, int, int, int, int, int]:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise OSError(f"{label} must not be hard-linked")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_log_parent(path: Path, *, create: bool) -> tuple[int, str]:
    absolute = path.absolute()
    if create:
        confined_ensure_managed_root(absolute.parent)
    parent_fd, name, _ = _open_confined_parent(
        absolute,
        absolute.parent,
        create_parents=False,
    )
    metadata = os.fstat(parent_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(parent_fd)
        raise OSError("measurement log parent must be a directory")
    return parent_fd, name


def _file_fingerprint_at(parent_fd: int, name: str, label: str) -> tuple[int, int, int, int, int, int, int] | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _regular_file_fingerprint(metadata, label)


def _read_measurement_events_at(
    parent_fd: int,
    name: str,
) -> tuple[list[dict[str, Any]], tuple[int, int, int, int, int, int, int] | None]:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        initial = _regular_file_fingerprint(os.fstat(descriptor), "measurement log")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            lines = handle.read().splitlines()
            final = _regular_file_fingerprint(os.fstat(handle.fileno()), "measurement log")
    except FileNotFoundError:
        return [], None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if final != initial or _file_fingerprint_at(parent_fd, name, "measurement log") != initial:
        raise OSError("measurement log changed while reading")

    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, initial


def read_measurement_events(path: Path) -> list[dict[str, Any]]:
    parent_fd = -1
    try:
        parent_fd, name = _open_log_parent(path, create=False)
        events, _ = _read_measurement_events_at(parent_fd, name)
        return events
    except (OSError, ValueError) as exc:
        debug(f"failed to read measurement log: {exc}")
        return []
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def is_measurement_event(event: dict[str, Any]) -> bool:
    return event.get("schema") == MEASUREMENT_EVENT_SCHEMA and event.get("eventType") in MEASUREMENT_EVENT_TYPES


def existing_records(path: Path, cutoff: dt.datetime) -> list[dict[str, Any]]:
    records = []
    for record in read_measurement_events(path):
        timestamp = record_time(record)
        if timestamp is not None and timestamp >= cutoff:
            records.append(record)
    return records


def _existing_records_at(
    parent_fd: int, name: str, cutoff: dt.datetime
) -> tuple[
    list[dict[str, Any]],
    tuple[int, int, int, int, int, int, int] | None,
]:
    events, identity = _read_measurement_events_at(parent_fd, name)
    records = []
    for record in events:
        timestamp = record_time(record)
        if timestamp is not None and timestamp >= cutoff:
            records.append(record)
    return records, identity


def _create_temp_file(parent_fd: int, name: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(32):
        temp_name = f".{name}.{secrets.token_hex(12)}.tmp"
        try:
            return os.open(temp_name, flags, 0o600, dir_fd=parent_fd), temp_name
        except FileExistsError:
            continue
    raise OSError("could not allocate an exclusive measurement temp file")


def _cleanup_owned_temp(parent_fd: int, temp_name: str, identity: tuple[int, int]) -> None:
    try:
        current = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        os.unlink(temp_name, dir_fd=parent_fd)


def _write_records_at(
    parent_fd: int,
    name: str,
    records: list[dict[str, Any]],
    expected: tuple[int, int, int, int, int, int, int] | None,
) -> None:
    descriptor, temp_name = _create_temp_file(parent_fd, name)
    created = os.fstat(descriptor)
    created_identity = (created.st_dev, created.st_ino)
    try:
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged = _regular_file_fingerprint(os.fstat(descriptor), "measurement temp file")
        if _file_fingerprint_at(parent_fd, temp_name, "measurement temp file") != staged:
            raise OSError("measurement temp file changed before replacement")
        if _file_fingerprint_at(parent_fd, name, "measurement log") != expected:
            raise OSError("measurement log changed before replacement")
        os.replace(
            temp_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            _cleanup_owned_temp(parent_fd, temp_name, created_identity)
        finally:
            os.close(descriptor)


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    parent_fd, name = _open_log_parent(path, create=False)
    try:
        expected = _file_fingerprint_at(parent_fd, name, "measurement log")
        _write_records_at(parent_fd, name, records, expected)
    finally:
        os.close(parent_fd)


def ensure_private_log_directory(path: Path) -> None:
    parent_fd, _ = _open_log_parent(path, create=True)
    os.close(parent_fd)


def _open_log_lock(parent_fd: int, lock_name: str) -> int:
    base_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(
            lock_name,
            base_flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(lock_name, base_flags, dir_fd=parent_fd)
        created = False
    try:
        initial = _regular_file_fingerprint(os.fstat(descriptor), "measurement lock")
        if created:
            os.fchmod(descriptor, 0o600)
            initial = _regular_file_fingerprint(os.fstat(descriptor), "measurement lock")
        current = _file_fingerprint_at(parent_fd, lock_name, "measurement lock")
        if current is None or current[:2] != initial[:2]:
            raise OSError("measurement lock changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def log_lock(
    path: Path,
    *,
    _parent_fd: int | None = None,
    _journal_name: str | None = None,
) -> Iterator[None]:
    owned_parent_fd = -1
    if _parent_fd is None:
        owned_parent_fd, journal_name = _open_log_parent(path, create=False)
        parent_fd = owned_parent_fd
    else:
        parent_fd = _parent_fd
        journal_name = _journal_name or path.absolute().name
    lock_name = journal_name + ".lock"
    descriptor = -1
    try:
        descriptor = _open_log_lock(parent_fd, lock_name)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if owned_parent_fd >= 0:
            os.close(owned_parent_fd)
        raise
    try:
        with handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if owned_parent_fd >= 0:
            os.close(owned_parent_fd)


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

    path = measurement_log_path(config, explicit_path).absolute()
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
        parent_fd, name = _open_log_parent(path, create=True)
        try:
            with log_lock(path, _parent_fd=parent_fd, _journal_name=name):
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
                records, expected = _existing_records_at(parent_fd, name, cutoff)
                records.append(record)
                _write_records_at(parent_fd, name, records[-max_entries:], expected)
        finally:
            os.close(parent_fd)
    except (OSError, ValueError) as exc:
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
