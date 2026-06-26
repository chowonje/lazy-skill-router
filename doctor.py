from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from install import DEFAULT_SMOKE_PROMPT, InstallError, install_hook_command, lazy_router_hook_item, smoke_hook
from lazy_skill_router_common import codex_home, load_hooks, load_json_object
from sync_skills import build_report, scan_installed_skills
from validate_routes import validate_config

HOOK_FILES = (
    "lazy_skill_router.py",
    "lazy_skill_router_core.py",
    "lazy_skill_router_common.py",
    "lazy_skill_router_logging.py",
    "lazy_skill_router_scoring.py",
)
DUPLICATE_SKILL_PREVIEW_LIMIT = 3
DUPLICATE_PATH_PREVIEW_LIMIT = 2


class CheckStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    message: str


def ok(message: str) -> CheckResult:
    return CheckResult(CheckStatus.OK, message)


def warn(message: str) -> CheckResult:
    return CheckResult(CheckStatus.WARN, message)


def fail(message: str) -> CheckResult:
    return CheckResult(CheckStatus.FAIL, message)


def format_check(check: CheckResult) -> str:
    return f"[{check.status.value}] {check.message}"


def check_codex_home(codex_root: Path) -> CheckResult:
    if codex_root.is_dir():
        return ok(f"Codex home found: {codex_root}")
    return fail(f"Codex home found: missing {codex_root}")


def check_hook_files(codex_root: Path) -> tuple[CheckResult, CheckResult]:
    hook_dir = codex_root / "hooks"
    hook_path = hook_dir / HOOK_FILES[0]
    secondary = tuple(hook_dir / name for name in HOOK_FILES[1:])
    missing_secondary = tuple(path.name for path in secondary if not path.is_file())

    main = (
        ok(f"hook file exists: {hook_path}") if hook_path.is_file() else fail(f"hook file exists: missing {hook_path}")
    )
    if missing_secondary:
        core = fail("core hook files exist: missing " + ", ".join(missing_secondary))
    else:
        core = ok("core hook files exist")
    return main, core


def load_routes_config(route_path: Path) -> tuple[dict[str, Any] | None, tuple[CheckResult, CheckResult]]:
    if not route_path.is_file():
        return None, (fail(f"routes.json exists: missing {route_path}"), fail("routes.json validates: unavailable"))
    try:
        config = load_json_object(route_path, "routes root")
    except (OSError, ValueError) as exc:
        return None, (ok(f"routes.json exists: {route_path}"), fail(f"routes.json validates: {exc}"))

    findings = validate_config(config)
    errors = tuple(finding.message for finding in findings if finding.severity == "ERROR")
    warnings = tuple(finding.message for finding in findings if finding.severity == "WARN")
    if errors:
        return config, (ok(f"routes.json exists: {route_path}"), fail("routes.json validates: " + "; ".join(errors)))
    if warnings:
        return config, (
            ok(f"routes.json exists: {route_path}"),
            warn(f"routes.json validates with {len(warnings)} warnings"),
        )
    return config, (ok(f"routes.json exists: {route_path}"), ok("routes.json validates"))


def check_hook_registration(hooks_path: Path, expected_command: str) -> CheckResult:
    try:
        hooks = load_hooks(hooks_path)
    except (OSError, ValueError) as exc:
        return fail(f"UserPromptSubmit hook registered: cannot read {hooks_path}: {exc}")

    item = lazy_router_hook_item(hooks)
    if item is None:
        return fail("UserPromptSubmit hook registered: missing lazy-skill-router entry")
    if item.get("command") != expected_command:
        return warn("UserPromptSubmit hook registered with a different command")
    return ok("UserPromptSubmit hook registered")


def check_smoke(hook_path: Path, route_path: Path, prompt: str) -> CheckResult:
    if not hook_path.is_file() or not route_path.is_file():
        return fail("hook dry-run smoke test passed: hook or routes file missing")
    try:
        smoke_hook(hook_path, route_path, prompt)
    except (InstallError, OSError) as exc:
        return fail(f"hook dry-run smoke test passed: {exc}")
    return ok("hook dry-run smoke test passed")


def duplicate_skill_word(count: int) -> str:
    return "skill name" if count == 1 else "skill names"


def duplicate_path_preview(paths: tuple[Path, ...]) -> str:
    preview = ", ".join(str(path) for path in paths[:DUPLICATE_PATH_PREVIEW_LIMIT])
    remaining = len(paths) - DUPLICATE_PATH_PREVIEW_LIMIT
    if remaining > 0:
        return f"{preview}, +{remaining} more paths"
    return preview


def duplicate_skill_examples(duplicates: tuple[tuple[str, tuple[Path, ...]], ...]) -> str:
    examples = [
        f"{name} ({len(paths)} copies: {duplicate_path_preview(paths)})"
        for name, paths in duplicates[:DUPLICATE_SKILL_PREVIEW_LIMIT]
    ]
    remaining = len(duplicates) - DUPLICATE_SKILL_PREVIEW_LIMIT
    if remaining > 0:
        examples.append(f"+{remaining} more duplicate {duplicate_skill_word(remaining)}")
    return "; ".join(examples)


def check_skill_sync(config: dict[str, Any] | None, codex_root: Path, agents_root: Path) -> CheckResult:
    if config is None:
        return fail("skill sync checked: routes unavailable")
    report = build_report(config, scan_installed_skills(codex_root, agents_root))
    missing = len(report.allowed_missing) + len(report.route_references_missing)
    if missing:
        return fail(f"skill sync checked: {missing} configured skills missing")
    if report.duplicate_installed:
        duplicate_count = len(report.duplicate_installed)
        return warn(
            f"skill sync checked: {duplicate_count} duplicate {duplicate_skill_word(duplicate_count)}; "
            f"not an install failure; examples: {duplicate_skill_examples(report.duplicate_installed)}; "
            "run sync_skills.py --json for full paths"
        )
    return ok("skill sync checked")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a lazy-skill-router installation without editing files.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument("--routes", help="Routes JSON. Defaults to $CODEX_HOME/lazy-skill-router/routes.json.")
    parser.add_argument("--smoke-prompt", default=DEFAULT_SMOKE_PROMPT, help="Prompt used for the hook smoke test.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    hook_path = codex_root / "hooks" / "lazy_skill_router.py"
    route_path = Path(args.routes).expanduser() if args.routes else codex_root / "lazy-skill-router" / "routes.json"
    hooks_path = codex_root / "hooks.json"
    expected_command = install_hook_command(hook_path, route_path)

    route_config, route_checks = load_routes_config(route_path)
    main_hook, core_hooks = check_hook_files(codex_root)
    checks = (
        check_codex_home(codex_root),
        main_hook,
        core_hooks,
        *route_checks,
        check_hook_registration(hooks_path, expected_command),
        check_smoke(hook_path, route_path, args.smoke_prompt),
        check_skill_sync(route_config, codex_root, agents_root),
    )

    print("lazy-skill-router doctor")
    for check in checks:
        print(format_check(check))
    return 1 if any(check.status is CheckStatus.FAIL for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
