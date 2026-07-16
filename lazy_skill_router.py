from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from lazy_skill_router_activation import activation_ir_dict
from lazy_skill_router_common import debug
from lazy_skill_router_contracts import hook_ir_v1, route_result_v2, structured_recommendation_v1
from lazy_skill_router_core import (
    activation_for_matches,
    activation_for_prompt,
    activation_mode,
    dry_run_output,
    format_context,
    load_config,
    prompt_is_too_long,
    route_matches_with_shadow_competition,
    show_router_notice,
)
from lazy_skill_router_inventory import InventorySnapshot, inventory_for_config
from lazy_skill_router_logging import log_completion, log_decision

EXPLICIT_SKILL_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._:@/+\-]{0,158}[A-Za-z0-9])?"
EXPLICIT_SKILL_PATTERNS = (
    re.compile(rf"(?<![A-Za-z0-9._:@/+\-])\$(?P<name>{EXPLICIT_SKILL_NAME})(?![A-Za-z0-9._:@/+\-])"),
    re.compile(rf"(?i)(?:skill|스킬)\s+[`$]?(?P<name>{EXPLICIT_SKILL_NAME})`?(?![A-Za-z0-9._:@/+\-])"),
    re.compile(rf"(?i)(?<![A-Za-z0-9._:@/+\-])[`$]?(?P<name>{EXPLICIT_SKILL_NAME})`?\s+(?:skill|스킬)"),
)
CLAUSE_SEGMENT = re.compile(r"[^;\n!?,。\uff01\uff1f\uff0c]+")
SENTENCE_BOUNDARY = re.compile(r"\n|[!?。\uff01\uff1f]|(?<![A-Za-z0-9._:@/+\-])\.|\.(?![A-Za-z0-9._:@/+\-])")
POSITIVE_SKILL_ACTION = re.compile(r"(?i)\b(?:use|apply|run|invoke|activate)\b|사용|적용|실행|써|이용|활용")
NEGATIVE_SKILL_ACTION = re.compile(
    r"(?i)\b(?:do\s+not|don't|dont|never|without|avoid|exclude|instead\s+of|not)\b"
    r"|사용하지|쓰지|제외|없이|말고|금지|아닌|하지\s*마"
)


def read_event_from_stdin() -> dict[str, object] | None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def prompt_and_event(
    prompt_option: str | None,
    prompt_text: str | None,
) -> tuple[str | None, dict[str, object] | None]:
    if prompt_option is not None:
        return prompt_option, None
    if prompt_text is not None:
        return prompt_text, None
    event = read_event_from_stdin()
    if event is None:
        return None, None
    prompt = event.get("prompt")
    return (prompt if isinstance(prompt, str) else None), event


def runtime_revision(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def deterministic_explicit_skill_references(
    prompt: str,
    inventory: InventorySnapshot | None,
) -> tuple[str, ...]:
    if inventory is None or inventory.state != "available":
        return ()
    configured_names: dict[str, list[str]] = {}
    for skill in inventory.skills:
        configured_name = skill.get("configured_name")
        if isinstance(configured_name, str):
            configured_names.setdefault(configured_name.casefold(), []).append(configured_name)
    sentence_ranges: list[tuple[int, int, bool]] = []
    sentence_start = 0
    for boundary in SENTENCE_BOUNDARY.finditer(prompt):
        sentence_ranges.append((sentence_start, boundary.start(), boundary.group(0) in {"?", "\uff1f"}))
        sentence_start = boundary.end()
    sentence_ranges.append((sentence_start, len(prompt), False))

    earliest_matches: dict[str, int] = {}
    for sentence_start, sentence_end, is_question in sentence_ranges:
        if is_question:
            continue
        inherited_action: str | None = None
        for clause_match in CLAUSE_SEGMENT.finditer(prompt, sentence_start, sentence_end):
            clause = clause_match.group(0)
            negative_action = NEGATIVE_SKILL_ACTION.search(clause)
            positive_action = POSITIVE_SKILL_ACTION.search(clause)
            if negative_action is not None:
                inherited_action = "negative"
                continue
            if positive_action is not None:
                inherited_action = "positive"
                action_start = (
                    0
                    if any(ord(character) > 127 for character in positive_action.group(0))
                    else positive_action.start()
                )
            elif inherited_action == "positive":
                action_start = 0
            else:
                continue
            for pattern in EXPLICIT_SKILL_PATTERNS:
                for match in pattern.finditer(clause):
                    if match.start() < action_start:
                        continue
                    candidates = configured_names.get(match.group("name").casefold(), [])
                    configured_name = candidates[0] if len(candidates) == 1 else None
                    if configured_name is not None and inventory.resolve(configured_name) is not None:
                        earliest_matches.setdefault(configured_name, clause_match.start() + match.start())
                        if len(earliest_matches) > 3:
                            return ()
    return tuple(name for name, _ in sorted(earliest_matches.items(), key=lambda item: item[1]))


def automated_reference_measurement(
    prompt: str,
    inventory: InventorySnapshot | None,
    result: dict[str, object] | None,
    latency_ms: float | None,
) -> tuple[tuple[str, ...], float | None]:
    status = result.get("status") if isinstance(result, dict) else None
    if status not in {"matched", "no-match"}:
        return (), latency_ms
    started = time.perf_counter()
    expected = deterministic_explicit_skill_references(prompt, inventory)
    parser_latency_ms = (time.perf_counter() - started) * 1000
    return expected, (latency_ms or 0.0) + parser_latency_ms


def capability_shadow_measurement(
    prompt: str,
    config: dict[str, object],
    inventory: InventorySnapshot | None,
    explicit_index: str | None,
    *,
    legacy_route: str | None = None,
    legacy_primary: str | None = None,
) -> tuple[dict[str, object] | None, float | None]:
    capability_config = config.get("capabilityRetrieval")
    logging_config = config.get("logging")
    if not isinstance(capability_config, dict) or capability_config.get("mode") != "shadow":
        return None, None
    if not isinstance(logging_config, dict) or logging_config.get("enabled") is not True:
        return None, None

    started = time.perf_counter()
    try:
        from lazy_skill_router_retrieval import retrieval_enabled, retrieve_capabilities

        if not retrieval_enabled(config):
            return None, None
        result = retrieve_capabilities(
            prompt,
            config,
            inventory,
            explicit_index=explicit_index,
            legacy_route=legacy_route,
            legacy_primary=legacy_primary,
        )
    except Exception as exc:  # The optional shadow lane must never block legacy routing.
        debug(f"capability retrieval failed open: {type(exc).__name__}")
        result = {
            "status": "degraded",
            "candidates": [],
            "reasonCodes": ["capability_retrieval_failed"],
        }
    return result, (time.perf_counter() - started) * 1000


def capability_log_fields(
    result: dict[str, object] | None,
    latency_ms: float | None,
    automated_expected_skill_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    if result is None:
        return {}
    raw_candidates = result.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    candidate_ids: list[str] = []
    candidate_observations: list[dict[str, object]] = []
    for candidate in candidates[:3]:
        skill_ref = candidate.get("skillRef") if isinstance(candidate, dict) else None
        configured_name = skill_ref.get("configuredName") if isinstance(skill_ref, dict) else None
        if isinstance(configured_name, str):
            candidate_ids.append(configured_name)
            raw_evidence = candidate.get("evidenceIds") if isinstance(candidate, dict) else None
            evidence_ids = tuple(raw_evidence[:8]) if isinstance(raw_evidence, list) else ()
            candidate_observations.append({"skillId": configured_name, "evidenceIds": evidence_ids})
    raw_reasons = result.get("reasonCodes")
    reason_codes = tuple(str(reason) for reason in raw_reasons[:8]) if isinstance(raw_reasons, list) else ()
    revision = result.get("indexRevision")
    algorithm = result.get("algorithm")
    implementation_revision = result.get("implementationRevision")
    status = result.get("status")
    return {
        "capability_index_revision": revision if isinstance(revision, str) else None,
        "capability_retrieval_algorithm": algorithm if isinstance(algorithm, str) else None,
        "capability_retrieval_implementation_revision": (
            implementation_revision if isinstance(implementation_revision, str) else None
        ),
        "capability_candidate_skill_ids": tuple(candidate_ids),
        "capability_candidate_observations": tuple(candidate_observations),
        "capability_retrieval_latency_ms": latency_ms,
        "capability_retrieval_status": status if isinstance(status, str) else "degraded",
        "capability_retrieval_reason_codes": reason_codes,
        "retrieval_top1": candidate_ids[0] if candidate_ids else None,
        "automated_expected_skill_ids": automated_expected_skill_ids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject Codex skill-routing context from a prompt hook event.")
    parser.add_argument("--config", help="Path to a routes JSON file.")
    parser.add_argument("--inventory", help="Path to a generated skill inventory manifest.")
    parser.add_argument("--capability-index", help="Path to a generated capability-index/v1 file.")
    parser.add_argument(
        "--hook-event",
        choices=("prompt", "stop"),
        default="prompt",
        help="Hook lifecycle event handled by this invocation.",
    )
    parser.add_argument("--prompt", help="Route this prompt directly instead of reading hook JSON from stdin.")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print legacy routing diagnostics instead of hook output.",
    )
    output_group.add_argument(
        "--route-result-v2",
        action="store_true",
        help="Print the experimental route-result v2 shadow contract instead of hook output.",
    )
    output_group.add_argument(
        "--recommendation-json",
        action="store_true",
        help="Print the experimental structured recommendation v1 shadow contract.",
    )
    output_group.add_argument(
        "--hook-ir-json",
        action="store_true",
        help="Print the experimental compact Hook IR v1 shadow contract.",
    )
    output_group.add_argument(
        "--activation-ir-json",
        action="store_true",
        help="Print the runtime Activation IR v1 decision contract.",
    )
    output_group.add_argument(
        "--capability-shadow-json",
        action="store_true",
        help="Print the configured capability retrieval shadow diagnostic.",
    )
    parser.add_argument("prompt_text", nargs="?", help="Prompt text for --dry-run.")
    args = parser.parse_args(argv)

    script_path = Path(__file__).resolve()
    config = load_config(script_path, args.config)
    code_revision = runtime_revision(script_path)
    if args.hook_event == "stop":
        event = read_event_from_stdin() or {}
        log_completion(event, config, runtime_revision=code_revision)
        print("{}")
        return 0

    prompt, event = prompt_and_event(args.prompt, args.prompt_text)
    if not isinstance(prompt, str):
        return 0
    input_rejected = prompt_is_too_long(prompt)
    if not input_rejected and not prompt.strip():
        return 0
    inventory = None if input_rejected else inventory_for_config(config, args.inventory)

    if args.capability_shadow_json:
        from lazy_skill_router_retrieval import PRODUCT_PREVIEW_ALGORITHM, retrieve_capabilities

        legacy_matches, _, _ = route_matches_with_shadow_competition(prompt, config, inventory)
        legacy_match = legacy_matches[0] if legacy_matches else None
        result = retrieve_capabilities(
            prompt,
            config,
            inventory,
            explicit_index=args.capability_index,
            force=True,
            legacy_route=legacy_match.route.name if legacy_match is not None else None,
            legacy_primary=legacy_match.route.primary if legacy_match is not None else None,
            algorithm=PRODUCT_PREVIEW_ALGORITHM,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.hook_ir_json:
        print(json.dumps(hook_ir_v1(prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    if args.activation_ir_json:
        activation = activation_for_prompt(prompt, config, inventory)
        print(json.dumps(activation_ir_dict(activation), ensure_ascii=False, indent=2))
        return 0

    if args.recommendation_json:
        print(json.dumps(structured_recommendation_v1(prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    if args.route_result_v2:
        print(json.dumps(route_result_v2(prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        print(json.dumps(dry_run_output(prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    started = time.perf_counter()
    mode = activation_mode(config)
    if input_rejected:
        log_decision(
            prompt,
            None,
            config,
            hook_event=event,
            mode=mode,
            injected=False,
            decision_status="input-rejected",
            latency_ms=(time.perf_counter() - started) * 1000,
            runtime_revision=code_revision,
        )
        return 0
    if mode == "off":
        retrieval_result, retrieval_latency_ms = capability_shadow_measurement(
            prompt,
            config,
            inventory,
            args.capability_index,
        )
        automated_expected_skill_ids, retrieval_latency_ms = automated_reference_measurement(
            prompt,
            inventory,
            retrieval_result,
            retrieval_latency_ms,
        )
        log_decision(
            prompt,
            None,
            config,
            hook_event=event,
            mode=mode,
            decision_status="off",
            latency_ms=(time.perf_counter() - started) * 1000,
            catalog_revision=inventory.revision if inventory is not None else None,
            runtime_revision=code_revision,
            **capability_log_fields(
                retrieval_result,
                retrieval_latency_ms,
                automated_expected_skill_ids,
            ),
        )
        return 0

    matches, shadow_matches, promotion_winners = route_matches_with_shadow_competition(prompt, config, inventory)
    match = matches[0] if matches else None
    activation = activation_for_matches(prompt, matches, config)
    retrieval_result, retrieval_latency_ms = capability_shadow_measurement(
        prompt,
        config,
        inventory,
        args.capability_index,
        legacy_route=match.route.name if match is not None else None,
        legacy_primary=match.route.primary if match is not None else None,
    )
    automated_expected_skill_ids, retrieval_latency_ms = automated_reference_measurement(
        prompt,
        inventory,
        retrieval_result,
        retrieval_latency_ms,
    )
    log_decision(
        prompt,
        match,
        config,
        hook_event=event,
        mode=mode,
        injected=mode == "inject" and activation.disposition != "abstain",
        candidates=matches[:3],
        shadow_candidates=shadow_matches[:3],
        shadow_would_win=promotion_winners,
        activation_disposition=activation.disposition,
        activation_reason=activation.reason_code,
        decision_status=("matched" if match is not None else "shadow-match" if shadow_matches else "no-match"),
        latency_ms=(time.perf_counter() - started) * 1000,
        catalog_revision=inventory.revision if inventory is not None else None,
        runtime_revision=code_revision,
        **capability_log_fields(
            retrieval_result,
            retrieval_latency_ms,
            automated_expected_skill_ids,
        ),
    )
    if activation.disposition == "abstain" or mode != "inject":
        return 0

    context = format_context(
        activation,
        config.get("_loaded_from") if isinstance(config.get("_loaded_from"), str) else None,
        show_router_notice(config),
    )
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
