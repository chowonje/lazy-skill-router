"""Artifact persistence for an intentionally vulnerable local demo fixture.

This implementation retains one bounded file-write flaw for the security scene.
Never deploy it or reuse it in production code.
"""

from pathlib import Path

from .webhook import CiEvent


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(self, event: CiEvent) -> Path:
        run_directory = self.root / event.run_id
        target = run_directory / event.artifact_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(event.artifact_content, encoding="utf-8")
        return target
