from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from lazy_skill_router_common import (
    backup_file,
    canonical_hook_command,
    codex_home,
    command_matches_any,
    load_hooks,
    registered_hook_command,
    write_json,
)
from lazy_skill_router_install_manifest import artifact_path, artifact_state, confined_path, load_install_manifest


def remove_hook_entries(
    data: dict[str, Any],
    *,
    prompt_owned_commands: tuple[str, ...],
    stop_owned_commands: tuple[str, ...],
) -> int:
    hooks = data.setdefault("hooks", {})
    removed = 0
    for event_name, owned_commands in (
        ("UserPromptSubmit", prompt_owned_commands),
        ("Stop", stop_owned_commands),
    ):
        groups = hooks.get(event_name)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_items = group.get("hooks")
            if not isinstance(hook_items, list):
                continue
            kept = []
            for item in hook_items:
                if isinstance(item, dict) and command_matches_any(item.get("command"), owned_commands):
                    removed += 1
                else:
                    kept.append(item)
            group["hooks"] = kept
    return removed


def remove_path(path: Path, *, dry_run: bool) -> str:
    if not path.exists() and not path.is_symlink():
        return f"missing {path}"
    if dry_run:
        return f"would remove {path}"
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return f"removed {path}"


def confined_manifest_path(codex_root: Path, manifest_path: Path) -> Path:
    try:
        relative = manifest_path.relative_to(codex_root)
    except ValueError as exc:
        raise ValueError("ownership manifest is outside Codex home") from exc
    return confined_path(codex_root, relative.as_posix(), allow_leaf_symlink=False)


def remove_manifest_artifacts(codex_root: Path, manifest_path: Path, *, dry_run: bool) -> list[str]:
    try:
        manifest_path = confined_manifest_path(codex_root, manifest_path)
    except ValueError as exc:
        return [f"kept installed files because ownership manifest path is unsafe: {exc}"]
    snapshot = load_install_manifest(manifest_path)
    if snapshot.state != "available":
        reason = ", ".join(snapshot.reason_codes) if snapshot.reason_codes else snapshot.state
        return [f"kept installed files because ownership manifest is unavailable: {reason}"]

    actions: list[str] = []
    protected = False
    artifacts = sorted(snapshot.artifacts, key=lambda item: len(Path(str(item.get("path", ""))).parts), reverse=True)
    for artifact in artifacts:
        ownership = str(artifact.get("ownership"))
        try:
            path = artifact_path(codex_root, artifact)
        except ValueError:
            actions.append(f"kept unsafe artifact path {artifact.get('path')}")
            protected = True
            continue
        if ownership == "preserved":
            actions.append(f"kept preserved artifact {path}")
            protected = True
            continue
        state = artifact_state(codex_root, artifact)
        if state == "matching":
            actions.append(remove_path(path, dry_run=dry_run))
        elif state == "symlink":
            actions.append(f"kept symlink {ownership} artifact {path}")
            protected = True
        elif state in {"modified", "unreadable"}:
            actions.append(f"kept modified {ownership} artifact {path}")
            protected = True
        else:
            actions.append(f"missing {path}")

    if protected:
        actions.append(f"kept ownership manifest {manifest_path} for remaining artifacts")
    else:
        actions.append(remove_path(manifest_path, dry_run=dry_run))
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Uninstall lazy-skill-router from Codex hooks.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--remove-files", action="store_true", help="Also remove the installed hook, routes, and bundled skill."
    )
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    manifest_path = codex_root / "lazy-skill-router" / "install.manifest.json"
    try:
        hooks_json = confined_path(codex_root, "hooks.json", allow_leaf_symlink=False)
        data = load_hooks(hooks_json)
    except (OSError, ValueError) as exc:
        print(f"ERROR: unsafe or unreadable hooks.json: {exc}", file=sys.stderr)
        return 1
    hook_path = codex_root / "hooks" / "lazy_skill_router.py"
    routes_path = codex_root / "lazy-skill-router" / "routes.json"
    prompt_owned_commands = [canonical_hook_command(hook_path, routes_path)]
    stop_owned_commands = [canonical_hook_command(hook_path, routes_path, stop=True)]
    try:
        safe_manifest_path = confined_manifest_path(codex_root, manifest_path)
    except ValueError:
        manifest_snapshot = None
    else:
        manifest_snapshot = load_install_manifest(safe_manifest_path)
    if manifest_snapshot is not None and manifest_snapshot.state == "available":
        registered_prompt = registered_hook_command(manifest_snapshot.registration, "UserPromptSubmit")
        registered_stop = registered_hook_command(manifest_snapshot.registration, "Stop")
        if registered_prompt is not None:
            prompt_owned_commands.append(registered_prompt)
        if registered_stop is not None:
            stop_owned_commands.append(registered_stop)
    removed = remove_hook_entries(
        data,
        prompt_owned_commands=tuple(dict.fromkeys(prompt_owned_commands)),
        stop_owned_commands=tuple(dict.fromkeys(stop_owned_commands)),
    )

    actions: list[str] = []
    if removed:
        actions.append(f"remove {removed} hook entry from {hooks_json}")
        if not args.dry_run:
            backup = backup_file(hooks_json, "uninstall")
            write_json(hooks_json, data)
            if backup:
                actions.append(f"backup {backup}")
    else:
        actions.append("no lazy-skill-router hook entry found")

    if args.remove_files:
        actions.extend(remove_manifest_artifacts(codex_root, manifest_path, dry_run=args.dry_run))

    print("lazy-skill-router uninstall summary:")
    for action in actions:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
