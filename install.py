from __future__ import annotations

import argparse
import shlex
import shutil
from pathlib import Path
from typing import Any

from lazy_skill_router_common import backup_file, codex_home, load_hooks, write_json

PROJECT_ROOT = Path(__file__).resolve().parent
HOOK_SOURCE = PROJECT_ROOT / "lazy_skill_router.py"
CORE_SOURCE = PROJECT_ROOT / "lazy_skill_router_core.py"
COMMON_SOURCE = PROJECT_ROOT / "lazy_skill_router_common.py"
LOGGING_SOURCE = PROJECT_ROOT / "lazy_skill_router_logging.py"
SCORING_SOURCE = PROJECT_ROOT / "lazy_skill_router_scoring.py"
ROUTES_SOURCE = PROJECT_ROOT / "routes.default.json"
SKILL_SOURCE = PROJECT_ROOT / "skills" / "personal-skill-router"


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Install lazy-skill-router into Codex hooks.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument("--force", action="store_true", help="Overwrite the bundled personal-skill-router skill.")
    parser.add_argument("--overwrite-routes", action="store_true", help="Overwrite an existing routes.json.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    hooks_json = codex_root / "hooks.json"
    hook_destination = codex_root / "hooks" / "lazy_skill_router.py"
    routes_destination = codex_root / "lazy-skill-router" / "routes.json"
    skill_destination = codex_root / "skills" / "personal-skill-router"

    data = load_hooks(hooks_json)
    hook_command = f"python3 {shlex.quote(str(hook_destination))} --config {shlex.quote(str(routes_destination))}"
    hook_state = ensure_user_prompt_hook(data, hook_command)

    actions: list[str] = []
    if hook_state in {"added", "updated"}:
        actions.append(f"{hook_state} hook entry in {hooks_json}")
        if not args.dry_run:
            backup = backup_file(hooks_json)
            write_json(hooks_json, data)
            if backup:
                actions.append(f"backup {backup}")
    else:
        actions.append(f"kept existing hook entry in {hooks_json}")

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

    if routes_destination.exists() and not args.overwrite_routes:
        actions.append(f"keep existing routes {routes_destination}")
    else:
        copy_file(ROUTES_SOURCE, routes_destination, dry_run=args.dry_run)
        actions.append(f"copy routes {routes_destination}")

    actions.append(f"{copy_skill(skill_destination, force=args.force, dry_run=args.dry_run)} {skill_destination}")

    print("lazy-skill-router install summary:")
    for action in actions:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
