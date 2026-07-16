#!/usr/bin/env python3
"""Create isolated, reproducible CI Relay demo scenes on the Desktop."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

_MARKER = ".lazy-skill-router-demo-root"
_MARKER_CONTENT = "lazy-skill-router judge demo root v1\n"
_SCENARIOS = ("01-mindmap", "02-ponytail", "03-security")
_SESSION_PATTERN = re.compile(r"session-(\d{3})")
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", ".demo-data")

_START_HERE = """# Lazy Skill Router Demo

이 폴더는 영상 촬영용 복사본입니다. 제품 소스와 연결된 symlink가 아니므로 자유롭게 수정해도 됩니다.
`CURRENT.txt`에 가장 최근 세션이 기록됩니다. 각 장면 폴더를 Codex에서 별도 프로젝트로 여세요.

## 01 Mindmap

> Map this repository as a project mind map. Show the main components and the data flow from event JSON to
> notification. Do not modify files.

## 02 Ponytail

> Add one retry to the notifier when TimeoutError occurs. Make the smallest correct change, preserve the public API,
> add one focused test, and add no dependency, sleep, or abstraction.

## 03 Security

> Scan this CI relay for a concrete exploitable security vulnerability before release. Validate it with a local
> proof, but do not modify files.

기본 검증 명령은 각 장면 폴더에서 `./scripts/verify.sh`입니다.
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _reject_symlink_components(path: Path) -> None:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
            raise ValueError(f"symlinked path component is not allowed: {current}")


def _validate_plain_tree(root: Path) -> None:
    _reject_symlink_components(root)
    if not root.is_dir():
        raise ValueError(f"fixture is not a directory: {root}")
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"fixture symlink is not allowed: {path}")
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"fixture special file is not allowed: {path}")


def _manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            parts = Path(relative).parts
            if "__pycache__" in parts or ".demo-data" in parts:
                continue
            if path.name == ".DS_Store" or path.suffix in {".pyc", ".pyo"}:
                continue
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _manifest_digest(manifest: dict[str, str]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _source_revision(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _next_session(output_root: Path) -> str:
    numbers = []
    for path in output_root.iterdir():
        match = _SESSION_PATTERN.fullmatch(path.name)
        if match:
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"invalid existing session path: {path}")
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("demo session limit reached")
    return f"session-{number:03d}"


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _ensure_owned_root(output_root: Path) -> None:
    _reject_symlink_components(output_root)
    if os.path.lexists(output_root):
        if output_root.is_symlink() or not output_root.is_dir():
            raise ValueError(f"output root must be a regular directory: {output_root}")
        marker = output_root / _MARKER
        if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
            raise ValueError(f"existing output root is not owned by this demo: {output_root}")
        return

    output_root.mkdir(mode=0o755)
    (output_root / _MARKER).write_text(_MARKER_CONTENT, encoding="utf-8")


def prepare_demo(source_root: Path, output_root: Path, *, source_revision: str) -> Path:
    source_root = source_root.expanduser().absolute()
    output_root = output_root.expanduser().absolute()
    _validate_plain_tree(source_root)
    _ensure_owned_root(output_root)

    source_manifest = _manifest(source_root)
    session_name = _next_session(output_root)
    final_session = output_root / session_name
    staging = output_root / f".{session_name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o755)

    try:
        for scenario in _SCENARIOS:
            destination = staging / scenario
            shutil.copytree(source_root, destination, symlinks=True, ignore=_COPY_IGNORE)
            _validate_plain_tree(destination)
            if _manifest(destination) != source_manifest:
                raise RuntimeError(f"copied fixture does not match source: {scenario}")

        metadata = {
            "schema": "lazy-skill-router.judge-demo-session/v1",
            "source_revision": source_revision,
            "fixture_sha256": _manifest_digest(source_manifest),
            "scenarios": list(_SCENARIOS),
        }
        (staging / "SESSION.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, final_session)
    except BaseException:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise

    _write_text_atomic(output_root / "START_HERE.md", _START_HERE)
    _write_text_atomic(output_root / "CURRENT.txt", f"{session_name}\n")
    return final_session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / "Desktop" / "Lazy Skill Router Demo",
        help="Directory that owns numbered demo sessions",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = _repo_root()
    source_root = repo_root / "examples" / "ci-relay-demo"
    try:
        session = prepare_demo(
            source_root,
            args.output_root,
            source_revision=_source_revision(repo_root),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"prepare-judge-demo: {exc}", file=sys.stderr)
        return 2

    print(session)
    for scenario in _SCENARIOS:
        print(session / scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
