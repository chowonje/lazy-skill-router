from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import eval_router_ab

REPLAY_BUNDLE_SCHEMA: Final = "lazy-skill-router.router-ab-replay-bundle/v1"
REPLAY_BUNDLE_REF_SCHEMA: Final = "lazy-skill-router.router-ab-replay-bundle-ref/v1"
MAX_REPLAY_ARTIFACT_BYTES: Final = 16 * 1024 * 1024
NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ROLE_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
CHUNK_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class StoredArtifact:
    role: str
    byte_revision: str
    size_bytes: int
    blob: str
    path: Path


def default_store() -> Path:
    return Path.home() / ".codex" / "private" / "lazy-skill-router" / "router-ab"


def ensure_private_directory(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_fd = os.open(expanded, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (AttributeError, OSError) as exc:
        raise ValueError("store path must be an available non-symlink directory") from exc
    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("store path must be a directory")
        resolved = expanded.resolve(strict=True)
        resolved_stat = os.stat(resolved, follow_symlinks=False)
        if (directory_stat.st_dev, directory_stat.st_ino) != (resolved_stat.st_dev, resolved_stat.st_ino):
            raise ValueError("store path changed while it was opened")
        os.fchmod(directory_fd, 0o700)
    finally:
        os.close(directory_fd)
    return resolved


def read_fd_digest(file_fd: int, maximum: int = MAX_REPLAY_ARTIFACT_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(file_fd, min(CHUNK_BYTES, maximum - total + 1))
        if not chunk:
            return digest.hexdigest(), total
        total += len(chunk)
        if total > maximum:
            raise ValueError("artifact exceeds the private store size limit")
        digest.update(chunk)


def source_fd(path: Path) -> tuple[int, os.stat_result]:
    expanded = path.expanduser()
    try:
        file_fd = os.open(
            expanded,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        )
    except (AttributeError, OSError) as exc:
        raise ValueError("artifact source is unavailable or a symlink") from exc
    try:
        file_stat = os.fstat(file_fd)
    except OSError as exc:
        os.close(file_fd)
        raise ValueError("artifact source is unreadable") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(file_fd)
        raise ValueError("artifact source must be a regular file")
    if file_stat.st_size <= 0:
        os.close(file_fd)
        raise ValueError("artifact source must not be empty")
    if file_stat.st_size > MAX_REPLAY_ARTIFACT_BYTES:
        os.close(file_fd)
        raise ValueError("artifact exceeds the private store size limit")
    return file_fd, file_stat


def verify_existing_blob(path: Path, expected_digest: str, expected_size: int) -> None:
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (AttributeError, OSError) as exc:
        raise ValueError("existing private blob is unavailable") from exc
    try:
        initial_stat = os.fstat(file_fd)
        if not stat.S_ISREG(initial_stat.st_mode):
            raise ValueError("existing private blob is not regular")
        observed_digest, observed_size = read_fd_digest(file_fd)
        final_stat = os.fstat(file_fd)
    finally:
        os.close(file_fd)
    if (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
        initial_stat.st_ctime_ns,
    ) != (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
        final_stat.st_mtime_ns,
        final_stat.st_ctime_ns,
    ):
        raise ValueError("existing private blob changed during content verification")
    if observed_digest != expected_digest or observed_size != expected_size:
        raise ValueError("existing private blob failed content verification")


def store_blob(role: str, source: Path, blobs: Path) -> StoredArtifact:
    file_fd, initial_stat = source_fd(source)
    temp_fd = -1
    temp_path: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        try:
            temp_fd, temp_name = tempfile.mkstemp(prefix=".blob-", dir=blobs)
            temp_path = Path(temp_name)
            with os.fdopen(temp_fd, "wb") as output:
                temp_fd = -1
                while True:
                    chunk = os.read(file_fd, min(CHUNK_BYTES, MAX_REPLAY_ARTIFACT_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REPLAY_ARTIFACT_BYTES:
                        raise ValueError("artifact exceeds the private store size limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            final_stat = os.fstat(file_fd)
        finally:
            os.close(file_fd)
            if temp_fd >= 0:
                os.close(temp_fd)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    assert temp_path is not None
    if (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
        initial_stat.st_ctime_ns,
    ) != (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
        final_stat.st_mtime_ns,
        final_stat.st_ctime_ns,
    ):
        temp_path.unlink(missing_ok=True)
        raise ValueError("artifact source changed while it was copied")

    digest_hex = digest.hexdigest()
    destination = blobs / digest_hex
    try:
        if destination.exists() or destination.is_symlink():
            verify_existing_blob(destination, digest_hex, total)
            temp_path.unlink(missing_ok=True)
        else:
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return StoredArtifact(
        role=role,
        byte_revision="sha256:" + digest_hex,
        size_bytes=total,
        blob=f"blobs/sha256/{digest_hex}",
        path=destination,
    )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def write_immutable(path: Path, content: bytes) -> None:
    file_fd, temp_name = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_fd, "wb") as handle:
            file_fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        try:
            os.link(temp_path, path, follow_symlinks=False)
        except FileExistsError:
            try:
                verify_existing_blob(path, hashlib.sha256(content).hexdigest(), len(content))
            except ValueError as exc:
                raise ValueError("private store target already exists with different content") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        temp_path.unlink(missing_ok=True)


def parse_extra(values: list[str]) -> dict[str, Path]:
    extras: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = value.partition("=")
        if not separator or ROLE_RE.fullmatch(role) is None or role in extras or not raw_path:
            raise ValueError("--extra must use a unique lowercase role=path")
        extras[role] = Path(raw_path)
    return extras


def preserve_bundle(
    *,
    store: Path,
    name: str,
    config: Path,
    inventory: Path,
    index: Path,
    manifest: Path,
    extras: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if NAME_RE.fullmatch(name) is None:
        raise ValueError("bundle name is invalid")
    sources = {"config": config, "inventory": inventory, "index": index, "manifest": manifest}
    for role, path in (extras or {}).items():
        if ROLE_RE.fullmatch(role) is None or role in sources:
            raise ValueError("extra artifact role is invalid or duplicated")
        sources[role] = path

    root = ensure_private_directory(store)
    blobs = ensure_private_directory(ensure_private_directory(root / "blobs") / "sha256")
    descriptors = ensure_private_directory(ensure_private_directory(root / "manifests") / "sha256")
    refs = ensure_private_directory(root / "refs")
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as stage_name:
        stage = ensure_private_directory(Path(stage_name))
        staged = [store_blob(role, path, stage) for role, path in sorted(sources.items())]
        by_role = {artifact.role: artifact for artifact in staged}
        parsed_manifest = eval_router_ab.load_manifest(by_role["manifest"].path)
        verified = eval_router_ab.verify_inputs(
            eval_router_ab.load_config(by_role["config"].path),
            by_role["inventory"].path,
            by_role["index"].path,
            parsed_manifest.frozen,
        )
        stored = [store_blob(artifact.role, artifact.path, blobs) for artifact in staged]
    descriptor = {
        "schema": REPLAY_BUNDLE_SCHEMA,
        "name": name,
        "experimentManifestRevision": parsed_manifest.revision,
        "frozenInputs": eval_router_ab.frozen_payload(verified.frozen),
        "artifacts": [
            {
                "role": artifact.role,
                "byteRevision": artifact.byte_revision,
                "sizeBytes": artifact.size_bytes,
                "blob": artifact.blob,
            }
            for artifact in stored
        ],
        "validation": {
            "status": "passed",
            "sourcePathsStored": False,
            "privateStore": True,
        },
    }
    descriptor_content = canonical_bytes(descriptor)
    descriptor_digest = hashlib.sha256(descriptor_content).hexdigest()
    descriptor_revision = "sha256:" + descriptor_digest
    descriptor_path = descriptors / f"{descriptor_digest}.json"
    write_immutable(descriptor_path, descriptor_content)
    ref = {
        "schema": REPLAY_BUNDLE_REF_SCHEMA,
        "name": name,
        "descriptorRevision": descriptor_revision,
        "descriptor": f"manifests/sha256/{descriptor_digest}.json",
    }
    write_immutable(refs / f"{name}.json", canonical_bytes(ref))
    return {
        "schema": REPLAY_BUNDLE_REF_SCHEMA,
        "name": name,
        "descriptorRevision": descriptor_revision,
        "experimentManifestRevision": parsed_manifest.revision,
        "artifacts": len(stored),
        "validationStatus": "passed",
        "sourcePathsStored": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preserve a frozen router A/B replay bundle in a private local CAS.")
    parser.add_argument("--store", type=Path, default=default_store(), help="Private content-addressed store root.")
    parser.add_argument("--name", required=True, help="Immutable local bundle reference name.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--extra", action="append", default=[], help="Additional lowercase role=path artifact.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = preserve_bundle(
            store=args.store,
            name=args.name,
            config=args.config,
            inventory=args.inventory,
            index=args.index,
            manifest=args.manifest,
            extras=parse_extra(args.extra),
        )
    except ValueError as exc:
        raise SystemExit(f"INVALID: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
