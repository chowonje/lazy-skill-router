from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lazy_skill_router_core import (
    answer_only_patterns,
    dry_run_output,
    format_context,
    load_config,
    log_decision,
    route_match,
    show_router_notice,
    text_matches,
)


def read_event_from_stdin() -> dict[str, object] | None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def prompt_from_args(prompt_option: str | None, prompt_text: str | None) -> str | None:
    if prompt_option is not None:
        return prompt_option
    if prompt_text is not None:
        return prompt_text
    event = read_event_from_stdin()
    if event is None:
        return None
    prompt = event.get("prompt")
    return prompt if isinstance(prompt, str) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject Codex skill-routing context from a prompt hook event.")
    parser.add_argument("--config", help="Path to a routes JSON file.")
    parser.add_argument("--prompt", help="Route this prompt directly instead of reading hook JSON from stdin.")
    parser.add_argument("--dry-run", action="store_true", help="Print routing diagnostics instead of hook output.")
    parser.add_argument("prompt_text", nargs="?", help="Prompt text for --dry-run.")
    args = parser.parse_args(argv)

    config = load_config(Path(__file__).resolve(), args.config)
    prompt = prompt_from_args(args.prompt, args.prompt_text)
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    if args.dry_run:
        print(json.dumps(dry_run_output(prompt, config), ensure_ascii=False, indent=2))
        return 0

    match = route_match(prompt, config)
    log_decision(prompt, match, config)
    if match is None:
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
