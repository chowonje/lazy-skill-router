from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_routes import (
    TEMPLATE_SOURCE,
    TemplateError,
    generate_config,
    generated_route_count,
    installed_skill_names,
)
from lazy_skill_router_capability_index import DEFAULT_CAPABILITY_INDEX_NAME, build_capability_index
from lazy_skill_router_common import (
    EMPTY_DIRECTORY_DIGEST,
    MISSING_PATH_IDENTITY,
    ConfinedPathIdentity,
    backup_file,
    canonical_hook_command,
    codex_home,
    command_matches_any,
    confined_atomic_write_bytes,
    confined_copy_path_to_private,
    confined_create_private_directory,
    confined_create_symlink,
    confined_directory_names,
    confined_ensure_managed_root,
    confined_ensure_parent,
    confined_list_private_directories,
    confined_path_identity,
    confined_read_bytes,
    confined_readlink,
    confined_remove_path,
    confined_replace_tree,
    confined_rmdir,
    load_hooks,
    load_json_object,
    registered_hook_command,
    write_json,
    write_json_atomic,
)
from lazy_skill_router_host_catalog import load_host_catalog, reconcile_inventory
from lazy_skill_router_install_manifest import (
    InstallManifestSnapshot,
    artifact_state,
    build_install_manifest,
    confined_path,
    load_install_manifest,
    path_kind_and_digest,
    physical_artifact_aliases,
    safe_relative_path,
    sha256_bytes,
)
from lazy_skill_router_inventory import (
    InventorySnapshot,
    build_inventory_manifest,
    inventory_revision,
)
from lazy_skill_router_policy_ir import parse_policy_config, select_smoke_primary
from sync_skills import scan_installed_skills
from validate_routes import validate_config

PROJECT_ROOT = Path(__file__).resolve().parent
HOOK_SOURCE = PROJECT_ROOT / "lazy_skill_router.py"
CORE_SOURCE = PROJECT_ROOT / "lazy_skill_router_core.py"
COMMON_SOURCE = PROJECT_ROOT / "lazy_skill_router_common.py"
LOGGING_SOURCE = PROJECT_ROOT / "lazy_skill_router_logging.py"
SCORING_SOURCE = PROJECT_ROOT / "lazy_skill_router_scoring.py"
CONTRACTS_SOURCE = PROJECT_ROOT / "lazy_skill_router_contracts.py"
INVENTORY_SOURCE = PROJECT_ROOT / "lazy_skill_router_inventory.py"
POLICY_IR_SOURCE = PROJECT_ROOT / "lazy_skill_router_policy_ir.py"
ACTIVATION_SOURCE = PROJECT_ROOT / "lazy_skill_router_activation.py"
CAPABILITY_INDEX_SOURCE = PROJECT_ROOT / "lazy_skill_router_capability_index.py"
RETRIEVAL_SOURCE = PROJECT_ROOT / "lazy_skill_router_retrieval.py"
SKILL_SOURCE = PROJECT_ROOT / "skills" / "personal-skill-router"
INTERNAL_SMOKE_PROMPT = "lazy-skill-router-internal-probe"
TRANSACTION_JOURNAL_SCHEMA = "lazy-skill-router.install-transaction/v1"
TRANSACTION_PREFIX = "lazy-skill-router-rollback-"


@dataclass(frozen=True)
class InstallError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class PathSnapshot:
    path: Path
    kind: str
    backup: Path | None = None
    link_target: str | None = None
    identity: ConfinedPathIdentity = MISSING_PATH_IDENTITY
    backup_identity: ConfinedPathIdentity | None = None


def checked_install_path(codex_root: Path, path: Path, *, allow_leaf_symlink: bool) -> Path:
    try:
        relative = safe_relative_path(codex_root, path)
        return confined_path(codex_root, relative, allow_leaf_symlink=allow_leaf_symlink)
    except ValueError as exc:
        raise InstallError(f"unsafe install target path: {path}: {exc}") from exc


class InstallMutation:
    def __init__(self, codex_root: Path, targets: tuple[Path, ...]) -> None:
        self.codex_root = codex_root
        self.targets = tuple(dict.fromkeys(targets))
        self.snapshots: list[PathSnapshot] = []
        self.created_paths: list[Path] = []
        self.created_parents: set[Path] = set()
        self.committed_identities: dict[Path, ConfinedPathIdentity] = {}
        self.temp_dir: Path | None = None
        self.temp_identity: ConfinedPathIdentity | None = None
        self.journal_identity: ConfinedPathIdentity | None = None

    def __enter__(self) -> InstallMutation:
        for path in self.targets:
            checked_install_path(self.codex_root, path, allow_leaf_symlink=True)
        self._record_created_parents()
        confined_ensure_managed_root(self.codex_root)
        self.temp_dir, self.temp_identity = confined_create_private_directory(
            self.codex_root.parent,
            TRANSACTION_PREFIX,
        )
        backup_root = self.temp_dir
        try:
            for index, path in enumerate(self.targets):
                backup = backup_root / str(index)
                identity = confined_path_identity(
                    path,
                    self.codex_root,
                    allow_leaf_symlink=True,
                    missing_parent_is_missing=True,
                )
                if identity.kind == "symlink":
                    self.snapshots.append(
                        PathSnapshot(
                            path,
                            "symlink",
                            link_target=confined_readlink(path, self.codex_root, identity),
                            identity=identity,
                        )
                    )
                elif identity.kind in {"file", "directory"}:
                    confined_copy_path_to_private(path, self.codex_root, identity, backup)
                    backup_identity = confined_path_identity(backup, self.temp_dir)
                    self.snapshots.append(
                        PathSnapshot(
                            path,
                            str(identity.kind),
                            backup=backup,
                            identity=identity,
                            backup_identity=backup_identity,
                        )
                    )
                elif identity.state == "missing":
                    self.snapshots.append(PathSnapshot(path, "missing", identity=identity))
                else:
                    raise InstallError(f"unsupported install target kind: {path}")
            self.write_journal()
            return self
        except Exception:
            self.cleanup_temp_dir()
            raise

    def _record_created_parents(self) -> None:
        for target in self.targets:
            parent = target.parent
            while parent == self.codex_root or self.codex_root in parent.parents:
                if not parent.exists():
                    self.created_parents.add(parent)
                if parent == self.codex_root:
                    break
                parent = parent.parent

    def track_created(self, path: Path | None) -> None:
        if path is not None:
            self.created_paths.append(path)
            identity = confined_path_identity(path, self.codex_root, allow_leaf_symlink=True)
            self.committed_identities[path] = identity
            self.write_journal()

    def record_committed(self, path: Path, identity: ConfinedPathIdentity) -> None:
        self.committed_identities[path] = identity
        self.write_journal()

    def journal_relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.codex_root)
        except ValueError as exc:
            raise InstallError(f"transaction path is outside Codex home: {path}") from exc
        return relative.as_posix() if relative.parts else "."

    def write_journal(self) -> None:
        if self.temp_dir is None or self.temp_identity is None:
            raise InstallError("transaction journal unavailable")
        transaction_root = self.temp_dir
        current_root = self.current_temp_root_identity()
        snapshots = []
        for snapshot in self.snapshots:
            backup = snapshot.backup.relative_to(transaction_root).as_posix() if snapshot.backup is not None else None
            snapshots.append(
                {
                    "path": self.journal_relative(snapshot.path),
                    "kind": snapshot.kind,
                    "backup": backup,
                    "link_target": snapshot.link_target,
                    "state": snapshot.identity.state,
                    "device": snapshot.identity.device,
                    "inode": snapshot.identity.inode,
                    "mode": snapshot.identity.mode,
                    "size": snapshot.identity.size,
                    "digest": snapshot.identity.digest,
                    "backup_identity": (
                        {
                            "state": snapshot.backup_identity.state,
                            "kind": snapshot.backup_identity.kind,
                            "device": snapshot.backup_identity.device,
                            "inode": snapshot.backup_identity.inode,
                            "mode": snapshot.backup_identity.mode,
                            "size": snapshot.backup_identity.size,
                            "digest": snapshot.backup_identity.digest,
                        }
                        if snapshot.backup_identity is not None
                        else None
                    ),
                }
            )
        journal = {
            "schema": TRANSACTION_JOURNAL_SCHEMA,
            "root_fingerprint": codex_root_fingerprint(self.codex_root),
            "transaction_root_identity": {
                "device": current_root.device,
                "inode": current_root.inode,
            },
            "snapshots": snapshots,
            "created_paths": [self.journal_relative(path) for path in self.created_paths],
            "created_parents": [
                self.journal_relative(path)
                for path in sorted(self.created_parents, key=lambda item: len(item.parts), reverse=True)
            ],
            "committed": [
                {
                    "path": self.journal_relative(path),
                    "state": identity.state,
                    "kind": identity.kind,
                    "device": identity.device,
                    "inode": identity.inode,
                    "mode": identity.mode,
                    "size": identity.size,
                    "digest": identity.digest,
                }
                for path, identity in sorted(
                    self.committed_identities.items(),
                    key=lambda item: item[0].as_posix(),
                )
            ],
        }
        journal_path = transaction_root / "journal.json"
        journal_identity = confined_path_identity(journal_path, transaction_root)
        written_identity = write_json_atomic(
            journal_path,
            journal,
            managed_root=transaction_root,
            expected=journal_identity,
        )
        if written_identity is None:
            raise InstallError("transaction journal write did not return an identity")
        self.journal_identity = written_identity

    def remove_current(self, path: Path) -> None:
        current = confined_path_identity(
            path,
            self.codex_root,
            allow_leaf_symlink=True,
            missing_parent_is_missing=True,
        )
        if current.state != "missing":
            confined_remove_path(path, self.codex_root, current)

    def rollback(self) -> None:
        rollback_errors: list[Exception] = []
        for path in reversed(self.created_paths):
            try:
                current = confined_path_identity(
                    path,
                    self.codex_root,
                    allow_leaf_symlink=True,
                    missing_parent_is_missing=True,
                )
                expected = self.committed_identities.get(path)
                if expected is not None and current != expected:
                    raise InstallError(f"rollback target changed; preserving concurrent replacement: {path}")
                if current.state != "missing":
                    confined_remove_path(path, self.codex_root, current)
            except Exception as exc:
                rollback_errors.append(exc)
        for snapshot in reversed(self.snapshots):
            try:
                current = confined_path_identity(
                    snapshot.path,
                    self.codex_root,
                    allow_leaf_symlink=True,
                    missing_parent_is_missing=True,
                )
                if current == snapshot.identity:
                    continue
                expected = self.committed_identities.get(snapshot.path)
                if expected is None:
                    raise InstallError(
                        f"rollback target changed without a committed installer identity; "
                        f"preserving concurrent replacement: {snapshot.path}"
                    )
                if current != expected:
                    raise InstallError(f"rollback target changed; preserving concurrent replacement: {snapshot.path}")
                if current.state != "missing":
                    confined_remove_path(snapshot.path, self.codex_root, current)
                confined_ensure_parent(snapshot.path, self.codex_root)
                if snapshot.kind == "file" and snapshot.backup is not None:
                    copy_file(
                        snapshot.backup,
                        snapshot.path,
                        dry_run=False,
                        codex_root=self.codex_root,
                        source_root=self.temp_dir or snapshot.backup.parent,
                        source_expected=snapshot.backup_identity,
                    )
                elif snapshot.kind == "directory" and snapshot.backup is not None:
                    confined_replace_tree(
                        snapshot.backup,
                        snapshot.path,
                        self.codex_root,
                        MISSING_PATH_IDENTITY,
                        source_root=self.temp_dir or snapshot.backup.parent,
                        source_expected=snapshot.backup_identity,
                    )
                elif snapshot.kind == "symlink" and snapshot.link_target is not None:
                    confined_create_symlink(
                        snapshot.path,
                        self.codex_root,
                        snapshot.link_target,
                        MISSING_PATH_IDENTITY,
                    )
            except Exception as exc:
                rollback_errors.append(exc)
        for parent in sorted(self.created_parents, key=lambda path: len(path.parts), reverse=True):
            try:
                if parent == self.codex_root:
                    if parent.is_symlink():
                        raise InstallError("unsafe rollback target: Codex home became a symlink")
                    continue
                current = confined_path_identity(
                    parent,
                    self.codex_root,
                    allow_leaf_symlink=True,
                    missing_parent_is_missing=True,
                )
                if current.kind == "directory" and current.digest == EMPTY_DIRECTORY_DIGEST:
                    confined_rmdir(parent, self.codex_root, current)
            except Exception as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            details = "; ".join(str(error) for error in rollback_errors)
            raise InstallError(f"install rollback was incomplete: {details}")

    def current_temp_root_identity(self) -> ConfinedPathIdentity:
        if self.temp_dir is None or self.temp_identity is None:
            raise InstallError("transaction root identity is unavailable")
        current = confined_path_identity(
            self.temp_dir,
            self.temp_dir.parent,
            allow_leaf_symlink=True,
        )
        if current.kind != "directory" or (current.device, current.inode) != (
            self.temp_identity.device,
            self.temp_identity.inode,
        ):
            raise InstallError("transaction root identity changed; preserving replacement")
        return current

    def cleanup_temp_dir(self) -> None:
        if self.temp_dir is None:
            return
        transaction_root = self.temp_dir
        self.current_temp_root_identity()
        cleanup_errors: list[Exception] = []
        for snapshot in self.snapshots:
            if snapshot.backup is None:
                continue
            try:
                current = confined_path_identity(
                    snapshot.backup,
                    transaction_root,
                    allow_leaf_symlink=True,
                    missing_parent_is_missing=True,
                )
                if current.state == "missing":
                    continue
                if snapshot.backup_identity is None or current != snapshot.backup_identity:
                    raise InstallError(
                        f"transaction backup identity changed; preserving replacement: {snapshot.backup}"
                    )
                confined_remove_path(snapshot.backup, transaction_root, current)
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            details = "; ".join(str(error) for error in cleanup_errors)
            raise InstallError(f"transaction cleanup was incomplete: {details}")

        current_root = self.current_temp_root_identity()
        names = confined_directory_names(transaction_root, transaction_root.parent, current_root)
        expected_names = {"journal.json"} if self.journal_identity is not None else set()
        unexpected_names = tuple(sorted(set(names) - expected_names))
        if unexpected_names:
            raise InstallError(
                "transaction root contains unexpected entries; preserving root: " + ", ".join(unexpected_names)
            )
        if self.journal_identity is not None:
            journal_path = transaction_root / "journal.json"
            current_journal = confined_path_identity(
                journal_path,
                transaction_root,
                allow_leaf_symlink=True,
            )
            if current_journal != self.journal_identity:
                raise InstallError("transaction journal identity changed; preserving replacement")
            confined_remove_path(journal_path, transaction_root, current_journal)

        current_root = self.current_temp_root_identity()
        if confined_directory_names(transaction_root, transaction_root.parent, current_root):
            raise InstallError("transaction root is not empty; preserving root")
        confined_rmdir(transaction_root, transaction_root.parent, current_root)
        self.temp_dir = None
        self.temp_identity = None
        self.journal_identity = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is not None:
            self.rollback()
        self.cleanup_temp_dir()
        return False


def codex_root_fingerprint(codex_root: Path) -> str:
    normalized = str(codex_root.resolve(strict=False)).encode()
    return hashlib.sha256(normalized).hexdigest()


def safe_journal_relative(value: Any, *, allow_root: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise InstallError("transaction journal contains an invalid path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise InstallError("transaction journal path escapes its root")
    if relative == Path(".") and not allow_root:
        raise InstallError("transaction journal path cannot target the Codex root")
    return relative


def recovered_path(
    root: Path,
    value: Any,
    *,
    allow_root: bool = False,
    allow_leaf_symlink: bool,
) -> Path:
    relative = safe_journal_relative(value, allow_root=allow_root)
    if relative == Path("."):
        if root.is_symlink():
            raise InstallError("transaction journal root became a symlink")
        return root
    try:
        return confined_path(root, relative.as_posix(), allow_leaf_symlink=allow_leaf_symlink)
    except ValueError as exc:
        raise InstallError(f"transaction journal path is unsafe: {value}") from exc


def transaction_from_journal(
    codex_root: Path,
    transaction_root: Path,
    journal: dict[str, Any],
    transaction_root_identity: ConfinedPathIdentity,
    journal_identity: ConfinedPathIdentity,
) -> InstallMutation:
    snapshots_value = journal.get("snapshots")
    created_paths_value = journal.get("created_paths", [])
    created_parents_value = journal.get("created_parents", [])
    committed_value = journal.get("committed", [])
    if not isinstance(snapshots_value, list):
        raise InstallError("transaction journal snapshots are invalid")
    if (
        not isinstance(created_paths_value, list)
        or not isinstance(created_parents_value, list)
        or not isinstance(committed_value, list)
    ):
        raise InstallError("transaction journal created paths are invalid")

    transaction = InstallMutation(codex_root, ())
    transaction.temp_dir = transaction_root
    transaction.temp_identity = transaction_root_identity
    transaction.journal_identity = journal_identity
    raw_root_identity = journal.get("transaction_root_identity")
    if (
        not isinstance(raw_root_identity, dict)
        or raw_root_identity.get("device") != transaction_root_identity.device
        or raw_root_identity.get("inode") != transaction_root_identity.inode
    ):
        raise InstallError("transaction root identity changed")
    for raw_snapshot in snapshots_value:
        if not isinstance(raw_snapshot, dict):
            raise InstallError("transaction journal snapshot is invalid")
        snapshot_path = recovered_path(
            codex_root,
            raw_snapshot.get("path"),
            allow_leaf_symlink=True,
        )
        kind = raw_snapshot.get("kind")
        if kind not in {"missing", "file", "directory", "symlink"}:
            raise InstallError("transaction journal snapshot kind is invalid")
        backup_value = raw_snapshot.get("backup")
        backup = None
        backup_identity = None
        if backup_value is not None:
            backup = recovered_path(
                transaction_root,
                backup_value,
                allow_leaf_symlink=False,
            )
            if not backup.exists():
                raise InstallError("transaction journal backup is invalid")
            raw_backup_identity = raw_snapshot.get("backup_identity")
            if raw_backup_identity is not None:
                if not isinstance(raw_backup_identity, dict):
                    raise InstallError("transaction journal backup identity is invalid")
                backup_state = raw_backup_identity.get("state")
                backup_kind = raw_backup_identity.get("kind")
                backup_digest = raw_backup_identity.get("digest")
                backup_numeric = tuple(raw_backup_identity.get(field) for field in ("device", "inode", "mode", "size"))
                if (
                    backup_state != "available"
                    or backup_kind not in {"file", "directory"}
                    or not isinstance(backup_digest, str)
                    or not all(isinstance(value, int) for value in backup_numeric)
                ):
                    raise InstallError("transaction journal backup identity is invalid")
                backup_identity = ConfinedPathIdentity(
                    backup_state,
                    backup_kind,
                    *backup_numeric,
                    backup_digest,
                )
                if confined_path_identity(backup, transaction_root) != backup_identity:
                    raise InstallError("transaction journal backup identity changed")
            else:
                backup_identity = confined_path_identity(backup, transaction_root)
        link_target = raw_snapshot.get("link_target")
        if link_target is not None and not isinstance(link_target, str):
            raise InstallError("transaction journal symlink target is invalid")
        mode = raw_snapshot.get("mode")
        digest = raw_snapshot.get("digest")
        if kind == "missing":
            identity = MISSING_PATH_IDENTITY
        else:
            state = raw_snapshot.get("state")
            device = raw_snapshot.get("device")
            inode = raw_snapshot.get("inode")
            size = raw_snapshot.get("size")
            if not isinstance(mode, int):
                if backup is not None:
                    mode = backup.lstat().st_mode & 0o7777
                else:
                    mode = 0o777
            if not isinstance(digest, str):
                if backup is not None:
                    backup_kind, digest = path_kind_and_digest(backup)
                    if backup_kind != kind:
                        raise InstallError("transaction journal backup kind is invalid")
                elif kind == "symlink" and link_target is not None:
                    digest = sha256_bytes(link_target.encode())
                else:
                    raise InstallError("transaction journal snapshot digest is invalid")
            if state == "available" and not all(isinstance(value, int) for value in (device, inode, size)):
                raise InstallError("transaction journal snapshot identity is invalid")
            identity = ConfinedPathIdentity(
                "available",
                kind,
                device if isinstance(device, int) else None,
                inode if isinstance(inode, int) else None,
                mode=mode,
                size=size if isinstance(size, int) else None,
                digest=digest,
            )
        transaction.snapshots.append(
            PathSnapshot(
                snapshot_path,
                kind,
                backup,
                link_target,
                identity,
                backup_identity,
            )
        )

    transaction.created_paths = [
        recovered_path(codex_root, value, allow_leaf_symlink=True) for value in created_paths_value
    ]
    transaction.created_parents = {
        recovered_path(
            codex_root,
            value,
            allow_root=True,
            allow_leaf_symlink=False,
        )
        for value in created_parents_value
    }
    for raw_committed in committed_value:
        if not isinstance(raw_committed, dict):
            raise InstallError("transaction journal committed identity is invalid")
        committed_path = recovered_path(
            codex_root,
            raw_committed.get("path"),
            allow_leaf_symlink=True,
        )
        state = raw_committed.get("state")
        kind = raw_committed.get("kind")
        if state not in {"available", "missing"}:
            raise InstallError("transaction journal committed state is invalid")
        if state == "available" and kind not in {"file", "directory", "symlink"}:
            raise InstallError("transaction journal committed kind is invalid")
        numeric_fields = ("device", "inode", "mode", "size")
        if any(
            raw_committed.get(field) is not None and not isinstance(raw_committed.get(field), int)
            for field in numeric_fields
        ):
            raise InstallError("transaction journal committed identity is invalid")
        digest = raw_committed.get("digest")
        if digest is not None and not isinstance(digest, str):
            raise InstallError("transaction journal committed digest is invalid")
        transaction.committed_identities[committed_path] = ConfinedPathIdentity(
            state,
            kind,
            raw_committed.get("device"),
            raw_committed.get("inode"),
            raw_committed.get("mode"),
            raw_committed.get("size"),
            digest,
        )
    return transaction


def recover_pending_transactions(codex_root: Path, *, dry_run: bool = False) -> int:
    parent = codex_root.parent
    recovered = 0
    fingerprint = codex_root_fingerprint(codex_root)
    for transaction_root, enumerated_identity in confined_list_private_directories(parent, TRANSACTION_PREFIX):
        journal_path = transaction_root / "journal.json"
        current_enumerated = confined_path_identity(
            transaction_root,
            parent,
            allow_leaf_symlink=True,
        )
        if current_enumerated != enumerated_identity:
            raise InstallError("transaction root changed after enumeration")
        journal_identity = confined_path_identity(
            journal_path,
            transaction_root,
            allow_leaf_symlink=True,
            missing_parent_is_missing=True,
        )
        if journal_identity.kind != "file":
            continue
        try:
            initial_root_identity = confined_path_identity(
                transaction_root,
                parent,
                allow_leaf_symlink=True,
            )
            journal = json.loads(confined_read_bytes(journal_path, transaction_root, journal_identity))
            transaction_root_identity = confined_path_identity(
                transaction_root,
                parent,
                allow_leaf_symlink=True,
            )
            if (
                transaction_root_identity.device,
                transaction_root_identity.inode,
            ) != (
                initial_root_identity.device,
                initial_root_identity.inode,
            ):
                raise InstallError("transaction root identity changed while reading journal")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise InstallError(f"cannot read pending transaction journal: {journal_path}") from exc
        if not isinstance(journal, dict) or journal.get("schema") != TRANSACTION_JOURNAL_SCHEMA:
            continue
        if journal.get("root_fingerprint") != fingerprint:
            continue
        transaction = transaction_from_journal(
            codex_root,
            transaction_root,
            journal,
            transaction_root_identity,
            journal_identity,
        )
        if not dry_run:
            transaction.rollback()
            transaction.cleanup_temp_dir()
        recovered += 1
    return recovered


def ensure_event_hook(
    data: dict[str, Any],
    event_name: str,
    hook_command: str,
    status_message: str,
    *,
    owned_commands: tuple[str, ...] = (),
) -> str:
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault(event_name, [])
    if not isinstance(groups, list):
        raise ValueError(f"hooks.{event_name} must be a list")

    existing_items = lazy_router_hook_items(
        data,
        event_name,
        owned_commands=(*owned_commands, hook_command),
    )
    if len(existing_items) > 1:
        raise InstallError(f"multiple lazy-skill-router {event_name} hook entries found; remove duplicates first")
    if existing_items:
        existing = existing_items[0]
        if existing.get("command") != hook_command:
            existing["command"] = hook_command
            return "updated"
        return "unchanged"

    target_group = first_hook_group(groups)
    target_group["hooks"].append(
        {
            "type": "command",
            "command": hook_command,
            "timeout": 5,
            "statusMessage": status_message,
        }
    )
    return "added"


def ensure_user_prompt_hook(
    data: dict[str, Any],
    hook_command: str,
    *,
    owned_commands: tuple[str, ...] = (),
) -> str:
    return ensure_event_hook(
        data,
        "UserPromptSubmit",
        hook_command,
        "Routing prompt to relevant skills",
        owned_commands=owned_commands,
    )


def ensure_stop_hook(
    data: dict[str, Any],
    hook_command: str,
    *,
    owned_commands: tuple[str, ...] = (),
) -> str:
    return ensure_event_hook(
        data,
        "Stop",
        hook_command,
        "Recording lazy-skill-router turn completion",
        owned_commands=owned_commands,
    )


def remove_event_router_hooks(
    data: dict[str, Any],
    event_name: str,
    *,
    owned_commands: tuple[str, ...],
) -> int:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    groups = hooks.get(event_name)
    if not isinstance(groups, list):
        return 0
    removed = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        kept = []
        for item in group["hooks"]:
            if isinstance(item, dict) and command_matches_any(item.get("command"), owned_commands):
                removed += 1
            else:
                kept.append(item)
        group["hooks"] = kept
    return removed


def configure_stop_hook(
    data: dict[str, Any],
    hook_command: str,
    *,
    enabled: bool,
    owned_commands: tuple[str, ...] = (),
) -> str:
    if enabled:
        return ensure_stop_hook(data, hook_command, owned_commands=owned_commands)
    commands = (*owned_commands, hook_command)
    return "removed" if remove_event_router_hooks(data, "Stop", owned_commands=commands) else "absent"


def event_hook_items(data: dict[str, Any], event_name: str) -> Iterator[dict[str, Any]]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    groups = hooks.get(event_name)
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        hook_items = group.get("hooks")
        if not isinstance(hook_items, list):
            continue
        for item in hook_items:
            if isinstance(item, dict):
                yield item


def user_prompt_hook_items(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from event_hook_items(data, "UserPromptSubmit")


def lazy_router_hook_items(
    data: dict[str, Any],
    event_name: str = "UserPromptSubmit",
    *,
    owned_commands: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        item for item in event_hook_items(data, event_name) if command_matches_any(item.get("command"), owned_commands)
    )


def reject_duplicate_lazy_router_hooks(
    data: dict[str, Any],
    prompt_owned_commands: tuple[str, ...],
    stop_owned_commands: tuple[str, ...],
) -> None:
    for event_name, owned_commands in (
        ("UserPromptSubmit", prompt_owned_commands),
        ("Stop", stop_owned_commands),
    ):
        if len(lazy_router_hook_items(data, event_name, owned_commands=owned_commands)) > 1:
            raise InstallError(f"multiple lazy-skill-router {event_name} hook entries found; remove duplicates first")


def first_hook_group(groups: list[Any]) -> dict[str, Any]:
    if not groups:
        groups.append({"hooks": []})

    target_group = next(
        (group for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)), None
    )
    if target_group is None:
        target_group = {"hooks": []}
        groups.append(target_group)
    return target_group


def canonical_hook_argv(hook_path: Path, routes_path: Path) -> tuple[str, ...]:
    return ("python3", str(hook_path), "--config", str(routes_path))


def canonical_stop_hook_argv(hook_path: Path, routes_path: Path) -> tuple[str, ...]:
    return (*canonical_hook_argv(hook_path, routes_path), "--hook-event", "stop")


def install_hook_command(hook_path: Path, routes_path: Path) -> str:
    return canonical_hook_command(hook_path, routes_path)


def install_stop_hook_command(hook_path: Path, routes_path: Path) -> str:
    return canonical_hook_command(hook_path, routes_path, stop=True)


def owned_hook_commands(
    manifest: InstallManifestSnapshot,
    event_name: str,
    canonical_command: str,
) -> tuple[str, ...]:
    commands = [canonical_command]
    if manifest.state == "available":
        registered = registered_hook_command(manifest.registration, event_name)
        if registered is not None:
            commands.append(registered)
    return tuple(dict.fromkeys(commands))


def planned_hooks_update(
    data: dict[str, Any],
    hook_command: str,
    stop_hook_command: str,
    *,
    measurement_enabled: bool,
    prompt_owned_commands: tuple[str, ...] = (),
    stop_owned_commands: tuple[str, ...] = (),
) -> tuple[dict[str, Any], str, str]:
    planned: dict[str, Any] = copy.deepcopy(data)
    prompt_state = ensure_user_prompt_hook(planned, hook_command, owned_commands=prompt_owned_commands)
    stop_state = configure_stop_hook(
        planned,
        stop_hook_command,
        enabled=measurement_enabled,
        owned_commands=stop_owned_commands,
    )
    return planned, prompt_state, stop_state


def hooks_json_diff(current: dict[str, Any], planned: dict[str, Any], path: Path) -> tuple[str, ...]:
    before = json.dumps(current, indent=2, ensure_ascii=False).splitlines()
    after = json.dumps(planned, indent=2, ensure_ascii=False).splitlines()
    return tuple(difflib.unified_diff(before, after, fromfile=str(path), tofile=f"{path} (planned)", lineterm=""))


def nearest_existing_destination_root(path: Path) -> Path:
    current = path.absolute().parent
    while not current.exists() and current != current.parent:
        current = current.parent
    if not current.is_dir():
        raise InstallError(f"install destination has no safe existing parent: {path}")
    return current


def copy_file(
    source: Path,
    destination: Path,
    *,
    dry_run: bool,
    codex_root: Path | None = None,
    mutation: InstallMutation | None = None,
    source_root: Path | None = None,
    source_expected: ConfinedPathIdentity | None = None,
) -> ConfinedPathIdentity | None:
    if dry_run:
        return None
    if codex_root is not None:
        checked_install_path(codex_root, destination, allow_leaf_symlink=False)
    elif destination.is_symlink():
        raise InstallError(f"refusing to overwrite symlink: {destination}")
    trusted_source_root = (
        source_root
        if source_root is not None
        else PROJECT_ROOT
        if source == PROJECT_ROOT or PROJECT_ROOT in source.parents
        else source.parent
    )
    try:
        source_identity = (
            source_expected if source_expected is not None else confined_path_identity(source, trusted_source_root)
        )
    except (OSError, ValueError) as exc:
        raise InstallError(f"install source is unsafe: {source}") from exc
    if source_identity.kind != "file":
        raise InstallError(f"install source is not a trusted regular file: {source}")
    source_bytes = confined_read_bytes(source, trusted_source_root, source_identity)
    destination_root = codex_root if codex_root is not None else nearest_existing_destination_root(destination)
    confined_ensure_parent(destination, destination_root)
    expected = confined_path_identity(destination, destination_root)
    installed_identity = confined_atomic_write_bytes(
        destination,
        source_bytes,
        destination_root,
        expected,
        mode=source_identity.mode,
    )
    if mutation is not None:
        mutation.record_committed(destination, installed_identity)
    return installed_identity


def copy_hook_runtime(
    hook_path: Path,
    *,
    dry_run: bool,
    codex_root: Path | None = None,
    mutation: InstallMutation | None = None,
) -> None:
    copy_file(HOOK_SOURCE, hook_path, dry_run=dry_run, codex_root=codex_root, mutation=mutation)
    copy_file(
        CORE_SOURCE,
        hook_path.parent / "lazy_skill_router_core.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        COMMON_SOURCE,
        hook_path.parent / "lazy_skill_router_common.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        LOGGING_SOURCE,
        hook_path.parent / "lazy_skill_router_logging.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        SCORING_SOURCE,
        hook_path.parent / "lazy_skill_router_scoring.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        CONTRACTS_SOURCE,
        hook_path.parent / "lazy_skill_router_contracts.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        INVENTORY_SOURCE,
        hook_path.parent / "lazy_skill_router_inventory.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        POLICY_IR_SOURCE,
        hook_path.parent / "lazy_skill_router_policy_ir.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        ACTIVATION_SOURCE,
        hook_path.parent / "lazy_skill_router_activation.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        CAPABILITY_INDEX_SOURCE,
        hook_path.parent / "lazy_skill_router_capability_index.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )
    copy_file(
        RETRIEVAL_SOURCE,
        hook_path.parent / "lazy_skill_router_retrieval.py",
        dry_run=dry_run,
        codex_root=codex_root,
        mutation=mutation,
    )


def copy_skill(
    destination: Path,
    *,
    force: bool,
    dry_run: bool,
    codex_root: Path | None = None,
    mutation: InstallMutation | None = None,
) -> str:
    exists = destination.exists() or destination.is_symlink()
    if exists and not force:
        return "would keep existing skill" if dry_run else "kept existing skill"
    if codex_root is not None:
        checked_install_path(codex_root, destination, allow_leaf_symlink=False)
    elif destination.is_symlink():
        raise InstallError(f"refusing to overwrite symlink: {destination}")
    if dry_run:
        return "would upgrade existing skill" if exists else "would copy skill"
    destination_root = codex_root if codex_root is not None else nearest_existing_destination_root(destination)
    confined_ensure_parent(destination, destination_root)
    expected = confined_path_identity(destination, destination_root)
    installed_identity = confined_replace_tree(
        SKILL_SOURCE,
        destination,
        destination_root,
        expected,
        source_root=PROJECT_ROOT,
    )
    if mutation is not None:
        mutation.record_committed(destination, installed_identity)
    return "upgraded existing skill" if exists else "copied skill"


def write_skill_inventory(
    destination: Path,
    codex_root: Path,
    agents_root: Path,
    *,
    dry_run: bool,
    mutation: InstallMutation | None = None,
) -> dict[str, Any] | None:
    if dry_run:
        return None
    checked_install_path(codex_root, destination, allow_leaf_symlink=False)
    confined_ensure_parent(destination, codex_root)
    expected = confined_path_identity(destination, codex_root)
    records = scan_installed_skills(codex_root, agents_root)
    manifest = build_inventory_manifest(records, codex_root, agents_root)
    host_catalog_path = destination.with_name("host-catalog.json")
    host_catalog = load_host_catalog(host_catalog_path)
    if host_catalog.state == "invalid":
        reason = ", ".join(host_catalog.reason_codes) or "invalid"
        raise InstallError(f"cannot use invalid host catalog: {reason}")
    if host_catalog.state == "available":
        manifest = reconcile_inventory(manifest, host_catalog)
    installed_identity = write_json(
        destination,
        manifest,
        managed_root=codex_root,
        expected=expected,
    )
    if mutation is not None and installed_identity is not None:
        mutation.record_committed(destination, installed_identity)
    return manifest


def write_capability_index(
    destination: Path,
    inventory_path: Path,
    codex_root: Path,
    *,
    dry_run: bool,
    inventory_data: dict[str, Any] | None = None,
    mutation: InstallMutation | None = None,
) -> None:
    if dry_run:
        return
    checked_install_path(codex_root, destination, allow_leaf_symlink=False)
    confined_ensure_parent(destination, codex_root)
    expected = confined_path_identity(destination, codex_root)
    if inventory_data is None:
        inventory_identity = confined_path_identity(inventory_path, codex_root)
        raw_inventory = confined_read_bytes(inventory_path, codex_root, inventory_identity)
        parsed_inventory = json.loads(raw_inventory)
        if not isinstance(parsed_inventory, dict):
            raise InstallError("generated inventory manifest is not an object")
        inventory_data = parsed_inventory
    skills = inventory_data.get("skills")
    revision = inventory_data.get("revision")
    if (
        not isinstance(skills, list)
        or not all(isinstance(skill, dict) for skill in skills)
        or not isinstance(revision, str)
        or revision != inventory_revision(skills)
    ):
        raise InstallError("generated inventory manifest is invalid")
    inventory = InventorySnapshot("available", revision, tuple(skills))
    index = build_capability_index(inventory)
    installed_identity = write_json_atomic(
        destination,
        index,
        managed_root=codex_root,
        expected=expected,
    )
    if mutation is not None and installed_identity is not None:
        mutation.record_committed(destination, installed_identity)


def write_install_json(
    codex_root: Path,
    destination: Path,
    data: dict[str, Any],
    *,
    expected: ConfinedPathIdentity | None = None,
    mutation: InstallMutation | None = None,
) -> ConfinedPathIdentity:
    checked_install_path(codex_root, destination, allow_leaf_symlink=False)
    expected_identity = expected if expected is not None else confined_path_identity(destination, codex_root)
    installed_identity = write_json(
        destination,
        data,
        managed_root=codex_root,
        expected=expected_identity,
    )
    if installed_identity is None:
        raise InstallError(f"confined install write did not return an identity: {destination}")
    if mutation is not None:
        mutation.record_committed(destination, installed_identity)
    return installed_identity


def previous_ownership(snapshot: InstallManifestSnapshot, relative_path: str, default: str) -> str:
    if snapshot.state != "available":
        return default
    for artifact in snapshot.artifacts:
        if artifact.get("path") == relative_path and artifact.get("ownership") in {"managed", "generated", "preserved"}:
            return str(artifact["ownership"])
    return default


def can_auto_upgrade_skill(snapshot: InstallManifestSnapshot, codex_root: Path) -> bool:
    if snapshot.state != "available":
        return False
    records = tuple(
        artifact for artifact in snapshot.artifacts if artifact.get("path") == "skills/personal-skill-router"
    )
    if len(records) != 1 or records[0].get("kind") != "directory" or records[0].get("ownership") != "managed":
        return False
    return artifact_state(codex_root, records[0]) == "matching"


def install_artifacts(
    hook_destination: Path,
    routes_destination: Path,
    inventory_destination: Path,
    capability_index_destination: Path,
    skill_destination: Path,
    *,
    route_ownership: str,
    skill_ownership: str,
) -> tuple[tuple[Path, str], ...]:
    hook_dir = hook_destination.parent
    return (
        (hook_destination, "managed"),
        (hook_dir / "lazy_skill_router_core.py", "managed"),
        (hook_dir / "lazy_skill_router_common.py", "managed"),
        (hook_dir / "lazy_skill_router_logging.py", "managed"),
        (hook_dir / "lazy_skill_router_scoring.py", "managed"),
        (hook_dir / "lazy_skill_router_contracts.py", "managed"),
        (hook_dir / "lazy_skill_router_inventory.py", "managed"),
        (hook_dir / "lazy_skill_router_policy_ir.py", "managed"),
        (hook_dir / "lazy_skill_router_activation.py", "managed"),
        (hook_dir / "lazy_skill_router_capability_index.py", "managed"),
        (hook_dir / "lazy_skill_router_retrieval.py", "managed"),
        (inventory_destination, "generated"),
        (capability_index_destination, "generated"),
        (routes_destination, route_ownership),
        (skill_destination, skill_ownership),
    )


def route_errors(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(finding.message for finding in validate_config(config) if finding.severity == "ERROR")


def installed_names_for_install(codex_root: Path, agents_root: Path) -> set[str]:
    try:
        names = installed_skill_names(codex_root, agents_root)
    except TemplateError as exc:
        raise InstallError(str(exc)) from exc
    names.add("personal-skill-router")
    return names


def generated_routes(template_path: Path, codex_root: Path, agents_root: Path) -> dict[str, Any]:
    result = generate_config(
        load_json_object(template_path, "template root"),
        installed_names_for_install(codex_root, agents_root),
    )
    if generated_route_count(result) == 0:
        raise InstallError("generated 0 routes; no installed primary candidates matched")
    errors = route_errors(result.config)
    if errors:
        raise InstallError("generated routes failed validation: " + "; ".join(errors))
    return result.config


def validate_routes_config(config: dict[str, Any], path: Path) -> None:
    errors = route_errors(config)
    if errors:
        raise InstallError(f"routes failed validation at {path}: " + "; ".join(errors))


def apply_router_notice_setting(config: dict[str, Any], enabled: bool | None) -> bool:
    if enabled is None:
        return False
    display = config.get("display")
    if not isinstance(display, dict):
        display = {}
        config["display"] = display
    if display.get("showRouterNotice") is enabled:
        return False
    display["showRouterNotice"] = enabled
    return True


def apply_activation_mode(config: dict[str, Any], mode: str | None) -> bool:
    if mode is None:
        return False
    activation = config.get("activation")
    if not isinstance(activation, dict):
        activation = {}
        config["activation"] = activation
    if activation.get("mode") == mode:
        return False
    activation["mode"] = mode
    return True


def apply_measurement_setting(config: dict[str, Any], enabled: bool | None) -> bool:
    if enabled is None:
        return False
    logging_config = config.get("logging")
    if not isinstance(logging_config, dict):
        logging_config = {}
        config["logging"] = logging_config
    if logging_config.get("enabled") is enabled:
        return False
    logging_config["enabled"] = enabled
    return True


def smoke_hook(hook_path: Path, route_path: Path, prompt: str) -> None:
    event = json.dumps({"prompt": prompt}, ensure_ascii=False)
    try:
        completed = subprocess.run(
            canonical_hook_argv(hook_path, route_path),
            check=False,
            capture_output=True,
            input=event,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError("hook smoke test timed out") from exc
    if completed.returncode != 0:
        raise InstallError(f"hook smoke test failed with exit code {completed.returncode}: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(f"hook smoke test did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError("hook smoke test returned unexpected JSON")
    output = payload.get("hookSpecificOutput")
    if not isinstance(output, dict):
        raise InstallError("hook smoke test returned unexpected hook envelope")
    if output.get("hookEventName") != "UserPromptSubmit":
        raise InstallError("hook smoke test returned unexpected hook event")
    if not output.get("additionalContext"):
        raise InstallError("hook smoke test returned empty additional context")


def smoke_stop_hook(hook_path: Path, route_path: Path) -> None:
    event = json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "lazy-skill-router-smoke-session",
            "turn_id": "lazy-skill-router-smoke-turn",
            "stop_hook_active": False,
        }
    )
    try:
        completed = subprocess.run(
            canonical_stop_hook_argv(hook_path, route_path),
            check=False,
            capture_output=True,
            input=event,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Stop hook smoke test timed out") from exc
    if completed.returncode != 0:
        raise InstallError(
            f"Stop hook smoke test failed with exit code {completed.returncode}: {completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(f"Stop hook smoke test did not return JSON: {exc}") from exc
    if payload != {}:
        raise InstallError("Stop hook smoke test returned unexpected JSON")


def first_route_primary(route_config: dict[str, Any]) -> str:
    parsed = parse_policy_config(route_config)
    primary = select_smoke_primary(parsed.policy)
    if primary is None:
        raise InstallError("eligible active route primary unavailable for implicit smoke probe")
    return primary


def smoke_probe_config(route_config: dict[str, Any]) -> dict[str, Any]:
    primary = first_route_primary(route_config)
    schema_version = parse_policy_config(route_config).policy.schema_version
    if schema_version == 2:
        capability = "internal-smoke-primary"
        config: dict[str, Any] = {
            "schemaVersion": 2,
            "policyVersion": "internal-smoke-probe",
            "selection": {
                "mode": "ranked",
                "maxRecommendations": 1,
                "minMatchStrength": 0.55,
                "minScoreMargin": 0.05,
            },
            "skillBindings": {capability: {"skill": primary}},
            "allowedSkills": [primary],
            "logging": {"enabled": False, "path": ""},
            "routes": [
                {
                    "id": "internal-smoke-probe",
                    "intent": "internal_smoke_probe",
                    "capabilityRequirements": {"primary": [capability]},
                    "match": {
                        "any": [
                            {
                                "id": "internal-smoke-probe.token",
                                "regex": f"^{INTERNAL_SMOKE_PROMPT}$",
                            }
                        ]
                    },
                    "lifecycle": {"state": "active"},
                }
            ],
        }
    else:
        config = {
            "allowedSkills": [primary],
            "logging": {"enabled": False, "path": ""},
            "routes": [
                {
                    "name": "internal-smoke-probe",
                    "primary": primary,
                    "supporting": [],
                    "verification": "",
                    "reason": "internal smoke probe",
                    "patterns": [f"^{INTERNAL_SMOKE_PROMPT}$"],
                }
            ],
        }
    errors = route_errors(config)
    if errors:
        raise InstallError("internal smoke probe routes failed validation: " + "; ".join(errors))
    return config


def smoke_config_for_prompt(route_config: dict[str, Any], explicit_prompt: str | None) -> tuple[dict[str, Any], str]:
    if explicit_prompt is None:
        return smoke_probe_config(route_config), INTERNAL_SMOKE_PROMPT
    staged_config: dict[str, Any] = copy.deepcopy(route_config)
    staged_config["logging"] = {"enabled": False, "path": ""}
    activation = staged_config.get("activation")
    if not isinstance(activation, dict):
        activation = {}
        staged_config["activation"] = activation
    activation["mode"] = "inject"
    return staged_config, explicit_prompt


def smoke_staged_hook(route_config: dict[str, Any], explicit_prompt: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="lazy-skill-router-install-") as temp_dir:
        staging_root = Path(temp_dir)
        hook_path = staging_root / "hooks" / "lazy_skill_router.py"
        route_path = staging_root / "lazy-skill-router" / "routes.json"
        staged_config, prompt = smoke_config_for_prompt(route_config, explicit_prompt)
        copy_hook_runtime(hook_path, dry_run=False)
        write_json(route_path, staged_config)
        smoke_hook(hook_path, route_path, prompt)
        smoke_stop_hook(hook_path, route_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install lazy-skill-router into Codex hooks.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument("--template", default=str(TEMPLATE_SOURCE), help="Candidate-based route template JSON.")
    parser.add_argument(
        "--smoke-prompt",
        default=None,
        help=("Explicit prompt used for the hook smoke test. When omitted, a temporary internal probe route is used."),
    )
    parser.add_argument("--force", action="store_true", help="Overwrite the bundled personal-skill-router skill.")
    parser.add_argument("--overwrite-routes", action="store_true", help="Overwrite an existing routes.json.")
    notice_group = parser.add_mutually_exclusive_group()
    notice_group.add_argument(
        "--show-router-notice",
        action="store_true",
        help="Ask Codex to briefly show the selected route before task-specific work.",
    )
    notice_group.add_argument(
        "--hide-router-notice",
        action="store_true",
        help="Keep route recommendations hidden from user-facing replies.",
    )
    parser.add_argument(
        "--activation-mode",
        choices=("inject", "off", "shadow"),
        help="Set automatic hook delivery mode without changing route selection rules.",
    )
    measurement_group = parser.add_mutually_exclusive_group()
    measurement_group.add_argument(
        "--enable-measurement",
        action="store_true",
        help="Enable privacy-preserving decision and completion event logging.",
    )
    measurement_group.add_argument(
        "--disable-measurement",
        action="store_true",
        help="Disable decision and completion event logging.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    template_path = Path(args.template).expanduser()
    hooks_json = codex_root / "hooks.json"
    hook_destination = codex_root / "hooks" / "lazy_skill_router.py"
    routes_destination = codex_root / "lazy-skill-router" / "routes.json"
    inventory_destination = codex_root / "lazy-skill-router" / "skills.manifest.json"
    capability_index_destination = inventory_destination.with_name(DEFAULT_CAPABILITY_INDEX_NAME)
    install_manifest_destination = codex_root / "lazy-skill-router" / "install.manifest.json"
    skill_destination = codex_root / "skills" / "personal-skill-router"

    hook_command = install_hook_command(hook_destination, routes_destination)
    stop_hook_command = install_stop_hook_command(hook_destination, routes_destination)
    actions: list[str] = []
    planned_diff: tuple[str, ...] = ()
    notice_setting = True if args.show_router_notice else False if args.hide_router_notice else None
    measurement_setting = True if args.enable_measurement else False if args.disable_measurement else None

    try:
        recovered_transactions = recover_pending_transactions(codex_root, dry_run=args.dry_run)
        if recovered_transactions:
            verb = "would recover" if args.dry_run else "recovered"
            actions.append(f"{verb} {recovered_transactions} interrupted install transaction")
        checked_install_path(codex_root, hooks_json, allow_leaf_symlink=False)
        checked_install_path(codex_root, install_manifest_destination, allow_leaf_symlink=False)
        current_hooks = load_hooks(hooks_json)
        previous_manifest = load_install_manifest(install_manifest_destination)
        if previous_manifest.state == "invalid":
            reason = ", ".join(previous_manifest.reason_codes) or "invalid"
            raise InstallError(f"cannot install from invalid ownership manifest: {reason}")
        manifest_aliases = physical_artifact_aliases(previous_manifest, codex_root)
        if manifest_aliases:
            detail = ", ".join(f"{left} = {right}" for left, right in manifest_aliases)
            raise InstallError(f"install ownership manifest contains physical aliases: {detail}")
        prompt_owned_commands = owned_hook_commands(previous_manifest, "UserPromptSubmit", hook_command)
        stop_owned_commands = owned_hook_commands(previous_manifest, "Stop", stop_hook_command)
        reject_duplicate_lazy_router_hooks(current_hooks, prompt_owned_commands, stop_owned_commands)

        if routes_destination.exists() and not args.overwrite_routes:
            route_config = load_json_object(routes_destination, "routes root")
            notice_changed = apply_router_notice_setting(route_config, notice_setting)
            activation_changed = apply_activation_mode(route_config, args.activation_mode)
            measurement_changed = apply_measurement_setting(route_config, measurement_setting)
            validate_routes_config(route_config, routes_destination)
            route_action = "keep"
        else:
            route_config = generated_routes(template_path, codex_root, agents_root)
            notice_changed = apply_router_notice_setting(route_config, notice_setting)
            activation_changed = apply_activation_mode(route_config, args.activation_mode)
            measurement_changed = apply_measurement_setting(route_config, measurement_setting)
            validate_routes_config(route_config, routes_destination)
            route_action = "generate"

        if not args.dry_run:
            smoke_staged_hook(route_config, args.smoke_prompt)
        configured_logging = route_config.get("logging")
        measurement_enabled = isinstance(configured_logging, dict) and configured_logging.get("enabled") is True

        target_paths = (
            *(
                path
                for path, _ in install_artifacts(
                    hook_destination,
                    routes_destination,
                    inventory_destination,
                    capability_index_destination,
                    skill_destination,
                    route_ownership="preserved",
                    skill_ownership="preserved",
                )
            ),
            install_manifest_destination,
            hooks_json,
        )
        mutation_context = nullcontext(None) if args.dry_run else InstallMutation(codex_root, target_paths)
        with mutation_context as mutation:
            active_mutation = mutation if isinstance(mutation, InstallMutation) else None
            copy_hook_runtime(
                hook_destination,
                dry_run=args.dry_run,
                codex_root=codex_root,
                mutation=active_mutation,
            )
            actions.append(f"copy hook {hook_destination}")
            actions.append(f"copy hook core {hook_destination.parent / 'lazy_skill_router_core.py'}")
            actions.append(f"copy hook common {hook_destination.parent / 'lazy_skill_router_common.py'}")
            actions.append(f"copy hook logging {hook_destination.parent / 'lazy_skill_router_logging.py'}")
            actions.append(f"copy hook scoring {hook_destination.parent / 'lazy_skill_router_scoring.py'}")
            actions.append(f"copy hook contracts {hook_destination.parent / 'lazy_skill_router_contracts.py'}")
            actions.append(f"copy hook inventory {hook_destination.parent / 'lazy_skill_router_inventory.py'}")
            actions.append(f"copy hook policy IR {hook_destination.parent / 'lazy_skill_router_policy_ir.py'}")
            actions.append(f"copy hook activation IR {hook_destination.parent / 'lazy_skill_router_activation.py'}")
            actions.append(
                f"copy hook capability index {hook_destination.parent / 'lazy_skill_router_capability_index.py'}"
            )
            actions.append(f"copy hook retrieval {hook_destination.parent / 'lazy_skill_router_retrieval.py'}")

            auto_upgrade_skill = can_auto_upgrade_skill(previous_manifest, codex_root)
            skill_state = copy_skill(
                skill_destination,
                force=args.force or auto_upgrade_skill,
                dry_run=args.dry_run,
                codex_root=codex_root,
                mutation=active_mutation,
            )
            actions.append(f"{skill_state} {skill_destination}")
            inventory_data = write_skill_inventory(
                inventory_destination,
                codex_root,
                agents_root,
                dry_run=args.dry_run,
                mutation=active_mutation,
            )
            actions.append(f"write skill inventory manifest {inventory_destination}")
            write_capability_index(
                capability_index_destination,
                inventory_destination,
                codex_root,
                dry_run=args.dry_run,
                inventory_data=inventory_data,
                mutation=active_mutation,
            )
            actions.append(f"write capability index {capability_index_destination}")

            if route_action == "keep":
                if (notice_changed or activation_changed or measurement_changed) and not args.dry_run:
                    write_install_json(
                        codex_root,
                        routes_destination,
                        route_config,
                        mutation=active_mutation,
                    )
                actions.append(f"keep existing routes {routes_destination}")
                actions.append(f"validate existing routes {routes_destination}")
            else:
                if not args.dry_run:
                    write_install_json(
                        codex_root,
                        routes_destination,
                        route_config,
                        mutation=active_mutation,
                    )
                actions.append(f"generate routes {routes_destination}")
                actions.append(f"validate generated routes {routes_destination}")
            if notice_changed:
                verb = "enable" if notice_setting else "disable"
                actions.append(f"{verb} visible router notice in {routes_destination}")
            if activation_changed:
                actions.append(f"set activation mode to {args.activation_mode} in {routes_destination}")
            if measurement_changed:
                verb = "enable" if measurement_setting else "disable"
                actions.append(f"{verb} measurement logging in {routes_destination}")

            route_ownership = (
                "generated"
                if route_action == "generate"
                else previous_ownership(previous_manifest, "lazy-skill-router/routes.json", "preserved")
            )
            skill_ownership = (
                "managed"
                if skill_state
                in {"copied skill", "would copy skill", "upgraded existing skill", "would upgrade existing skill"}
                else "preserved"
            )
            if args.dry_run:
                actions.append(f"would write install ownership manifest {install_manifest_destination}")
            else:
                manifest = build_install_manifest(
                    codex_root,
                    install_artifacts(
                        hook_destination,
                        routes_destination,
                        inventory_destination,
                        capability_index_destination,
                        skill_destination,
                        route_ownership=route_ownership,
                        skill_ownership=skill_ownership,
                    ),
                    hook_command,
                    stop_hook_command=stop_hook_command if measurement_enabled else None,
                )
                write_install_json(
                    codex_root,
                    install_manifest_destination,
                    manifest,
                    mutation=active_mutation,
                )
                actions.append(f"write install ownership manifest {install_manifest_destination}")

            if args.dry_run:
                actions.append(f"would smoke test hook {hook_destination}")
                actions.append(f"would smoke test Stop hook {hook_destination}")
                planned_hooks, hook_state, stop_hook_state = planned_hooks_update(
                    current_hooks,
                    hook_command,
                    stop_hook_command,
                    measurement_enabled=measurement_enabled,
                    prompt_owned_commands=prompt_owned_commands,
                    stop_owned_commands=stop_owned_commands,
                )
                planned_diff = hooks_json_diff(current_hooks, planned_hooks, hooks_json)
                if hook_state == "unchanged":
                    actions.append(f"would keep existing hook entry in {hooks_json}")
                else:
                    verb = "add" if hook_state == "added" else "update"
                    actions.append(f"would {verb} hook entry in {hooks_json}")
                if stop_hook_state == "absent":
                    actions.append(f"would keep Stop hook absent in {hooks_json}")
                elif stop_hook_state == "unchanged":
                    actions.append(f"would keep existing Stop hook entry in {hooks_json}")
                elif stop_hook_state == "removed":
                    actions.append(f"would remove Stop hook entry from {hooks_json}")
                else:
                    verb = "add" if stop_hook_state == "added" else "update"
                    actions.append(f"would {verb} Stop hook entry in {hooks_json}")
            else:
                actions.append(f"smoke test hook {hook_destination}")
                actions.append(f"smoke test Stop hook {hook_destination}")
                data = load_hooks(hooks_json)
                hook_state = ensure_user_prompt_hook(data, hook_command, owned_commands=prompt_owned_commands)
                stop_hook_state = configure_stop_hook(
                    data,
                    stop_hook_command,
                    enabled=measurement_enabled,
                    owned_commands=stop_owned_commands,
                )
                if hook_state in {"added", "updated"} or stop_hook_state in {"added", "updated", "removed"}:
                    checked_install_path(codex_root, hooks_json, allow_leaf_symlink=False)
                    hooks_identity = confined_path_identity(hooks_json, codex_root)
                    backup = backup_file(hooks_json, codex_root)
                    if isinstance(mutation, InstallMutation):
                        mutation.track_created(backup)
                    write_install_json(
                        codex_root,
                        hooks_json,
                        data,
                        expected=hooks_identity,
                        mutation=active_mutation,
                    )
                    if backup:
                        actions.append(f"backup {backup}")
                    actions.append(f"{hook_state} hook entry in {hooks_json}")
                else:
                    actions.append(f"kept existing hook entry in {hooks_json}")
                if stop_hook_state in {"added", "updated", "removed"}:
                    actions.append(f"{stop_hook_state} Stop hook entry in {hooks_json}")
                elif stop_hook_state == "absent":
                    actions.append(f"kept Stop hook absent in {hooks_json}")
                else:
                    actions.append(f"kept existing Stop hook entry in {hooks_json}")
    except (InstallError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("lazy-skill-router install summary:")
    for action in actions:
        print(f"- {action}")
    if args.dry_run:
        print()
        print("Planned hooks.json diff:")
        if planned_diff:
            for line in planned_diff:
                print(line)
        else:
            print("(no changes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
