from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from install import (
    InstallError,
    install_hook_command,
    install_stop_hook_command,
    lazy_router_hook_items,
    smoke_config_for_prompt,
    smoke_hook,
    smoke_stop_hook,
)
from lazy_skill_router_common import codex_home, load_hooks, load_json_object, write_json
from lazy_skill_router_install_manifest import artifact_state, load_install_manifest
from lazy_skill_router_inventory import load_inventory_manifest
from sync_skills import build_report, scan_installed_skills
from validate_routes import validate_config

HOOK_FILES = (
    "lazy_skill_router.py",
    "lazy_skill_router_core.py",
    "lazy_skill_router_common.py",
    "lazy_skill_router_contracts.py",
    "lazy_skill_router_inventory.py",
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


def check_hook_registration(
    hooks_path: Path,
    expected_command: str,
    event_name: str = "UserPromptSubmit",
) -> CheckResult:
    try:
        hooks = load_hooks(hooks_path)
    except (OSError, ValueError) as exc:
        return fail(f"{event_name} hook registered: cannot read {hooks_path}: {exc}")

    items = lazy_router_hook_items(hooks, event_name)
    if not items:
        return fail(f"{event_name} hook registered: missing lazy-skill-router entry")
    if len(items) > 1:
        return fail(f"{event_name} hook registered: multiple lazy-skill-router entries found")
    item = items[0]
    if item.get("command") != expected_command:
        return fail(f"{event_name} hook registered with a different command")
    return ok(f"{event_name} hook registered")


def check_stop_registration(
    hooks_path: Path,
    expected_command: str,
    route_config: dict[str, Any] | None,
) -> CheckResult:
    logging_config = route_config.get("logging") if isinstance(route_config, dict) else None
    enabled = isinstance(logging_config, dict) and logging_config.get("enabled") is True
    if enabled:
        return check_hook_registration(hooks_path, expected_command, "Stop")
    try:
        hooks = load_hooks(hooks_path)
    except (OSError, ValueError) as exc:
        return fail(f"Stop hook registration matches measurement setting: cannot read {hooks_path}: {exc}")
    if lazy_router_hook_items(hooks, "Stop"):
        return warn("Stop hook registered while measurement logging is disabled")
    return ok("Stop hook not required while measurement logging is disabled")


def check_smoke(hook_path: Path, route_path: Path, explicit_prompt: str | None) -> CheckResult:
    if not hook_path.is_file() or not route_path.is_file():
        return fail("hook smoke test passed: hook or routes file missing")
    try:
        with tempfile.TemporaryDirectory(prefix="lazy-skill-router-doctor-") as temp_dir:
            smoke_route = Path(temp_dir) / "routes.json"
            config = load_json_object(route_path, "routes root")
            smoke_config, prompt = smoke_config_for_prompt(config, explicit_prompt)
            write_json(smoke_route, smoke_config)
            smoke_hook(hook_path, smoke_route, prompt)
            smoke_stop_hook(hook_path, smoke_route)
    except (InstallError, OSError, ValueError) as exc:
        return fail(f"hook smoke test passed: {exc}")
    return ok("hook smoke test passed")


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


def check_inventory_manifest(path: Path) -> CheckResult:
    snapshot = load_inventory_manifest(path)
    if snapshot.state == "available":
        return ok(f"skill inventory manifest validates: {snapshot.revision}")
    reason = ", ".join(snapshot.reason_codes) if snapshot.reason_codes else snapshot.state
    return fail(f"skill inventory manifest validates: {reason}")


def check_install_manifest(path: Path, codex_root: Path) -> CheckResult:
    snapshot = load_install_manifest(path)
    if snapshot.state != "available":
        reason = ", ".join(snapshot.reason_codes) if snapshot.reason_codes else snapshot.state
        return fail(f"install ownership manifest validates: {reason}")

    managed_drift = []
    generated_drift = []
    for artifact in snapshot.artifacts:
        ownership = artifact.get("ownership")
        if ownership == "preserved":
            continue
        state = artifact_state(codex_root, artifact)
        if state == "matching":
            continue
        detail = f"{artifact.get('path')} ({state})"
        if ownership == "managed":
            managed_drift.append(detail)
        else:
            generated_drift.append(detail)
    if managed_drift:
        return fail("install ownership manifest validates: managed artifact drift: " + ", ".join(managed_drift))
    if generated_drift:
        return warn("install ownership manifest validates with generated config drift: " + ", ".join(generated_drift))
    return ok(f"install ownership manifest validates: {snapshot.revision}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a lazy-skill-router installation without editing files.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument("--routes", help="Routes JSON. Defaults to $CODEX_HOME/lazy-skill-router/routes.json.")
    parser.add_argument(
        "--inventory",
        help="Skill inventory manifest. Defaults to $CODEX_HOME/lazy-skill-router/skills.manifest.json.",
    )
    parser.add_argument(
        "--smoke-prompt",
        default=None,
        help=("Explicit prompt used for the hook smoke test. When omitted, a temporary internal probe route is used."),
    )
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    hook_path = codex_root / "hooks" / "lazy_skill_router.py"
    route_path = Path(args.routes).expanduser() if args.routes else codex_root / "lazy-skill-router" / "routes.json"
    inventory_path = (
        Path(args.inventory).expanduser()
        if args.inventory
        else codex_root / "lazy-skill-router" / "skills.manifest.json"
    )
    install_manifest_path = codex_root / "lazy-skill-router" / "install.manifest.json"
    hooks_path = codex_root / "hooks.json"
    expected_command = install_hook_command(hook_path, route_path)
    expected_stop_command = install_stop_hook_command(hook_path, route_path)

    route_config, route_checks = load_routes_config(route_path)
    main_hook, core_hooks = check_hook_files(codex_root)
    checks = (
        check_codex_home(codex_root),
        main_hook,
        core_hooks,
        *route_checks,
        check_inventory_manifest(inventory_path),
        check_install_manifest(install_manifest_path, codex_root),
        check_hook_registration(hooks_path, expected_command),
        check_stop_registration(hooks_path, expected_stop_command, route_config),
        check_smoke(hook_path, route_path, args.smoke_prompt),
        check_skill_sync(route_config, codex_root, agents_root),
    )

    print("lazy-skill-router doctor")
    for check in checks:
        print(format_check(check))
    return 1 if any(check.status is CheckStatus.FAIL for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
