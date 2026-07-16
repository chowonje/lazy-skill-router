"""Validation for the small CI event contract used by the demo."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STATUSES = frozenset({"queued", "running", "passed", "failed"})


@dataclass(frozen=True)
class CiEvent:
    repository: str
    run_id: str
    status: str
    artifact_name: str
    artifact_content: str


def _required_text(payload: Mapping[str, Any], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{key} exceeds {max_length} characters")
    return value


def parse_event(payload: Mapping[str, Any]) -> CiEvent:
    """Parse and validate one CI webhook-like payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("event must be an object")

    repository = _required_text(payload, "repository", max_length=160)
    run_id = _required_text(payload, "run_id", max_length=64)
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id may contain only letters, numbers, underscores, and hyphens")

    status = _required_text(payload, "status", max_length=16)
    if status not in _STATUSES:
        raise ValueError(f"unsupported status: {status}")

    artifact = payload.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("artifact must be an object")
    artifact_name = _required_text(artifact, "name", max_length=240)
    if Path(artifact_name).is_absolute() or "\x00" in artifact_name:
        raise ValueError("artifact name must be a relative path")
    artifact_content = _required_text(artifact, "content", max_length=1_000_000)

    return CiEvent(
        repository=repository,
        run_id=run_id,
        status=status,
        artifact_name=artifact_name,
        artifact_content=artifact_content,
    )
