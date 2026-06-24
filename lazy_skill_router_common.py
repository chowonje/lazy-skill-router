from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def debug(message: str) -> None:
    if os.environ.get("LAZY_SKILL_ROUTER_DEBUG"):
        print(f"lazy-skill-router: {message}", file=sys.stderr)


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


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
