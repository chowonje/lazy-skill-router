from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from pathlib import Path
from typing import Any


def default_codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".codex"


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-lazy-skill-router-uninstall-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} must contain a hooks object")
    return data


def remove_hook_entries(data: dict[str, Any]) -> int:
    hooks = data.setdefault("hooks", {})
    groups = hooks.get("UserPromptSubmit")
    if not isinstance(groups, list):
        return 0

    removed = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        hook_items = group.get("hooks")
        if not isinstance(hook_items, list):
            continue
        kept = []
        for item in hook_items:
            if isinstance(item, dict) and "lazy_skill_router.py" in str(item.get("command", "")):
                removed += 1
            else:
                kept.append(item)
        group["hooks"] = kept
    return removed


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def remove_path(path: Path, *, dry_run: bool) -> str:
    if not path.exists():
        return f"missing {path}"
    if dry_run:
        return f"would remove {path}"
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return f"removed {path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Uninstall lazy-skill-router from Codex hooks.")
    parser.add_argument("--codex-home", default=str(default_codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
    parser.add_argument("--remove-files", action="store_true", help="Also remove the installed hook, routes, and bundled skill.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files.")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    hooks_json = codex_home / "hooks.json"
    data = load_hooks(hooks_json)
    removed = remove_hook_entries(data)

    actions: list[str] = []
    if removed:
        actions.append(f"remove {removed} hook entry from {hooks_json}")
        if not args.dry_run:
            backup = backup_file(hooks_json)
            write_json(hooks_json, data)
            if backup:
                actions.append(f"backup {backup}")
    else:
        actions.append("no lazy-skill-router hook entry found")

    if args.remove_files:
        actions.append(remove_path(codex_home / "hooks" / "lazy_skill_router.py", dry_run=args.dry_run))
        actions.append(remove_path(codex_home / "hooks" / "lazy_skill_router_core.py", dry_run=args.dry_run))
        actions.append(remove_path(codex_home / "hooks" / "lazy_skill_router_scoring.py", dry_run=args.dry_run))
        actions.append(remove_path(codex_home / "lazy-skill-router", dry_run=args.dry_run))
        actions.append(remove_path(codex_home / "skills" / "personal-skill-router", dry_run=args.dry_run))

    print("lazy-skill-router uninstall summary:")
    for action in actions:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
