from __future__ import annotations

import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final

import doctor
import install
import uninstall

COMMANDS: Final = ("install", "doctor", "uninstall")
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
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return source_version()


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
