"""Persistence for validated CI run metadata."""

import json
from dataclasses import asdict
from pathlib import Path

from .webhook import CiEvent


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(self, event: CiEvent) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{event.run_id}.json"
        payload = {key: value for key, value in asdict(event).items() if key not in {"artifact_content"}}
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target
