from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_DIRS = {".ci", ".codegraph", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "artifacts"}
EXCLUDED_FILES = {"SHA256SUMS"}


@dataclass(frozen=True)
class Checksum:
    digest: str
    path: str


def should_include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if path.is_symlink():
        raise ValueError(f"checksum root contains symlink: {relative.as_posix()}")
    return path.is_file() and path.name not in EXCLUDED_FILES


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def includable_files(root: Path, excluded_paths: tuple[Path, ...] = ()) -> list[Path]:
    excluded = {path.resolve(strict=False) for path in excluded_paths}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not should_include(path, root):
            continue
        if path.resolve(strict=False) in excluded:
            continue
        files.append(path)
    return files


def collect_checksums(root: Path, excluded_paths: tuple[Path, ...] = ()) -> list[Checksum]:
    return [
        Checksum(digest_file(path), path.relative_to(root).as_posix())
        for path in includable_files(root, excluded_paths)
    ]


def write_manifest(root: Path, output: Path) -> None:
    checksums = collect_checksums(root, (output,))
    with output.open("w", encoding="utf-8") as handle:
        for item in checksums:
            handle.write(f"{item.digest}  {item.path}\n")
    print(f"Wrote {len(checksums)} checksums to {output}")


def parse_manifest(path: Path) -> list[Checksum]:
    checksums: list[Checksum] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(f"invalid manifest line {line_number}: {line.rstrip()}")
            checksums.append(Checksum(parts[0], parts[1].strip()))
    return checksums


def safe_manifest_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if not value or not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must be a nonempty relative path without '..'")

    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("path must not contain a symlink")

    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path resolves outside the checksum root") from exc
    return candidate


def verify_manifest(root: Path, manifest: Path) -> int:
    failures: list[str] = []
    root = root.resolve(strict=False)
    try:
        checksums = parse_manifest(manifest)
    except (OSError, ValueError) as exc:
        print(f"INVALID MANIFEST: {exc}")
        return 1

    if not checksums:
        failures.append("EMPTY manifest has no checksum entries")

    paths: dict[str, Path] = {}
    for item in checksums:
        if item.path in paths:
            failures.append(f"DUPLICATE {item.path}")
            continue
        try:
            paths[item.path] = safe_manifest_path(root, item.path)
        except ValueError as exc:
            failures.append(f"UNSAFE {item.path}: {exc}")

    try:
        expected_paths = {path.relative_to(root).as_posix() for path in includable_files(root, (manifest,))}
    except ValueError as exc:
        failures.append(f"UNSAFE ROOT: {exc}")
        expected_paths = set()
    listed_paths = set(paths)
    for path in sorted(expected_paths - listed_paths):
        failures.append(f"UNLISTED {path}")
    for path in sorted(listed_paths - expected_paths):
        failures.append(f"UNEXPECTED {path}")

    if failures:
        for failure in failures:
            print(failure)
        return 1

    for item in checksums:
        path = paths[item.path]
        if not path.is_file():
            failures.append(f"MISSING {item.path}")
            continue
        actual = digest_file(path)
        if actual != item.digest:
            failures.append(f"CHANGED {item.path}")
    for failure in failures:
        print(failure)
    if failures:
        return 1
    print(f"OK: verified {len(checksums)} checksums from {manifest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify SHA-256 checksums for release files.")
    parser.add_argument("--root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--output", help="Write a checksum manifest to this path.")
    parser.add_argument("--verify", help="Verify files against an existing checksum manifest.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.output:
        write_manifest(root, Path(args.output))
        return 0
    if args.verify:
        return verify_manifest(root, Path(args.verify))

    parser.error("pass --output or --verify")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
