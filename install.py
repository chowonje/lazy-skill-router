from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_routes import TEMPLATE_SOURCE, generate_config, generated_route_count, installed_skill_names
from lazy_skill_router_common import backup_file, codex_home, load_hooks, load_json_object, write_json
from validate_routes import validate_config

PROJECT_ROOT = Path(__file__).resolve().parent
HOOK_SOURCE = PROJECT_ROOT / "lazy_skill_router.py"
CORE_SOURCE = PROJECT_ROOT / "lazy_skill_router_core.py"
COMMON_SOURCE = PROJECT_ROOT / "lazy_skill_router_common.py"
LOGGING_SOURCE = PROJECT_ROOT / "lazy_skill_router_logging.py"
SCORING_SOURCE = PROJECT_ROOT / "lazy_skill_router_scoring.py"
SKILL_SOURCE = PROJECT_ROOT / "skills" / "personal-skill-router"
DEFAULT_SMOKE_PROMPT = "스킬 추천해줘"


@dataclass(frozen=True)
class InstallError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def ensure_user_prompt_hook(data: dict[str, Any], hook_command: str) -> str:
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(groups, list):
        raise ValueError("hooks.UserPromptSubmit must be a list")

    for group in groups:
        if not isinstance(group, dict):
            continue
        hook_items = group.get("hooks")
        if not isinstance(hook_items, list):
            continue
        for item in hook_items:
            if isinstance(item, dict) and "lazy_skill_router.py" in str(item.get("command", "")):
                if item.get("command") != hook_command:
                    item["command"] = hook_command
                    return "updated"
                return "unchanged"

    if not groups:
        groups.append({"hooks": []})

    target_group = next(
        (group for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)), None
    )
    if target_group is None:
        target_group = {"hooks": []}
        groups.append(target_group)

    target_group["hooks"].append(
        {
            "type": "command",
            "command": hook_command,
            "timeout": 5,
            "statusMessage": "Routing prompt to relevant skills",
        }
    )
    return "added"


def copy_file(source: Path, destination: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_skill(destination: Path, *, force: bool, dry_run: bool) -> str:
    if destination.exists() and not force:
        return "kept existing skill"
    if dry_run:
        return "would copy skill"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SKILL_SOURCE, destination)
    return "copied skill"


def route_errors(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(finding.message for finding in validate_config(config) if finding.severity == "ERROR")


def installed_names_for_install(codex_root: Path, agents_root: Path) -> set[str]:
    names = installed_skill_names(codex_root, agents_root)
    names.add("personal-skill-router")
    return names


def generated_routes(template_path: Path, codex_root: Path, agents_root: Path) -> dict[str, Any]:
    result = generate_config(
        load_json_object(template_path, "template root"),
        installed_names_for_install(codex_root, agents_root),
    )
    if generated_route_count(result) == 0:
        raise InstallError("generated 0 routes; no installed primary candidates matched")
    errors = route_errors(result.config)
    if errors:
        raise InstallError("generated routes failed validation: " + "; ".join(errors))
    return result.config


def validate_routes_config(config: dict[str, Any], path: Path) -> None:
    errors = route_errors(config)
    if errors:
        raise InstallError(f"routes failed validation at {path}: " + "; ".join(errors))


def smoke_hook(hook_path: Path, route_path: Path, prompt: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(hook_path), "--config", str(route_path), "--dry-run", prompt],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise InstallError(f"hook smoke test failed with exit code {completed.returncode}: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(f"hook smoke test did not return JSON: {exc}") from exc
    if not isinstance(payload, dict) or "shouldInject" not in payload:
        raise InstallError("hook smoke test returned unexpected JSON")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install lazy-skill-router into Codex hooks.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument("--template", default=str(TEMPLATE_SOURCE), help="Candidate-based route template JSON.")
    parser.add_argument("--smoke-prompt", default=DEFAULT_SMOKE_PROMPT, help="Prompt used for the hook smoke test.")
    parser.add_argument("--force", action="store_true", help="Overwrite the bundled personal-skill-router skill.")
    parser.add_argument("--overwrite-routes", action="store_true", help="Overwrite an existing routes.json.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    template_path = Path(args.template).expanduser()
    hooks_json = codex_root / "hooks.json"
    hook_destination = codex_root / "hooks" / "lazy_skill_router.py"
    routes_destination = codex_root / "lazy-skill-router" / "routes.json"
    skill_destination = codex_root / "skills" / "personal-skill-router"

    hook_command = f"python3 {shlex.quote(str(hook_destination))} --config {shlex.quote(str(routes_destination))}"
    actions: list[str] = []

    try:
        copy_file(HOOK_SOURCE, hook_destination, dry_run=args.dry_run)
        actions.append(f"copy hook {hook_destination}")
        copy_file(CORE_SOURCE, hook_destination.parent / "lazy_skill_router_core.py", dry_run=args.dry_run)
        actions.append(f"copy hook core {hook_destination.parent / 'lazy_skill_router_core.py'}")
        copy_file(COMMON_SOURCE, hook_destination.parent / "lazy_skill_router_common.py", dry_run=args.dry_run)
        actions.append(f"copy hook common {hook_destination.parent / 'lazy_skill_router_common.py'}")
        copy_file(LOGGING_SOURCE, hook_destination.parent / "lazy_skill_router_logging.py", dry_run=args.dry_run)
        actions.append(f"copy hook logging {hook_destination.parent / 'lazy_skill_router_logging.py'}")
        copy_file(SCORING_SOURCE, hook_destination.parent / "lazy_skill_router_scoring.py", dry_run=args.dry_run)
        actions.append(f"copy hook scoring {hook_destination.parent / 'lazy_skill_router_scoring.py'}")

        actions.append(f"{copy_skill(skill_destination, force=args.force, dry_run=args.dry_run)} {skill_destination}")

        if routes_destination.exists() and not args.overwrite_routes:
            validate_routes_config(load_json_object(routes_destination, "routes root"), routes_destination)
            actions.append(f"keep existing routes {routes_destination}")
            actions.append(f"validate existing routes {routes_destination}")
        else:
            route_config = generated_routes(template_path, codex_root, agents_root)
            if not args.dry_run:
                write_json(routes_destination, route_config)
            actions.append(f"generate routes {routes_destination}")
            actions.append(f"validate generated routes {routes_destination}")

        if args.dry_run:
            actions.append(f"would smoke test hook {hook_destination}")
            actions.append(f"would register hook entry in {hooks_json}")
        else:
            smoke_hook(hook_destination, routes_destination, args.smoke_prompt)
            actions.append(f"smoke test hook {hook_destination}")
            data = load_hooks(hooks_json)
            hook_state = ensure_user_prompt_hook(data, hook_command)
            if hook_state in {"added", "updated"}:
                backup = backup_file(hooks_json)
                write_json(hooks_json, data)
                if backup:
                    actions.append(f"backup {backup}")
                actions.append(f"{hook_state} hook entry in {hooks_json}")
            else:
                actions.append(f"kept existing hook entry in {hooks_json}")
    except (InstallError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("lazy-skill-router install summary:")
    for action in actions:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
