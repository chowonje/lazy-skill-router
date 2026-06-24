from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from lazy_skill_router_common import codex_home, debug


class RouteLike(Protocol):
    name: str
    primary: str


class RouteMatchLike(Protocol):
    route: RouteLike
    confidence: float
    score: float
    matched_signals: tuple[str, ...]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def log_decision(prompt: str, match: RouteMatchLike | None, config: dict[str, Any]) -> None:
    logging_config = config.get("logging")
    if not isinstance(logging_config, dict) or logging_config.get("enabled") is not True:
        return

    configured_path = logging_config.get("path")
    log_path = (
        Path(configured_path).expanduser()
        if isinstance(configured_path, str) and configured_path
        else codex_home() / "logs" / "lazy_skill_router.jsonl"
    )
    record = {
        "time": dt.datetime.now(dt.UTC).isoformat(),
        "promptHash": prompt_hash(prompt),
        "shouldInject": match is not None,
        "route": match.route.name if match else None,
        "primary": match.route.primary if match else None,
        "confidence": match.confidence if match else 0.0,
        "score": match.score if match else 0.0,
        "matchedSignals": list(match.matched_signals) if match else [],
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        debug(f"failed to write log: {exc}")
