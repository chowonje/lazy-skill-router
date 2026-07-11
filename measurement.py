from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from lazy_skill_router_core import load_config
from lazy_skill_router_logging import (
    append_measurement_event,
    config_revision,
    hash_identifier,
    is_measurement_event,
    measurement_log_path,
    policy_version,
    read_measurement_events,
)

MEASUREMENT_REPORT_SCHEMA = "lazy-skill-router.measurement-report/v1"
OUTCOME_ARMS = ("inject", "native", "oracle", "shadow")
OUTCOME_STATUSES = ("fail", "pass", "unknown")
OUTCOME_SOURCES = ("grader", "human", "objective")
DECISION_CONTEXT_FIELDS = ("policyVersion", "configRevision", "catalogRevision", "runtimeRevision")
OUTCOME_CONTEXT_FIELDS = ("policyVersion", "configRevision")


def load_measurement_config(config_path: str | None) -> dict[str, Any]:
    if config_path is None:
        return {}
    return load_config(Path(__file__).resolve(), config_path)


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


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
    matched = sum(event.get("decisionStatus") == "matched" for event in decisions)
    no_match = sum(event.get("decisionStatus") == "no-match" for event in decisions)
    shadow_only = sum(event.get("decisionStatus") == "shadow-match" for event in decisions)
    shadowed = sum(
        event.get("mode") == "shadow" or event.get("decisionStatus") == "shadow-match" for event in decisions
    )
    injected = sum(event.get("injected") is True for event in decisions)
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
        f"Decision latency ms: mean {latency['mean']}, p95 {latency['p95']}, "
        f"max {latency['max']} (n {latency['count']})"
    )
    print(
        f"Completions: {completions['correlatedTurns']}/{completions['decisionTurns']} correlated "
        f"(rate {completions['completionRate']})"
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
