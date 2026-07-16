from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from eval_router_ab import FROZEN_FIELDS, parse_frozen
from lazy_skill_router_common import _open_confined_parent, confined_read_regular_snapshot
from lazy_skill_router_retrieval import SUPPORTED_RETRIEVAL_ALGORITHMS
from measurement import canonical_revision, latency_summary, rate, valid_sha256_revision

ROW_SCHEMA: Final = "lazy-skill-router.external-user-holdout-row/v1"
REPORT_SCHEMA: Final = "lazy-skill-router.external-user-holdout-report/v1"
MAX_ARTIFACT_BYTES: Final = 16 * 1024 * 1024
MAX_ROWS: Final = 10_000
MAX_ROW_BYTES: Final = 4 * 1024
MAX_TIME_TO_CORRECT_START_MS: Final = 3_600_000
MIN_FINAL_PARTICIPANTS: Final = 3
MAX_FINAL_PARTICIPANTS: Final = 5
METRIC_NAMES: Final = (
    "recommendation-appropriateness",
    "time-to-correct-start-ms",
    "recommendation-authority-understanding",
)
REPORT_METRIC_FIELDS: Final = (
    "recommendationAppropriateness",
    "timeToCorrectStartMs",
    "authorityDistinctionUnderstanding",
)

STUDY_FIELDS: Final = frozenset(
    {
        "schema",
        "recordType",
        "studyId",
        "protocolRevision",
        "frozen",
        "metrics",
        "precommitRequired",
        "rawPromptStored",
        "retuningAllowed",
        "authority",
        "autoPromote",
    }
)
EXPECTATION_FIELDS: Final = frozenset(
    {
        "schema",
        "recordType",
        "studyId",
        "planRevision",
        "participantId",
        "caseId",
        "expectedDisposition",
        "expectedSkillToken",
        "rawPromptStored",
    }
)
OBSERVATION_FIELDS: Final = frozenset(
    {
        "schema",
        "recordType",
        "studyId",
        "planRevision",
        "participantId",
        "caseId",
        "expectationRevision",
        "runStatus",
        "routerDisposition",
        "recommendedSkillToken",
        "fitVerdict",
        "timeToCorrectStartMs",
        "authorityAnswer",
        "rawPromptStored",
    }
)
ROUTER_RESULT_FIELDS: Final = frozenset({"runStatus", "routerDisposition", "recommendedSkillToken"})
OBSERVATION_INPUT_FIELDS: Final = frozenset({"fitVerdict", "timeToCorrectStartMs", "authorityAnswer"})
STUDY_ID_RE: Final = re.compile(r"^study-[0-9a-f]{16}$")
PARTICIPANT_ID_RE: Final = re.compile(r"^participant-[0-9a-f]{16}$")
CASE_ID_RE: Final = re.compile(r"^case-[0-9a-f]{16}$")
SKILL_TOKEN_RE: Final = re.compile(r"^skill-[0-9a-f]{16}$")


def _require_exact_fields(value: Any, expected: frozenset[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ValueError(f"{location} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")
    return value


def _require_token(value: Any, pattern: re.Pattern[str], location: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{location} must be an opaque random token")
    return value


def _require_revision(value: Any, location: str) -> str:
    if not valid_sha256_revision(value):
        raise ValueError(f"{location} must be a sha256 revision")
    return str(value)


def _validate_study(row: Any) -> dict[str, Any]:
    value = _require_exact_fields(row, STUDY_FIELDS, "study")
    if value["schema"] != ROW_SCHEMA or value["recordType"] != "study":
        raise ValueError("study schema or recordType is invalid")
    _require_token(value["studyId"], STUDY_ID_RE, "study.studyId")
    _require_revision(value["protocolRevision"], "study.protocolRevision")
    frozen = _require_exact_fields(value["frozen"], FROZEN_FIELDS, "study.frozen")
    parse_frozen(frozen)
    if isinstance(frozen["maxCandidates"], bool) or not isinstance(frozen["maxCandidates"], int):
        raise ValueError("study.frozen.maxCandidates must be an integer")
    for field in ("configRevision", "inventoryRevision", "indexRevision", "experimentCodeRevision"):
        _require_revision(frozen[field], f"study.frozen.{field}")
    if frozen["retrievalAlgorithm"] not in SUPPORTED_RETRIEVAL_ALGORITHMS:
        raise ValueError("study.frozen.retrievalAlgorithm is unsupported")
    if value["metrics"] != list(METRIC_NAMES):
        raise ValueError("study.metrics must contain exactly the three holdout metrics")
    if value["precommitRequired"] is not True:
        raise ValueError("study.precommitRequired must be true")
    if value["rawPromptStored"] is not False:
        raise ValueError("study.rawPromptStored must be false")
    if value["retuningAllowed"] is not False:
        raise ValueError("study.retuningAllowed must be false")
    if value["authority"] != "none" or value["autoPromote"] is not False:
        raise ValueError("study cannot grant authority or automatic promotion")
    return value


def _validate_expected_skill(disposition: Any, skill_token: Any, location: str) -> None:
    if disposition == "skill":
        _require_token(skill_token, SKILL_TOKEN_RE, f"{location}SkillToken")
    elif disposition == "abstain":
        if skill_token is not None:
            raise ValueError(f"{location}SkillToken must be null for abstain")
    else:
        raise ValueError(f"{location}Disposition is unsupported")


def _validate_expectation(row: Any) -> dict[str, Any]:
    value = _require_exact_fields(row, EXPECTATION_FIELDS, "expectation")
    if value["schema"] != ROW_SCHEMA or value["recordType"] != "expectation":
        raise ValueError("expectation schema or recordType is invalid")
    _require_token(value["studyId"], STUDY_ID_RE, "expectation.studyId")
    _require_revision(value["planRevision"], "expectation.planRevision")
    _require_token(value["participantId"], PARTICIPANT_ID_RE, "expectation.participantId")
    _require_token(value["caseId"], CASE_ID_RE, "expectation.caseId")
    _validate_expected_skill(value["expectedDisposition"], value["expectedSkillToken"], "expected")
    if value["rawPromptStored"] is not False:
        raise ValueError("expectation.rawPromptStored must be false")
    return value


def _valid_duration(value: Any) -> bool:
    return value is None or (
        not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= MAX_TIME_TO_CORRECT_START_MS
    )


def _validate_observation(row: Any) -> dict[str, Any]:
    value = _require_exact_fields(row, OBSERVATION_FIELDS, "observation")
    if value["schema"] != ROW_SCHEMA or value["recordType"] != "observation":
        raise ValueError("observation schema or recordType is invalid")
    _require_token(value["studyId"], STUDY_ID_RE, "observation.studyId")
    _require_revision(value["planRevision"], "observation.planRevision")
    _require_token(value["participantId"], PARTICIPANT_ID_RE, "observation.participantId")
    _require_token(value["caseId"], CASE_ID_RE, "observation.caseId")
    _require_revision(value["expectationRevision"], "observation.expectationRevision")
    if value["authorityAnswer"] not in {"recommendation-only", "authorizes-or-executes", "unsure"}:
        raise ValueError("observation.authorityAnswer is unsupported")
    if not _valid_duration(value["timeToCorrectStartMs"]):
        raise ValueError("observation.timeToCorrectStartMs is invalid")
    if value["rawPromptStored"] is not False:
        raise ValueError("observation.rawPromptStored must be false")

    if value["runStatus"] == "ok":
        _validate_expected_skill(value["routerDisposition"], value["recommendedSkillToken"], "recommended")
        if value["fitVerdict"] not in {"appropriate", "not-appropriate"}:
            raise ValueError("successful observation fitVerdict is invalid")
    elif value["runStatus"] == "operational-failure":
        if (
            value["routerDisposition"] != "unavailable"
            or value["recommendedSkillToken"] is not None
            or value["fitVerdict"] != "not-observable"
            or value["timeToCorrectStartMs"] is not None
        ):
            raise ValueError("operational failure observation fields are inconsistent")
    else:
        raise ValueError("observation.runStatus is unsupported")
    return value


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON numeric constant: {value}")


def _parse_json_line(line: str, line_number: int) -> dict[str, Any]:
    if not line.strip():
        raise ValueError(f"line {line_number} is blank")
    if len(line.encode("utf-8")) > MAX_ROW_BYTES:
        raise ValueError(f"line {line_number} exceeds the row size limit")
    try:
        value = json.loads(
            line,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"line {line_number} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"line {line_number} must be a JSON object")
    return value


def _safe_absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise ValueError("holdout artifact path cannot contain parent traversal")
    return expanded.absolute()


def _validate_rows(
    rows: list[dict[str, Any]],
    source_revision: str,
    *,
    require_complete: bool,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("holdout JSONL must not be empty")
    study = _validate_study(rows[0])
    plan_revision = canonical_revision(study)
    expectations: dict[tuple[str, str], dict[str, Any]] = {}
    observations: dict[tuple[str, str], dict[str, Any]] = {}

    for offset, row in enumerate(rows[1:], start=2):
        record_type = row.get("recordType") if isinstance(row, dict) else None
        if record_type == "study":
            raise ValueError(f"line {offset} contains a second study record")
        if record_type == "expectation":
            value = _validate_expectation(row)
            key = (value["participantId"], value["caseId"])
            if key in expectations:
                raise ValueError(f"line {offset} duplicates an expectation")
            if value["studyId"] != study["studyId"] or value["planRevision"] != plan_revision:
                raise ValueError(f"line {offset} does not match the frozen study")
            expectations[key] = value
            continue
        if record_type == "observation":
            value = _validate_observation(row)
            key = (value["participantId"], value["caseId"])
            expectation = expectations.get(key)
            if expectation is None:
                raise ValueError(f"line {offset} appears before its expectation")
            if key in observations:
                raise ValueError(f"line {offset} duplicates an observation")
            if value["studyId"] != study["studyId"] or value["planRevision"] != plan_revision:
                raise ValueError(f"line {offset} does not match the frozen study")
            if value["expectationRevision"] != canonical_revision(expectation):
                raise ValueError(f"line {offset} does not bind the recorded expectation")
            observations[key] = value
            continue
        raise ValueError(f"line {offset} has an unsupported recordType")

    incomplete = len(set(expectations) - set(observations))
    participant_count = len({participant_id for participant_id, _ in expectations})
    participant_gate_satisfied = MIN_FINAL_PARTICIPANTS <= participant_count <= MAX_FINAL_PARTICIPANTS
    if require_complete and (incomplete or not observations or not participant_gate_satisfied):
        raise ValueError("final holdout requires complete cases from three to five unique participants")
    return {
        "study": study,
        "planRevision": plan_revision,
        "sourceRevision": source_revision,
        "expectations": expectations,
        "observations": observations,
        "incompleteCases": incomplete,
        "participantCount": participant_count,
        "participantGateSatisfied": participant_gate_satisfied,
        "rows": len(rows),
    }


def load_holdout(path: Path, *, require_complete: bool) -> dict[str, Any]:
    source = _safe_absolute_path(path)
    try:
        content, identity = confined_read_regular_snapshot(source, source.parent, MAX_ARTIFACT_BYTES)
    except (OSError, ValueError) as exc:
        raise ValueError("holdout artifact is unavailable or unsafe") from exc
    if content is None:
        raise ValueError("holdout artifact exceeds the size limit")
    if not content:
        raise ValueError("holdout artifact must not be empty")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("holdout artifact must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) > MAX_ROWS:
        raise ValueError("holdout artifact has too many rows")
    rows = [_parse_json_line(line, index) for index, line in enumerate(lines, start=1)]
    source_revision = identity.digest
    if not valid_sha256_revision(source_revision):
        raise ValueError("holdout artifact snapshot revision is unavailable")
    return _validate_rows(rows, str(source_revision), require_complete=require_complete)


def _append_row(path: Path, row: dict[str, Any], *, exclusive: bool = False) -> None:
    destination = _safe_absolute_path(path)
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_ROW_BYTES:
        raise ValueError("holdout row exceeds the size limit")
    parent_fd, name, _ = _open_confined_parent(destination, destination.parent, create_parents=False)
    initial: os.stat_result | None = None
    descriptor = -1
    final: os.stat_result | None = None
    verification_parent_fd = -1
    try:
        try:
            initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not exclusive:
                raise ValueError("holdout journal is missing") from None
        else:
            if exclusive:
                raise ValueError("holdout journal already exists")
            if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or stat.S_IMODE(initial.st_mode) != 0o600:
                raise ValueError("existing holdout journal must be a private non-hardlinked regular file")

        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if exclusive:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("opened holdout journal must be a private non-hardlinked regular file")
        if initial is not None and (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mode,
            opened.st_nlink,
        ) != (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mode,
            initial.st_nlink,
        ):
            raise ValueError("holdout journal changed while opening")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mode, current.st_nlink) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mode,
            opened.st_nlink,
        ):
            raise ValueError("holdout journal path changed before append")
        if opened.st_size + len(encoded_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError("holdout artifact exceeds the size limit")

        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            final = os.fstat(handle.fileno())
        os.fsync(parent_fd)

        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if final is None or (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or stat.S_IMODE(final.st_mode) != 0o600
            or final.st_size != opened.st_size + len(encoded_bytes)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mode, current.st_nlink)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mode, final.st_nlink)
        ):
            raise ValueError("holdout journal changed during append")

        verification_parent_fd, verification_name, _ = _open_confined_parent(
            destination,
            destination.parent,
            create_parents=False,
        )
        verification_parent = os.fstat(verification_parent_fd)
        original_parent = os.fstat(parent_fd)
        verified = os.stat(verification_name, dir_fd=verification_parent_fd, follow_symlinks=False)
        if (
            verification_name != name
            or (verification_parent.st_dev, verification_parent.st_ino)
            != (original_parent.st_dev, original_parent.st_ino)
            or (verified.st_dev, verified.st_ino, verified.st_size, verified.st_mode, verified.st_nlink)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mode, final.st_nlink)
        ):
            raise ValueError("holdout journal path changed after append")
    finally:
        if verification_parent_fd >= 0:
            os.close(verification_parent_fd)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def initialize_study(path: Path, study: dict[str, Any]) -> None:
    _validate_study(study)
    _append_row(path, study, exclusive=True)


def collect_case(
    path: Path,
    *,
    participant_id: str,
    case_id: str,
    expected_disposition: str,
    expected_skill_token: str | None,
    router_callback: Callable[[], dict[str, Any]],
    observation_input: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = load_holdout(path, require_complete=False)
    if current["rows"] > MAX_ROWS - 2:
        raise ValueError("holdout artifact has no room for another complete case")
    key = (participant_id, case_id)
    if key in current["expectations"]:
        raise ValueError("holdout case already has an expectation")
    expectation = {
        "schema": ROW_SCHEMA,
        "recordType": "expectation",
        "studyId": current["study"]["studyId"],
        "planRevision": current["planRevision"],
        "participantId": participant_id,
        "caseId": case_id,
        "expectedDisposition": expected_disposition,
        "expectedSkillToken": expected_skill_token,
        "rawPromptStored": False,
    }
    _validate_expectation(expectation)

    # The callback may reveal the router result, so the precommit must be durable first.
    _append_row(path, expectation)
    router_result = _require_exact_fields(router_callback(), ROUTER_RESULT_FIELDS, "router result")
    input_result = _require_exact_fields(
        observation_input(dict(router_result)), OBSERVATION_INPUT_FIELDS, "observation input"
    )
    observation = {
        "schema": ROW_SCHEMA,
        "recordType": "observation",
        "studyId": current["study"]["studyId"],
        "planRevision": current["planRevision"],
        "participantId": participant_id,
        "caseId": case_id,
        "expectationRevision": canonical_revision(expectation),
        **router_result,
        **input_result,
        "rawPromptStored": False,
    }
    _validate_observation(observation)
    latest = load_holdout(path, require_complete=False)
    if latest["expectations"].get(key) != expectation or key in latest["observations"]:
        raise ValueError("holdout journal changed during collection")
    _append_row(path, observation)
    return expectation, observation


def build_report(validated: dict[str, Any]) -> dict[str, Any]:
    study = validated["study"]
    expectations = list(validated["expectations"].values())
    observations = list(validated["observations"].values())
    successful = [row for row in observations if row["runStatus"] == "ok"]
    fit_eligible = [row for row in successful if row["fitVerdict"] != "not-observable"]
    appropriate = sum(row["fitVerdict"] == "appropriate" for row in fit_eligible)
    duration_events = [
        {"latencyMs": row["timeToCorrectStartMs"]} for row in successful if row["timeToCorrectStartMs"] is not None
    ]
    durations = latency_summary(duration_events)
    understood = sum(row["authorityAnswer"] == "recommendation-only" for row in observations)
    complete = bool(observations) and validated["incompleteCases"] == 0 and validated["participantGateSatisfied"]
    report = {
        "schema": REPORT_SCHEMA,
        "scope": "external-user-usability-only",
        "collectionStatus": "complete" if complete else "incomplete",
        "sourceRevision": validated["sourceRevision"],
        "planRevision": validated["planRevision"],
        "protocolRevision": study["protocolRevision"],
        "frozenRevision": canonical_revision(study["frozen"]),
        "observed": {
            "rows": validated["rows"],
            "participants": len({row["participantId"] for row in expectations}),
            "cases": len(expectations),
            "observations": len(observations),
            "incompleteCases": validated["incompleteCases"],
            "operationalFailures": sum(row["runStatus"] == "operational-failure" for row in observations),
            "participantGate": {
                "minimum": MIN_FINAL_PARTICIPANTS,
                "maximum": MAX_FINAL_PARTICIPANTS,
                "satisfied": validated["participantGateSatisfied"],
            },
        },
        "metrics": {
            "recommendationAppropriateness": {
                "source": "participant-self-report",
                "eligible": len(fit_eligible),
                "appropriate": appropriate,
                "notAppropriate": len(fit_eligible) - appropriate,
                "rate": rate(appropriate, len(fit_eligible)),
            },
            "timeToCorrectStartMs": {
                "source": "recommendation-visible-to-correct-start-confirmed",
                "eligible": len(successful),
                "observed": durations["count"],
                "notStarted": len(successful) - durations["count"],
                "mean": durations["mean"],
                "p95": durations["p95"],
                "max": durations["max"],
            },
            "authorityDistinctionUnderstanding": {
                "source": "fixed-choice-question",
                "eligible": len(observations),
                "understood": understood,
                "notUnderstood": len(observations) - understood,
                "rate": rate(understood, len(observations)),
            },
        },
        "privacy": {
            "rawPromptStored": False,
            "directPersonalIdentifiersStored": False,
            "randomPseudonymsStored": True,
            "absolutePathsStored": False,
            "aggregateIdentifiersEmitted": False,
        },
        "evidenceBoundary": {
            "promotionStatus": "blocked",
            "authority": "none",
            "autoPromote": False,
            "provesIndependence": False,
            "provesQuality": False,
            "eligibleForPromotionEvidence": False,
            "retuningAllowed": False,
            "promotionBlockers": [
                "independence_not_verified",
                "independent_adjudication_not_collected",
                "ownership_activation_outcome_linkage_unavailable",
                "usability_metrics_do_not_replace_promotion_metrics",
            ],
        },
    }
    report["reportRevision"] = canonical_revision(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or report a promptless external-user holdout JSONL file.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("input", type=Path)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        validated = load_holdout(args.input, require_complete=False)
        complete = (
            bool(validated["observations"])
            and validated["incompleteCases"] == 0
            and validated["participantGateSatisfied"]
        )
        if args.command == "report":
            print(json.dumps(build_report(validated), ensure_ascii=False, indent=2))
        else:
            print("OK: external-user holdout is complete" if complete else "BLOCKED: holdout is incomplete")
        return 0 if complete else 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
