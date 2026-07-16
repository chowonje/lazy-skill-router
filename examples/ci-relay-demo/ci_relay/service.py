"""Orchestration for the CI relay data flow."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore
from .notifier import Sender, build_message, send_notification
from .run_store import RunStore
from .webhook import parse_event


@dataclass(frozen=True)
class ProcessResult:
    run_record: Path
    artifact: Path
    notification: str

    def as_dict(self) -> dict[str, str]:
        return {
            "run_record": str(self.run_record),
            "artifact": str(self.artifact),
            "notification": self.notification,
        }


def process_event(payload: Mapping[str, Any], workspace: Path, sender: Sender) -> ProcessResult:
    event = parse_event(payload)
    workspace = Path(workspace)
    run_record = RunStore(workspace / "runs").save(event)
    artifact = ArtifactStore(workspace / "artifacts").save(event)
    message = build_message(event)
    send_notification(message, sender)
    return ProcessResult(run_record=run_record, artifact=artifact, notification=message)
