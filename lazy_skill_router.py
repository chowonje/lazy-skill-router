from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from lazy_skill_router_contracts import hook_ir_v1, route_result_v2, structured_recommendation_v1
from lazy_skill_router_core import (
    activation_mode,
    answer_only_patterns,
    dry_run_output,
    format_context,
    load_config,
    route_matches_with_shadow_competition,
    show_router_notice,
    text_matches,
)
from lazy_skill_router_inventory import inventory_for_config
from lazy_skill_router_logging import log_completion, log_decision


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject Codex skill-routing context from a prompt hook event.")
    parser.add_argument("--config", help="Path to a routes JSON file.")
    parser.add_argument("--inventory", help="Path to a generated skill inventory manifest.")
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
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    inventory = inventory_for_config(config, args.inventory)

    if args.hook_ir_json:
        print(json.dumps(hook_ir_v1(prompt, config, inventory), ensure_ascii=False, indent=2))
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
    if mode == "off":
        log_decision(
            prompt,
            None,
            config,
            hook_event=event,
            mode=mode,
            decision_status="off",
            latency_ms=(time.perf_counter() - started) * 1000,
            runtime_revision=code_revision,
        )
        return 0

    matches, shadow_matches, promotion_winners = route_matches_with_shadow_competition(prompt, config, inventory)
    match = matches[0] if matches else None
    log_decision(
        prompt,
        match,
        config,
        hook_event=event,
        mode=mode,
        injected=mode == "inject" and match is not None,
        candidates=matches[:3],
        shadow_candidates=shadow_matches[:3],
        shadow_would_win=promotion_winners,
        decision_status=("matched" if match is not None else "shadow-match" if shadow_matches else "no-match"),
        latency_ms=(time.perf_counter() - started) * 1000,
        catalog_revision=inventory.revision if inventory is not None else None,
        runtime_revision=code_revision,
    )
    if match is None or mode != "inject":
        return 0

    context = format_context(
        match,
        text_matches(prompt, answer_only_patterns(config)),
        config.get("_loaded_from") if isinstance(config.get("_loaded_from"), str) else None,
        show_router_notice(config),
    )
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
