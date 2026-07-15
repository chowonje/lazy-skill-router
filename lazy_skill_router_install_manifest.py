from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

INSTALL_MANIFEST_SCHEMA = "lazy-skill-router.install-manifest/v1"
OWNERSHIP_VALUES = {"managed", "generated", "preserved"}


@dataclass(frozen=True)
class InstallManifestSnapshot:
    state: str
    revision: str | None
    artifacts: tuple[dict[str, Any], ...]
    registration: dict[str, Any] | None
    reason_codes: tuple[str, ...] = ()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def directory_digest(path: Path) -> str:
    entries: list[dict[str, str]] = []
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            target_hash = sha256_bytes(os.readlink(child).encode())
            entries.append({"path": relative, "kind": "symlink", "digest": target_hash})
        elif child.is_file():
            entries.append({"path": relative, "kind": "file", "digest": file_digest(child)})
        elif child.is_dir():
            entries.append({"path": relative, "kind": "directory", "digest": ""})
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(canonical)


def path_kind_and_digest(path: Path) -> tuple[str, str]:
    if path.is_symlink():
        return "symlink", sha256_bytes(os.readlink(path).encode())
    if path.is_file():
        return "file", file_digest(path)
    if path.is_dir():
        return "directory", directory_digest(path)
    raise ValueError(f"install artifact missing: {path}")


def safe_relative_path(codex_root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(codex_root)
    except ValueError as exc:
        raise ValueError(f"install artifact is outside Codex home: {path}") from exc
    if not relative.parts or ".." in relative.parts:
        raise ValueError(f"invalid install artifact path: {path}")
    return relative.as_posix()


def artifact_record(codex_root: Path, path: Path, ownership: str) -> dict[str, str]:
    if ownership not in OWNERSHIP_VALUES:
        raise ValueError(f"unsupported install ownership: {ownership}")
    kind, digest = path_kind_and_digest(path)
    return {
        "path": safe_relative_path(codex_root, path),
        "kind": kind,
        "ownership": ownership,
        "digest": digest,
    }


def manifest_revision(artifacts: list[dict[str, Any]], registration: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "schema": INSTALL_MANIFEST_SCHEMA,
            "artifacts": artifacts,
            "registration": registration,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256_bytes(canonical)


def build_install_manifest(
    codex_root: Path,
    artifacts: Iterable[tuple[Path, str]],
    hook_command: str,
    *,
    stop_hook_command: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    records = [artifact_record(codex_root, path, ownership) for path, ownership in artifacts]
    records.sort(key=lambda artifact: artifact["path"])
    registration: dict[str, Any] = {"event": "UserPromptSubmit", "command": hook_command}
    if stop_hook_command is not None:
        registration["lifecycle"] = {"event": "Stop", "command": stop_hook_command}
    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": INSTALL_MANIFEST_SCHEMA,
        "revision": manifest_revision(records, registration),
        "generated_at": timestamp,
        "artifacts": records,
        "registration": registration,
    }


def refresh_generated_artifact_digests(
    snapshot: InstallManifestSnapshot,
    replacements: Mapping[str, str],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a manifest with selected generated-file digests refreshed.

    The ownership and registration contract is preserved.  A sync operation
    may refresh only artifacts that the installer already recorded as
    generated regular files.
    """

    if snapshot.state != "available" or snapshot.registration is None:
        raise ValueError("an available install ownership manifest is required")
    if not replacements:
        raise ValueError("at least one generated artifact digest is required")
    if any(not valid_manifest_path(path) for path in replacements):
        raise ValueError("generated artifact path is invalid")
    if any(
        not isinstance(digest, str)
        or len(digest) != len("sha256:") + 64
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest.removeprefix("sha256:"))
        for digest in replacements.values()
    ):
        raise ValueError("generated artifact digest is invalid")

    records = [dict(artifact) for artifact in snapshot.artifacts]
    for relative_path, digest in replacements.items():
        matches = [record for record in records if record.get("path") == relative_path]
        if len(matches) != 1:
            raise ValueError(f"install ownership record missing or ambiguous: {relative_path}")
        record = matches[0]
        if record.get("ownership") != "generated" or record.get("kind") != "file":
            raise ValueError(f"install ownership record is not a generated file: {relative_path}")
        record["digest"] = digest

    records.sort(key=lambda artifact: str(artifact.get("path", "")))
    registration = dict(snapshot.registration)
    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": INSTALL_MANIFEST_SCHEMA,
        "revision": manifest_revision(records, registration),
        "generated_at": timestamp,
        "artifacts": records,
        "registration": registration,
    }


def invalid_snapshot(
    reason: str,
    artifacts: tuple[dict[str, Any], ...] = (),
) -> InstallManifestSnapshot:
    return InstallManifestSnapshot("invalid", None, artifacts, None, (reason,))


def valid_manifest_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and path.as_posix() == value
        and "." not in path.parts
        and ".." not in path.parts
        and "\\" not in value
    )


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def confined_path(
    codex_root: Path,
    relative_value: Any,
    *,
    allow_leaf_symlink: bool,
) -> Path:
    if not valid_manifest_path(relative_value):
        raise ValueError("install manifest artifact path is invalid")
    relative = Path(str(relative_value))
    candidate = codex_root / relative

    current = codex_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("install artifact path contains a symlinked parent")

    resolved_root = codex_root.resolve(strict=False)
    resolved_parent = candidate.parent.resolve(strict=False)
    if not path_is_within(resolved_parent, resolved_root):
        raise ValueError("install artifact path escapes Codex home")
    if not allow_leaf_symlink:
        if candidate.is_symlink():
            raise ValueError("install artifact write target is a symlink")
        if not path_is_within(candidate.resolve(strict=False), resolved_root):
            raise ValueError("install artifact write target escapes Codex home")
    return candidate


def load_install_manifest(path: Path) -> InstallManifestSnapshot:
    if path.is_symlink():
        return invalid_snapshot("install_manifest_symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InstallManifestSnapshot("missing", None, (), None, ("install_manifest_missing",))
    except (OSError, json.JSONDecodeError):
        return invalid_snapshot("install_manifest_unreadable")
    if not isinstance(data, dict) or data.get("schema") != INSTALL_MANIFEST_SCHEMA:
        return invalid_snapshot("install_manifest_schema_unsupported")
    artifacts = data.get("artifacts")
    registration = data.get("registration")
    revision = data.get("revision")
    if not isinstance(artifacts, list) or not all(isinstance(artifact, dict) for artifact in artifacts):
        return invalid_snapshot("install_manifest_artifacts_invalid")
    if not isinstance(registration, dict):
        return invalid_snapshot("install_manifest_registration_invalid")
    path_aliases: dict[str, str] = {}
    for artifact in artifacts:
        if not valid_manifest_path(artifact.get("path")):
            return invalid_snapshot("install_manifest_path_invalid")
        raw_path = str(artifact["path"])
        path_key = unicodedata.normalize("NFC", raw_path).casefold()
        previous_path = path_aliases.get(path_key)
        if previous_path is not None:
            return invalid_snapshot("install_manifest_path_invalid", tuple(artifacts))
        path_aliases[path_key] = raw_path
        if artifact.get("ownership") not in OWNERSHIP_VALUES:
            return invalid_snapshot("install_manifest_ownership_invalid")
        if artifact.get("kind") not in {"file", "directory", "symlink"}:
            return invalid_snapshot("install_manifest_kind_invalid")
        if not isinstance(artifact.get("digest"), str):
            return invalid_snapshot("install_manifest_digest_invalid")
    if not isinstance(revision, str) or revision != manifest_revision(artifacts, registration):
        return invalid_snapshot("install_manifest_revision_mismatch")
    return InstallManifestSnapshot("available", revision, tuple(artifacts), registration)


def artifact_path(codex_root: Path, artifact: dict[str, Any]) -> Path:
    relative = artifact.get("path")
    return confined_path(codex_root, relative, allow_leaf_symlink=True)


def physical_artifact_aliases(
    snapshot: InstallManifestSnapshot,
    codex_root: Path,
) -> tuple[tuple[str, str], ...]:
    if snapshot.state != "available":
        return ()
    identities: dict[tuple[int, int], str] = {}
    aliases: list[tuple[str, str]] = []
    for artifact in snapshot.artifacts:
        relative = str(artifact.get("path"))
        try:
            installed_path = artifact_path(codex_root, artifact)
            installed_stat = installed_path.lstat()
        except (OSError, ValueError):
            continue
        physical_key = (installed_stat.st_dev, installed_stat.st_ino)
        previous = identities.get(physical_key)
        if previous is not None:
            aliases.append((previous, relative))
        else:
            identities[physical_key] = relative
    return tuple(aliases)


def artifact_state(codex_root: Path, artifact: dict[str, Any]) -> str:
    try:
        path = artifact_path(codex_root, artifact)
    except ValueError:
        return "unsafe"
    if path.is_symlink():
        return "symlink"
    if not path.exists():
        return "missing"
    try:
        kind, digest = path_kind_and_digest(path)
    except (OSError, ValueError):
        return "unreadable"
    if kind != artifact.get("kind") or digest != artifact.get("digest"):
        return "modified"
    return "matching"
