#!/usr/bin/env python3
"""Create isolated, reproducible CI Relay demo scenes on the Desktop."""

import argparse
import hashlib
import json
import os
import re
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

_TrackedSnapshot = dict[str, tuple[bytes, int]]

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


def _normalized_allowed_files(source_root: Path, allowed_files: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in allowed_files:
        if not isinstance(value, str):
            raise ValueError("allowed fixture path must be a string")
        relative = Path(value)
        if not value or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid allowed fixture path: {value!r}")
        canonical = relative.as_posix()
        source = source_root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"allowed fixture path is not a regular file: {canonical}")
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise ValueError("fixture allowlist is empty")
    return tuple(sorted(normalized))


def _tracked_fixture_snapshot(repo_root: Path, source_root: Path) -> _TrackedSnapshot:
    try:
        source_relative = source_root.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("fixture source is outside the repository") from exc

    clean = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", source_relative.as_posix()],
        cwd=repo_root,
        check=False,
    )
    if clean.returncode == 1:
        raise ValueError("fixture contains uncommitted tracked changes")
    if clean.returncode != 0:
        raise ValueError("cannot verify fixture working-tree state")

    result = subprocess.run(
        ["git", "ls-tree", "-rz", "--full-tree", "HEAD", "--", source_relative.as_posix()],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError("cannot enumerate tracked fixture files")

    snapshot: _TrackedSnapshot = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            repository_path = Path(os.fsdecode(raw_path))
            relative = repository_path.relative_to(source_relative)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("git returned an invalid fixture path") from exc
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(f"tracked fixture path is not a regular file: {relative.as_posix()}")
        canonical = relative.as_posix()
        if canonical in snapshot:
            raise ValueError(f"duplicate tracked fixture path: {canonical}")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id.decode("ascii")],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            raise ValueError(f"cannot snapshot tracked fixture file: {canonical}")
        snapshot[canonical] = (blob.stdout, 0o755 if mode == b"100755" else 0o644)
    if not snapshot:
        raise ValueError("fixture allowlist is empty")
    return dict(sorted(snapshot.items()))


def _tracked_fixture_files(repo_root: Path, source_root: Path) -> tuple[str, ...]:
    return tuple(_tracked_fixture_snapshot(repo_root, source_root))


def _snapshot_manifest(snapshot: _TrackedSnapshot) -> dict[str, str]:
    return {relative: hashlib.sha256(content).hexdigest() for relative, (content, _) in snapshot.items()}


def _read_descriptor(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(file_fd, 64 * 1024)
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _allowed_fixture_snapshot(source_root: Path, allowed_files: Sequence[str]) -> _TrackedSnapshot:
    snapshot: _TrackedSnapshot = {}
    for relative in allowed_files:
        source = source_root / relative
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(source, flags)
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"allowed fixture path is not a regular file: {relative}")
            mode = 0o755 if file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else 0o644
            snapshot[relative] = (_read_descriptor(file_fd), mode)
        finally:
            os.close(file_fd)
    return snapshot


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_chain(root_fd: int, parts: Sequence[str], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _write_all(file_fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(file_fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("fixture write made no progress")
        view = view[written:]


def _write_file_at(directory_fd: int, name: str, content: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_fd = os.open(name, flags, mode, dir_fd=directory_fd)
    try:
        _write_all(file_fd, content)
        os.fchmod(file_fd, mode)
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _copy_snapshot_at(destination_fd: int, snapshot: _TrackedSnapshot) -> None:
    for relative, (content, mode) in snapshot.items():
        parts = Path(relative).parts
        parent_fd = _open_directory_chain(destination_fd, parts[:-1], create=True)
        try:
            _write_file_at(parent_fd, parts[-1], content, mode)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    os.fsync(destination_fd)


def _manifest_at(root_fd: int, allowed_files: Sequence[str]) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative in allowed_files:
        parts = Path(relative).parts
        parent_fd = _open_directory_chain(root_fd, parts[:-1], create=False)
        file_fd: int | None = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError(f"copied fixture path is not a regular file: {relative}")
            manifest[relative] = hashlib.sha256(_read_descriptor(file_fd)).hexdigest()
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent_fd)
    return manifest


def _write_text_atomic_at(directory_fd: int, name: str, content: str) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_file_at(directory_fd, temporary, content.encode("utf-8"))
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for child in os.listdir(directory_fd):
            child_stat = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(child_stat.st_mode):
                _remove_tree_at(directory_fd, child)
            else:
                os.unlink(child, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


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


def _next_session(output_fd: int) -> str:
    numbers = []
    for name in os.listdir(output_fd):
        match = _SESSION_PATTERN.fullmatch(name)
        if match:
            path_stat = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
            if not stat.S_ISDIR(path_stat.st_mode):
                raise ValueError(f"invalid existing session path: {name}")
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("demo session limit reached")
    return f"session-{number:03d}"


def _directory_identity(path: Path) -> tuple[int, int]:
    file_stat = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(file_stat.st_mode):
        raise ValueError(f"output root must be a regular directory: {path}")
    return file_stat.st_dev, file_stat.st_ino


def _output_root_matches(output_root: Path, expected_identity: tuple[int, int]) -> bool:
    try:
        _reject_symlink_components(output_root)
        return _directory_identity(output_root) == expected_identity
    except (OSError, ValueError):
        return False


def _require_output_root_identity(output_root: Path, expected_identity: tuple[int, int]) -> None:
    if not _output_root_matches(output_root, expected_identity):
        raise ValueError("owned output root identity changed during demo preparation")


def _open_owned_root_fd(output_root: Path, expected_identity: tuple[int, int]) -> int:
    output_fd = os.open(output_root, _directory_open_flags())
    file_stat = os.fstat(output_fd)
    if (file_stat.st_dev, file_stat.st_ino) != expected_identity:
        os.close(output_fd)
        raise ValueError("owned output root identity changed before descriptor open")
    return output_fd


def _ensure_owned_root(output_root: Path) -> tuple[int, int]:
    _reject_symlink_components(output_root)
    if os.path.lexists(output_root):
        if output_root.is_symlink() or not output_root.is_dir():
            raise ValueError(f"output root must be a regular directory: {output_root}")
        marker = output_root / _MARKER
        if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
            raise ValueError(f"existing output root is not owned by this demo: {output_root}")
        return _directory_identity(output_root)

    output_root.mkdir(mode=0o755)
    (output_root / _MARKER).write_text(_MARKER_CONTENT, encoding="utf-8")
    return _directory_identity(output_root)


def prepare_demo(
    source_root: Path,
    output_root: Path,
    *,
    source_revision: str,
    allowed_files: Optional[Sequence[str]] = None,
) -> Path:
    source_root = source_root.expanduser().absolute()
    output_root = output_root.expanduser().absolute()
    _validate_plain_tree(source_root)
    source_resolved = source_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    if _paths_overlap(source_resolved, output_resolved):
        raise ValueError("fixture source and output root overlap")

    tracked_snapshot = _tracked_fixture_snapshot(_repo_root(), source_root) if allowed_files is None else None
    allowed = (
        tuple(tracked_snapshot)
        if tracked_snapshot is not None
        else _normalized_allowed_files(source_root, allowed_files or ())
    )
    snapshot = tracked_snapshot if tracked_snapshot is not None else _allowed_fixture_snapshot(source_root, allowed)
    source_manifest = _snapshot_manifest(snapshot)

    output_identity = _ensure_owned_root(output_root)
    output_fd = _open_owned_root_fd(output_root, output_identity)
    staging_name: str | None = None
    try:
        _require_output_root_identity(output_root, output_identity)
        session_name = _next_session(output_fd)
        final_session = output_root / session_name
        staging_name = f".{session_name}.{uuid.uuid4().hex}.tmp"

        # Re-check the public path immediately before the first mutation. All
        # mutations after this point are anchored to the validated directory FD.
        _require_output_root_identity(output_root, output_identity)
        os.mkdir(staging_name, mode=0o755, dir_fd=output_fd)
        staging_fd = os.open(staging_name, _directory_open_flags(), dir_fd=output_fd)
        try:
            for scenario in _SCENARIOS:
                os.mkdir(scenario, mode=0o755, dir_fd=staging_fd)
                scenario_fd = os.open(scenario, _directory_open_flags(), dir_fd=staging_fd)
                try:
                    _copy_snapshot_at(scenario_fd, snapshot)
                    if _manifest_at(scenario_fd, allowed) != source_manifest:
                        raise RuntimeError(f"copied fixture does not match source: {scenario}")
                finally:
                    os.close(scenario_fd)

            metadata = {
                "schema": "lazy-skill-router.judge-demo-session/v1",
                "source_revision": source_revision,
                "fixture_sha256": _manifest_digest(source_manifest),
                "scenarios": list(_SCENARIOS),
            }
            _write_file_at(
                staging_fd,
                "SESSION.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)

        os.replace(staging_name, session_name, src_dir_fd=output_fd, dst_dir_fd=output_fd)
        staging_name = None
        os.fsync(output_fd)

        _write_text_atomic_at(output_fd, "START_HERE.md", _START_HERE)
        _write_text_atomic_at(output_fd, "CURRENT.txt", f"{session_name}\n")

        # Report failure when the caller-visible path no longer names the
        # directory that received the descriptor-relative writes.
        _require_output_root_identity(output_root, output_identity)
        return final_session
    except BaseException:
        if staging_name is not None:
            _remove_tree_at(output_fd, staging_name)
        raise
    finally:
        os.close(output_fd)


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

    print(f"Prepared demo session: {session.name}")
    print("Scenes:")
    for scenario in _SCENARIOS:
        print(f"- {scenario}")
    print("Open the selected --output-root directory and follow CURRENT.txt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
