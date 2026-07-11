from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def debug(message: str) -> None:
    if os.environ.get("LAZY_SKILL_ROUTER_DEBUG"):
        print(f"lazy-skill-router: {message}", file=sys.stderr)


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def canonical_hook_command(hook_path: Path, routes_path: Path, *, stop: bool = False) -> str:
    argv = ("python3", str(hook_path), "--config", str(routes_path))
    if stop:
        argv = (*argv, "--hook-event", "stop")
    return shlex.join(argv)


def normalized_command_argv(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        argv = tuple(shlex.split(value))
    except ValueError:
        return None
    return argv or None


def command_matches_any(value: Any, expected_commands: Iterable[str]) -> bool:
    argv = normalized_command_argv(value)
    if argv is None:
        return False
    for command in expected_commands:
        expected = normalized_command_argv(command)
        if expected is not None and argv == expected:
            return True
    return False


def registered_hook_command(registration: Any, event_name: str) -> str | None:
    if not isinstance(registration, dict):
        return None
    if event_name == "UserPromptSubmit":
        entry = registration
    elif event_name == "Stop":
        entry = registration.get("lifecycle")
    else:
        return None
    if not isinstance(entry, dict) or entry.get("event") != event_name:
        return None
    command = entry.get("command")
    return command if normalized_command_argv(command) is not None else None


def load_json_object(path: Path, root_name: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{root_name} must be an object: {path}")
    return data


def load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    data = load_json_object(path, str(path))
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} must contain a hooks object")
    return data


def backup_file(path: Path, label: str = "") -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    label_part = f"-{label}" if label else ""
    backup_path = path.with_suffix(path.suffix + f".bak-lazy-skill-router{label_part}-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def ensure_safe_write_target(path: Path, managed_root: Path) -> None:
    target = path.absolute()
    managed = managed_root.absolute()
    if target.is_symlink():
        raise ValueError("write target is a symlink")

    candidates = (managed, Path.cwd().absolute(), Path.home().absolute(), Path(tempfile.gettempdir()).absolute())
    boundaries: list[Path] = []
    for candidate in candidates:
        try:
            target.relative_to(candidate)
        except ValueError:
            continue
        boundaries.append(candidate)
    boundary = max(boundaries, key=lambda item: len(item.parts)) if boundaries else Path(target.anchor)
    relative = target.relative_to(boundary)

    current = boundary
    if boundary == managed and current.is_symlink():
        raise ValueError("managed root is a symlink")
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("write target has a symlinked parent")
    try:
        target.parent.resolve(strict=False).relative_to(boundary.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("write target escapes trusted root") from exc


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
