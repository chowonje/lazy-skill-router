from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final

import doctor
import install
import measurement
import uninstall
from lazy_skill_router_contracts import hook_ir_v1, route_result_v2, structured_recommendation_v1
from lazy_skill_router_core import dry_run_output, load_config
from lazy_skill_router_inventory import inventory_for_config

COMMANDS: Final = ("install", "doctor", "uninstall", "route", "outcome", "report")
DATA_ROOT_NAME: Final = "lazy-skill-router"
PACKAGE_NAME: Final = "lazy-skill-router"
UNKNOWN_VERSION: Final = "0.0.0"


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def installed_data_root() -> Path:
    return Path(sys.prefix) / "share" / DATA_ROOT_NAME


def source_version() -> str:
    project_file = source_root() / "pyproject.toml"
    if not project_file.is_file():
        return UNKNOWN_VERSION

    in_project_section = False
    for line in project_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project_section = True
            continue
        if stripped.startswith("["):
            in_project_section = False
            continue
        if in_project_section and stripped.startswith("version = "):
            return stripped.split("=", 1)[1].strip().strip('"')
    return UNKNOWN_VERSION


def package_version() -> str:
    source = source_version()
    if source != UNKNOWN_VERSION:
        return source
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def resource_root() -> Path:
    candidate = installed_data_root()
    if (candidate / "lazy_skill_router.py").is_file() and (candidate / "routes.template.json").is_file():
        return candidate
    return source_root()


def configure_install_sources(root: Path) -> None:
    install.HOOK_SOURCE = root / "lazy_skill_router.py"
    install.CORE_SOURCE = root / "lazy_skill_router_core.py"
    install.COMMON_SOURCE = root / "lazy_skill_router_common.py"
    install.LOGGING_SOURCE = root / "lazy_skill_router_logging.py"
    install.SCORING_SOURCE = root / "lazy_skill_router_scoring.py"
    install.CONTRACTS_SOURCE = root / "lazy_skill_router_contracts.py"
    install.INVENTORY_SOURCE = root / "lazy_skill_router_inventory.py"
    install.SKILL_SOURCE = root / "skills" / "personal-skill-router"
    install.TEMPLATE_SOURCE = root / "routes.template.json"


def print_help() -> None:
    print("usage: lazy-skill-router <command> [options]")
    print()
    print("Commands:")
    for command in COMMANDS:
        print(f"  {command}")


def print_version() -> None:
    print(f"lazy-skill-router {package_version()}")


def route_prompt(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router route",
        description="Show which skill route would be recommended for a prompt.",
    )
    parser.add_argument("--config", help="Path to a routes JSON file.")
    parser.add_argument("--inventory", help="Path to a generated skill inventory manifest.")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print legacy route diagnostics as JSON.")
    output_group.add_argument(
        "--route-result-v2",
        action="store_true",
        help="Print the experimental route-result v2 shadow contract.",
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
    parser.add_argument("prompt", help="Prompt text to route.")
    parsed = parser.parse_args(args)

    config = load_config(resource_root() / "lazy_skill_router.py", parsed.config)
    if parsed.hook_ir_json:
        inventory = inventory_for_config(config, parsed.inventory)
        print(json.dumps(hook_ir_v1(parsed.prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    if parsed.recommendation_json:
        inventory = inventory_for_config(config, parsed.inventory)
        print(json.dumps(structured_recommendation_v1(parsed.prompt, config, inventory), ensure_ascii=False, indent=2))
        return 0

    if parsed.route_result_v2:
        print(json.dumps(route_result_v2(parsed.prompt, config), ensure_ascii=False, indent=2))
        return 0

    result = dry_run_output(parsed.prompt, config)
    if parsed.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not result["shouldInject"]:
        print("No route")
        print(f"Reason: {result['reason']}")
        print(f"Answer-only: {str(result['answerOnly']).lower()}")
        return 0

    supporting = result["supporting"] if isinstance(result["supporting"], list) else []
    supporting_text = ", ".join(str(skill) for skill in supporting) if supporting else "none"
    verification = result["verification"] or "none"
    signals = result["matchedSignals"] if isinstance(result["matchedSignals"], list) else []
    signals_text = ", ".join(str(signal) for signal in signals) if signals else "none"

    print(f"Route: {result['route']}")
    print(f"Primary skill: {result['primary']}")
    print(f"Supporting skills: {supporting_text}")
    print(f"Verification skill: {verification}")
    print(f"Confidence: {result['confidence']:.2f} ({result['confidenceLabel']})")
    print(f"Selection score: {result['score']:.2f}")
    print(f"Matched signals: {signals_text}")
    print(f"Answer-only: {str(result['answerOnly']).lower()}")
    return 0


def run_main(main_func: Callable[[], int], command: str, args: list[str]) -> int:
    previous = sys.argv
    sys.argv = [f"lazy-skill-router {command}", *args]
    try:
        return main_func()
    finally:
        sys.argv = previous


def run_command(command: str, args: list[str]) -> int:
    if command == "install":
        configure_install_sources(resource_root())
        return run_main(install.main, command, args)
    if command == "doctor":
        return run_main(doctor.main, command, args)
    if command == "uninstall":
        return run_main(uninstall.main, command, args)
    if command == "route":
        return route_prompt(args)
    if command == "outcome":
        return measurement.outcome_main(args)
    if command == "report":
        return measurement.report_main(args)
    raise ValueError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print_help()
        return 0
    if args[0] == "--version":
        print_version()
        return 0
    command = args[0]
    if command not in COMMANDS:
        print(f"ERROR: unknown command: {command}", file=sys.stderr)
        print_help()
        return 2
    return run_command(command, args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
