from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path


EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
EXCLUDED_FILES = {"SHA256SUMS"}


@dataclass(frozen=True)
class Checksum:
    digest: str
    path: str


def should_include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    return path.is_file() and path.name not in EXCLUDED_FILES


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def collect_checksums(root: Path) -> list[Checksum]:
    checksums: list[Checksum] = []
    for path in sorted(root.rglob("*")):
        if should_include(path, root):
            checksums.append(Checksum(digest_file(path), path.relative_to(root).as_posix()))
    return checksums


def write_manifest(root: Path, output: Path) -> None:
    checksums = collect_checksums(root)
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


def verify_manifest(root: Path, manifest: Path) -> int:
    failures: list[str] = []
    for item in parse_manifest(manifest):
        path = root / item.path
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
    print(f"OK: verified {manifest}")
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
