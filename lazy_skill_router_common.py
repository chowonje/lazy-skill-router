from __future__ import annotations

import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

MAX_ROUTABLE_PROMPT_CHARS: Final = 4096


@dataclass(frozen=True)
class ConfinedPathIdentity:
    state: str
    kind: str | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    digest: str | None = None


@dataclass(frozen=True)
class ConfinedStagedWrite:
    path: Path
    managed_root: Path
    parent_fd: int
    destination_name: str
    temp_name: str
    expected: ConfinedPathIdentity
    temp_identity: ConfinedPathIdentity


@dataclass(frozen=True)
class ConfinedRemovalEntry:
    name: str
    identity: ConfinedPathIdentity
    children: tuple[ConfinedRemovalEntry, ...] = ()


@dataclass(frozen=True)
class ConfinedQuarantinedPath:
    wrapper_name: str
    wrapper_fd: int
    original_name: str
    target_name: str
    expected: ConfinedPathIdentity


MISSING_PATH_IDENTITY: Final = ConfinedPathIdentity("missing")
EMPTY_DIRECTORY_DIGEST: Final = "sha256:" + hashlib.sha256(b"[]").hexdigest()


def _ambient_trusted_boundary(path: Path) -> Path:
    candidates = (
        Path.cwd().absolute(),
        Path.home().absolute(),
        Path(tempfile.gettempdir()).absolute(),
    )
    boundaries = []
    for candidate in candidates:
        try:
            path.relative_to(candidate)
        except ValueError:
            continue
        boundaries.append(candidate)
    return max(boundaries, key=lambda item: len(item.parts)) if boundaries else Path(path.anchor)


def _open_directory_chain(boundary: Path, relative: Path, error_message: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(boundary, os.O_RDONLY | directory | nofollow)
    except OSError as exc:
        raise ValueError(error_message) from exc
    try:
        for part in relative.parts:
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise ValueError(error_message) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def trusted_write_boundary(path: Path, managed_root: Path) -> Path:
    target = path.absolute()
    managed = managed_root.absolute()
    candidates = (managed, Path.cwd().absolute(), Path.home().absolute(), Path(tempfile.gettempdir()).absolute())
    boundaries: list[Path] = []
    for candidate in candidates:
        try:
            target.relative_to(candidate)
        except ValueError:
            continue
        boundaries.append(candidate)
    return max(boundaries, key=lambda item: len(item.parts)) if boundaries else Path(target.anchor)


def _open_confined_parent(
    path: Path,
    managed_root: Path,
    *,
    create_parents: bool,
    allow_leaf_symlink: bool = False,
) -> tuple[int, str, tuple[Path, ...]]:
    target = path.absolute()
    if not allow_leaf_symlink:
        ensure_safe_write_target(target, managed_root)
    boundary = trusted_write_boundary(target, managed_root)
    relative = target.relative_to(boundary)
    if not relative.parts:
        raise ValueError("confined mutation target cannot be its trusted boundary")
    ambient = _ambient_trusted_boundary(boundary)
    descriptor = _open_directory_chain(
        ambient,
        boundary.relative_to(ambient),
        "trusted write boundary has a symlinked parent",
    )
    created: list[Path] = []
    current = boundary
    try:
        for part in relative.parts[:-1]:
            current /= part
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create_parents:
                    raise ValueError("confined mutation parent is missing") from None
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                created.append(current)
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise ValueError("confined mutation parent is unsafe") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, relative.name, tuple(created)
    except Exception:
        os.close(descriptor)
        raise


def _hash_open_file(descriptor: int, expected_stat: os.stat_result) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    final_stat = os.fstat(descriptor)
    if (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns) != (
        expected_stat.st_dev,
        expected_stat.st_ino,
        expected_stat.st_size,
        expected_stat.st_mtime_ns,
    ):
        raise ValueError("confined file changed while hashing")
    return "sha256:" + digest.hexdigest()


def _identity_from_open_regular_file(descriptor: int) -> ConfinedPathIdentity:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("opened temporary path is not a regular file")
    original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = _hash_open_file(descriptor, opened)
    os.lseek(descriptor, original_offset, os.SEEK_SET)
    return ConfinedPathIdentity(
        "available",
        "file",
        opened.st_dev,
        opened.st_ino,
        stat.S_IMODE(opened.st_mode),
        opened.st_size,
        digest,
    )


def _directory_plan_from_fd(
    directory_fd: int,
) -> tuple[tuple[ConfinedRemovalEntry, ...], list[dict[str, str]], str]:
    initial_stat = os.fstat(directory_fd)
    initial_names = sorted(os.listdir(directory_fd))
    plan: list[ConfinedRemovalEntry] = []
    entries: list[dict[str, str]] = []
    for child_name in initial_names:
        child_stat = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(child_stat.st_mode):
            target = os.readlink(child_name, dir_fd=directory_fd)
            digest = "sha256:" + hashlib.sha256(target.encode()).hexdigest()
            identity = ConfinedPathIdentity(
                "available",
                "symlink",
                child_stat.st_dev,
                child_stat.st_ino,
                stat.S_IMODE(child_stat.st_mode),
                child_stat.st_size,
                digest,
            )
            entries.append({"path": child_name, "kind": "symlink", "digest": digest})
            plan.append(ConfinedRemovalEntry(child_name, identity))
        elif stat.S_ISREG(child_stat.st_mode):
            identity = _identity_at(directory_fd, child_name)
            entries.append({"path": child_name, "kind": "file", "digest": str(identity.digest)})
            plan.append(ConfinedRemovalEntry(child_name, identity))
        elif stat.S_ISDIR(child_stat.st_mode):
            child_fd = os.open(
                child_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (child_stat.st_dev, child_stat.st_ino):
                    raise ValueError("confined directory entry changed while opening")
                children, child_entries, child_digest = _directory_plan_from_fd(child_fd)
            finally:
                os.close(child_fd)
            identity = ConfinedPathIdentity(
                "available",
                "directory",
                child_stat.st_dev,
                child_stat.st_ino,
                stat.S_IMODE(child_stat.st_mode),
                child_stat.st_size,
                child_digest,
            )
            entries.append({"path": child_name, "kind": "directory", "digest": ""})
            entries.extend(
                {"path": f"{child_name}/{entry['path']}", "kind": entry["kind"], "digest": entry["digest"]}
                for entry in child_entries
            )
            plan.append(ConfinedRemovalEntry(child_name, identity, children))
        else:
            raise ValueError("confined directory contains an unsupported entry kind")
    final_stat = os.fstat(directory_fd)
    if sorted(os.listdir(directory_fd)) != initial_names or (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_mtime_ns,
        final_stat.st_ctime_ns,
    ) != (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_mtime_ns,
        initial_stat.st_ctime_ns,
    ):
        raise ValueError("confined directory changed while hashing")
    entries.sort(key=lambda entry: entry["path"])
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return tuple(plan), entries, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _directory_digest_from_fd(directory_fd: int) -> str:
    _, _, digest = _directory_plan_from_fd(directory_fd)
    return digest


def _identity_at(parent_fd: int, name: str) -> ConfinedPathIdentity:
    try:
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return MISSING_PATH_IDENTITY
    mode = stat.S_IMODE(path_stat.st_mode)
    if stat.S_ISREG(path_stat.st_mode):
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened_stat = os.fstat(descriptor)
            if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise ValueError("confined target changed while opening")
            digest = _hash_open_file(descriptor, opened_stat)
        finally:
            os.close(descriptor)
        return ConfinedPathIdentity(
            "available",
            "file",
            path_stat.st_dev,
            path_stat.st_ino,
            mode,
            path_stat.st_size,
            digest,
        )
    if stat.S_ISDIR(path_stat.st_mode):
        directory_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise ValueError("confined directory changed while opening")
            digest = _directory_digest_from_fd(directory_fd)
        finally:
            os.close(directory_fd)
        return ConfinedPathIdentity(
            "available",
            "directory",
            path_stat.st_dev,
            path_stat.st_ino,
            mode,
            path_stat.st_size,
            digest,
        )
    if stat.S_ISLNK(path_stat.st_mode):
        target = os.readlink(name, dir_fd=parent_fd)
        return ConfinedPathIdentity(
            "available",
            "symlink",
            path_stat.st_dev,
            path_stat.st_ino,
            mode,
            path_stat.st_size,
            "sha256:" + hashlib.sha256(target.encode()).hexdigest(),
        )
    return ConfinedPathIdentity(
        "available",
        "other",
        path_stat.st_dev,
        path_stat.st_ino,
        mode,
        path_stat.st_size,
    )


def confined_path_identity(
    path: Path,
    managed_root: Path,
    *,
    allow_leaf_symlink: bool = False,
    missing_parent_is_missing: bool = False,
) -> ConfinedPathIdentity:
    try:
        parent_fd, name, _ = _open_confined_parent(
            path,
            managed_root,
            create_parents=False,
            allow_leaf_symlink=allow_leaf_symlink,
        )
    except ValueError as exc:
        if missing_parent_is_missing and str(exc) == "confined mutation parent is missing":
            return MISSING_PATH_IDENTITY
        raise
    try:
        return _identity_at(parent_fd, name)
    finally:
        os.close(parent_fd)


def confined_read_bytes(
    path: Path,
    managed_root: Path,
    expected: ConfinedPathIdentity,
) -> bytes:
    parent_fd, name, _ = _open_confined_parent(path, managed_root, create_parents=False)
    try:
        _verify_identity_at(parent_fd, name, expected)
        if expected.kind != "file":
            raise ValueError("confined read requires a regular file")
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (expected.device, expected.inode)
                or opened.st_size != expected.size
            ):
                raise ValueError("confined opened file identity changed")
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
            final = os.fstat(descriptor)
            if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ):
                raise ValueError("confined opened file changed while reading")
            if "sha256:" + digest.hexdigest() != expected.digest:
                raise ValueError("confined opened file digest changed")
        finally:
            os.close(descriptor)
        _verify_identity_at(parent_fd, name, expected)
        return b"".join(chunks)
    finally:
        os.close(parent_fd)


def confined_readlink(
    path: Path,
    managed_root: Path,
    expected: ConfinedPathIdentity,
) -> str:
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_identity_at(parent_fd, name, expected)
        if expected.kind != "symlink":
            raise ValueError("confined readlink requires a symlink")
        target = os.readlink(name, dir_fd=parent_fd)
        _verify_identity_at(parent_fd, name, expected)
        return target
    finally:
        os.close(parent_fd)


def confined_directory_names(
    path: Path,
    managed_root: Path,
    expected: ConfinedPathIdentity,
) -> tuple[str, ...]:
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_identity_at(parent_fd, name, expected)
        if expected.kind != "directory":
            raise ValueError("confined directory listing requires a directory")
        directory_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (expected.device, expected.inode):
                raise ValueError("confined directory identity changed while listing")
            names = tuple(sorted(os.listdir(directory_fd)))
        finally:
            os.close(directory_fd)
        _verify_identity_at(parent_fd, name, expected)
        return names
    finally:
        os.close(parent_fd)


def confined_ensure_parent(path: Path, managed_root: Path) -> tuple[Path, ...]:
    parent_fd, _, created = _open_confined_parent(path, managed_root, create_parents=True)
    os.close(parent_fd)
    return created


def confined_ensure_managed_root(managed_root: Path) -> bool:
    managed = managed_root.absolute()
    boundary = _ambient_trusted_boundary(managed)
    try:
        descriptor = os.open(
            boundary,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ValueError("managed root has a symlinked parent") from exc
    created = False
    try:
        for index, part in enumerate(managed.relative_to(boundary).parts):
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                if index == len(managed.relative_to(boundary).parts) - 1:
                    created = True
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise ValueError("managed root is unsafe") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return created
    finally:
        os.close(descriptor)


def _verify_identity_at(parent_fd: int, name: str, expected: ConfinedPathIdentity) -> None:
    current = _identity_at(parent_fd, name)
    if current != expected:
        raise ValueError("confined mutation target identity changed")


def _verify_parent_binding(staged_path: Path, managed_root: Path, parent_fd: int) -> None:
    rebound_fd, _, _ = _open_confined_parent(
        staged_path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        current = os.fstat(parent_fd)
        rebound = os.fstat(rebound_fd)
        if (current.st_dev, current.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise ValueError("confined mutation parent binding changed")
    finally:
        os.close(rebound_fd)


def confined_stage_bytes(
    path: Path,
    content: bytes,
    managed_root: Path,
    expected: ConfinedPathIdentity,
    *,
    mode: int | None = None,
) -> ConfinedStagedWrite:
    parent_fd, destination_name, _ = _open_confined_parent(path, managed_root, create_parents=False)
    temp_name: str | None = None
    descriptor: int | None = None
    created_identity: ConfinedPathIdentity | None = None
    try:
        _verify_identity_at(parent_fd, destination_name, expected)
        selected_mode = mode if mode is not None else expected.mode if expected.mode is not None else 0o600
        for _ in range(32):
            candidate = f".{destination_name}.confined-{secrets.token_hex(8)}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if descriptor is None or temp_name is None:
            raise OSError("could not allocate confined staged file")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, selected_mode)
        os.fsync(descriptor)
        created_identity = _identity_from_open_regular_file(descriptor)
        os.close(descriptor)
        descriptor = None
        temp_identity = _identity_at(parent_fd, temp_name)
        if temp_identity != created_identity:
            raise ValueError("confined staged file identity changed after creation")
        return ConfinedStagedWrite(
            path.absolute(),
            managed_root.absolute(),
            parent_fd,
            destination_name,
            temp_name,
            expected,
            temp_identity,
        )
    except Exception:
        if descriptor is not None:
            try:
                created_identity = _identity_from_open_regular_file(descriptor)
            except (OSError, ValueError):
                created_identity = None
            os.close(descriptor)
        if temp_name is not None:
            current = _identity_at(parent_fd, temp_name)
            if created_identity is not None and current == created_identity:
                quarantined = _quarantine_verified_entry(parent_fd, temp_name, created_identity)
                _finish_quarantined_removal(parent_fd, quarantined)
            elif current.state != "missing":
                debug("preserved replacement at failed confined stage path")
        os.close(parent_fd)
        raise


def confined_replace_staged(staged: ConfinedStagedWrite) -> ConfinedPathIdentity:
    _verify_parent_binding(staged.path, staged.managed_root, staged.parent_fd)
    _verify_identity_at(staged.parent_fd, staged.temp_name, staged.temp_identity)
    original: ConfinedQuarantinedPath | None = None
    try:
        if staged.expected.state == "missing":
            _verify_identity_at(staged.parent_fd, staged.destination_name, staged.expected)
        else:
            original = _quarantine_verified_entry(
                staged.parent_fd,
                staged.destination_name,
                staged.expected,
            )
        _verify_identity_at(staged.parent_fd, staged.temp_name, staged.temp_identity)
        try:
            os.link(
                staged.temp_name,
                staged.destination_name,
                src_dir_fd=staged.parent_fd,
                dst_dir_fd=staged.parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ValueError("confined destination changed during promotion") from exc
        os.fsync(staged.parent_fd)
        installed = _identity_at(staged.parent_fd, staged.destination_name)
        if installed != staged.temp_identity:
            raise ValueError("confined staged promotion verification failed")

        staged_temp = _quarantine_verified_entry(
            staged.parent_fd,
            staged.temp_name,
            staged.temp_identity,
        )
        _finish_quarantined_removal(staged.parent_fd, staged_temp)
        _verify_identity_at(staged.parent_fd, staged.destination_name, staged.temp_identity)
        if original is not None:
            _finish_quarantined_removal(staged.parent_fd, original)
            original = None
        return staged.temp_identity
    except Exception:
        current_destination = _identity_at(staged.parent_fd, staged.destination_name)
        if current_destination == staged.temp_identity:
            promoted = _quarantine_verified_entry(
                staged.parent_fd,
                staged.destination_name,
                staged.temp_identity,
            )
            _finish_quarantined_removal(staged.parent_fd, promoted)
            current_destination = MISSING_PATH_IDENTITY
        if original is not None:
            restored = False
            if current_destination.state == "missing":
                restored = _restore_quarantined_entry(staged.parent_fd, original)
            if restored or not os.listdir(original.wrapper_fd):
                _remove_empty_private_directory(
                    staged.parent_fd,
                    original.wrapper_name,
                    original.wrapper_fd,
                )
            else:
                os.close(original.wrapper_fd)
        raise


def confined_discard_staged(staged: ConfinedStagedWrite) -> bool:
    try:
        current = _identity_at(staged.parent_fd, staged.temp_name)
        if current.state == "missing":
            return True
        if current != staged.temp_identity:
            debug("preserved concurrent replacement at confined staged path")
            return False
        quarantined = _quarantine_verified_entry(
            staged.parent_fd,
            staged.temp_name,
            staged.temp_identity,
        )
        _finish_quarantined_removal(staged.parent_fd, quarantined)
        return True
    finally:
        os.close(staged.parent_fd)


def confined_atomic_write_bytes(
    path: Path,
    content: bytes,
    managed_root: Path,
    expected: ConfinedPathIdentity,
    *,
    mode: int | None = None,
) -> ConfinedPathIdentity:
    staged = confined_stage_bytes(path, content, managed_root, expected, mode=mode)
    installed: ConfinedPathIdentity | None = None
    try:
        installed = confined_replace_staged(staged)
        return installed
    finally:
        try:
            confined_discard_staged(staged)
        except Exception:
            if installed is None:
                raise
            debug("preserved staged cleanup state after committed confined write")


def _create_private_directory(parent_fd: int, prefix: str) -> tuple[str, int]:
    for _ in range(32):
        name = f".{prefix}-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        return name, descriptor
    raise OSError("could not allocate confined private directory")


def confined_create_private_directory(
    parent: Path,
    prefix: str,
) -> tuple[Path, ConfinedPathIdentity]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", prefix) is None:
        raise ValueError("private directory prefix contains unsupported characters")
    probe = parent.absolute() / f"{prefix}probe"
    parent_fd, _, _ = _open_confined_parent(
        probe,
        parent.absolute(),
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        for _ in range(32):
            name = f"{prefix}{secrets.token_hex(12)}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            os.fsync(parent_fd)
            identity = _identity_at(parent_fd, name)
            if identity.kind != "directory" or identity.digest != EMPTY_DIRECTORY_DIGEST:
                raise ValueError("created private directory identity is invalid")
            return parent.absolute() / name, identity
    finally:
        os.close(parent_fd)
    raise OSError("could not allocate confined private directory")


def confined_list_private_directories(
    parent: Path,
    prefix: str,
) -> tuple[tuple[Path, ConfinedPathIdentity], ...]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", prefix) is None:
        raise ValueError("private directory prefix contains unsupported characters")
    parent = parent.absolute()
    boundary = _ambient_trusted_boundary(parent)
    try:
        parent_fd = _open_directory_chain(
            boundary,
            parent.relative_to(boundary),
            "private directory parent is unsafe",
        )
    except ValueError as exc:
        if not parent.exists() and not parent.is_symlink():
            return ()
        raise exc
    try:
        initial = os.fstat(parent_fd)
        initial_names = tuple(sorted(os.listdir(parent_fd)))
        results: list[tuple[Path, ConfinedPathIdentity]] = []
        for name in initial_names:
            if not name.startswith(prefix):
                continue
            identity = _identity_at(parent_fd, name)
            if identity.kind == "directory":
                results.append((parent / name, identity))
        final = os.fstat(parent_fd)
        if tuple(sorted(os.listdir(parent_fd))) != initial_names or (
            final.st_dev,
            final.st_ino,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (
            initial.st_dev,
            initial.st_ino,
            initial.st_mtime_ns,
            initial.st_ctime_ns,
        ):
            raise ValueError("private directory parent changed while enumerating")
        return tuple(results)
    finally:
        os.close(parent_fd)


def _rename_to_quarantine(parent_fd: int, name: str, quarantine_fd: int, target_name: str) -> None:
    os.rename(name, target_name, src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)


def _rename_no_replace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(source_fd, source, destination_fd, destination, 0x00000004)
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(source_fd, source, destination_fd, destination, 0x00000001)
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _remove_empty_private_directory(parent_fd: int, wrapper_name: str, wrapper_fd: int) -> None:
    try:
        named = os.stat(wrapper_name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(wrapper_fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or os.listdir(wrapper_fd)
        ):
            raise ValueError("confined private directory identity changed")
        os.rmdir(wrapper_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(wrapper_fd)


def _quarantine_verified_entry(
    parent_fd: int,
    name: str,
    expected: ConfinedPathIdentity,
) -> ConfinedQuarantinedPath:
    wrapper_name, wrapper_fd = _create_private_directory(parent_fd, f"{name}.quarantine")
    target_name = "target"
    moved = False
    try:
        _verify_identity_at(parent_fd, name, expected)
        _rename_to_quarantine(parent_fd, name, wrapper_fd, target_name)
        moved = True
        os.fsync(wrapper_fd)
        os.fsync(parent_fd)
        quarantined = _identity_at(wrapper_fd, target_name)
        if quarantined != expected:
            restored = False
            if _identity_at(parent_fd, name).state == "missing":
                try:
                    _rename_no_replace(wrapper_fd, target_name, parent_fd, name)
                except FileExistsError:
                    pass
                else:
                    os.fsync(wrapper_fd)
                    os.fsync(parent_fd)
                    moved = False
                    restored = True
            detail = "restored" if restored else f"preserved in {wrapper_name}"
            raise ValueError(f"confined mutation target identity changed after quarantine; {detail}")
        return ConfinedQuarantinedPath(wrapper_name, wrapper_fd, name, target_name, expected)
    except Exception:
        if not moved:
            try:
                _remove_empty_private_directory(parent_fd, wrapper_name, wrapper_fd)
            except OSError:
                pass
        else:
            os.close(wrapper_fd)
        raise


def _delete_quarantined_entry(quarantined: ConfinedQuarantinedPath) -> None:
    _verify_identity_at(quarantined.wrapper_fd, quarantined.target_name, quarantined.expected)
    if quarantined.expected.kind == "directory":
        directory_fd = os.open(
            quarantined.target_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=quarantined.wrapper_fd,
        )
        try:
            _verify_open_directory_entry(
                quarantined.wrapper_fd,
                quarantined.target_name,
                quarantined.expected,
                directory_fd,
            )
            plan, _, digest = _directory_plan_from_fd(directory_fd)
            if digest != quarantined.expected.digest:
                raise ValueError("confined directory digest changed after quarantine")
            _remove_directory_contents(directory_fd, plan)
            if os.listdir(directory_fd):
                raise ValueError("confined directory changed during quarantined removal")
            _verify_open_directory_entry(
                quarantined.wrapper_fd,
                quarantined.target_name,
                quarantined.expected,
                directory_fd,
            )
            os.rmdir(quarantined.target_name, dir_fd=quarantined.wrapper_fd)
        finally:
            os.close(directory_fd)
    elif quarantined.expected.kind in {"file", "symlink"}:
        _verify_identity_at(quarantined.wrapper_fd, quarantined.target_name, quarantined.expected)
        os.unlink(quarantined.target_name, dir_fd=quarantined.wrapper_fd)
    else:
        raise ValueError("confined removal target kind is unsupported")
    os.fsync(quarantined.wrapper_fd)


def _restore_quarantined_entry(parent_fd: int, quarantined: ConfinedQuarantinedPath) -> bool:
    if (
        _identity_at(parent_fd, quarantined.original_name).state != "missing"
        or _identity_at(quarantined.wrapper_fd, quarantined.target_name).state == "missing"
    ):
        return False
    try:
        _rename_no_replace(
            quarantined.wrapper_fd,
            quarantined.target_name,
            parent_fd,
            quarantined.original_name,
        )
    except FileExistsError:
        return False
    os.fsync(quarantined.wrapper_fd)
    os.fsync(parent_fd)
    return True


def _finish_quarantined_removal(parent_fd: int, quarantined: ConfinedQuarantinedPath) -> None:
    try:
        _delete_quarantined_entry(quarantined)
    except Exception:
        restored = _restore_quarantined_entry(parent_fd, quarantined)
        if restored or not os.listdir(quarantined.wrapper_fd):
            _remove_empty_private_directory(parent_fd, quarantined.wrapper_name, quarantined.wrapper_fd)
        else:
            os.close(quarantined.wrapper_fd)
        raise
    _remove_empty_private_directory(parent_fd, quarantined.wrapper_name, quarantined.wrapper_fd)


def _copy_regular_fd(
    source_fd: int,
    destination_fd: int,
    expected: ConfinedPathIdentity,
) -> None:
    opened = os.fstat(source_fd)
    if (
        expected.kind != "file"
        or not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino, opened.st_size) != (expected.device, expected.inode, expected.size)
    ):
        raise ValueError("confined copy source identity changed")
    digest = hashlib.sha256()
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            view = view[written:]
    final = os.fstat(source_fd)
    if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ):
        raise ValueError("confined copy source changed while reading")
    if "sha256:" + digest.hexdigest() != expected.digest:
        raise ValueError("confined copy source digest changed")
    os.fchmod(destination_fd, expected.mode if expected.mode is not None else 0o600)
    os.fsync(destination_fd)


def _copy_tree_plan(
    source_fd: int,
    destination_fd: int,
    plan: tuple[ConfinedRemovalEntry, ...],
) -> None:
    _verify_removal_plan(source_fd, plan)
    for entry in plan:
        _verify_identity_at(source_fd, entry.name, entry.identity)
        if entry.identity.kind == "file":
            source_file_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_fd,
            )
            destination_file_fd = os.open(
                entry.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=destination_fd,
            )
            try:
                _copy_regular_fd(source_file_fd, destination_file_fd, entry.identity)
            finally:
                os.close(source_file_fd)
                os.close(destination_file_fd)
            _verify_identity_at(source_fd, entry.name, entry.identity)
        elif entry.identity.kind == "symlink":
            target = os.readlink(entry.name, dir_fd=source_fd)
            _verify_identity_at(source_fd, entry.name, entry.identity)
            os.symlink(target, entry.name, dir_fd=destination_fd)
        elif entry.identity.kind == "directory":
            source_directory_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_fd,
            )
            os.mkdir(
                entry.name,
                entry.identity.mode if entry.identity.mode is not None else 0o700,
                dir_fd=destination_fd,
            )
            destination_directory_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=destination_fd,
            )
            try:
                _verify_open_directory_entry(source_fd, entry.name, entry.identity, source_directory_fd)
                _copy_tree_plan(source_directory_fd, destination_directory_fd, entry.children)
                os.fsync(destination_directory_fd)
            finally:
                os.close(source_directory_fd)
                os.close(destination_directory_fd)
            _verify_identity_at(source_fd, entry.name, entry.identity)
        else:
            raise ValueError("confined copy source contains an unsupported entry kind")
    _verify_removal_plan(source_fd, plan)
    os.fsync(destination_fd)


def confined_copy_path_to_private(
    path: Path,
    managed_root: Path,
    expected: ConfinedPathIdentity,
    backup: Path,
) -> None:
    if backup.exists() or backup.is_symlink():
        raise ValueError("private snapshot destination already exists")
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_identity_at(parent_fd, name, expected)
        if expected.kind == "file":
            source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            destination_fd = os.open(
                backup,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _copy_regular_fd(source_fd, destination_fd, expected)
            finally:
                os.close(source_fd)
                os.close(destination_fd)
        elif expected.kind == "directory":
            source_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                plan, _, digest = _directory_plan_from_fd(source_fd)
                if digest != expected.digest:
                    raise ValueError("confined snapshot directory digest changed")
                backup.mkdir(mode=expected.mode if expected.mode is not None else 0o700)
                destination_fd = os.open(
                    backup,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    _copy_tree_plan(source_fd, destination_fd, plan)
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
        elif expected.kind != "symlink":
            raise ValueError("confined snapshot source kind is unsupported")
        _verify_identity_at(parent_fd, name, expected)
    finally:
        os.close(parent_fd)


def _trusted_directory_plan(
    path: Path,
    source_root: Path,
    expected: ConfinedPathIdentity | None = None,
) -> tuple[int, tuple[ConfinedRemovalEntry, ...], ConfinedPathIdentity]:
    parent_fd, name, _ = _open_confined_parent(path, source_root, create_parents=False)
    source_fd: int | None = None
    try:
        identity = _identity_at(parent_fd, name)
        if identity.kind != "directory":
            raise ValueError("trusted tree source is not a regular directory")
        if expected is not None and identity != expected:
            raise ValueError("trusted tree source identity changed")
        source_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (identity.device, identity.inode):
            raise ValueError("trusted tree source identity changed while opening")
        plan, _, digest = _directory_plan_from_fd(source_fd)
        if digest != identity.digest:
            raise ValueError("trusted tree source changed while reading")
        _verify_identity_at(parent_fd, name, identity)
        return source_fd, plan, identity
    except Exception:
        if source_fd is not None:
            os.close(source_fd)
        raise
    finally:
        os.close(parent_fd)


def _discard_staged_tree(parent_fd: int, wrapper_name: str, wrapper_fd: int) -> None:
    names = os.listdir(wrapper_fd)
    if names == ["tree"]:
        identity = _identity_at(wrapper_fd, "tree")
        staged = ConfinedQuarantinedPath(wrapper_name, wrapper_fd, "tree", "tree", identity)
        _delete_quarantined_entry(staged)
        names = os.listdir(wrapper_fd)
    if names:
        os.close(wrapper_fd)
        return
    _remove_empty_private_directory(parent_fd, wrapper_name, wrapper_fd)


def confined_replace_tree(
    source: Path,
    destination: Path,
    managed_root: Path,
    expected: ConfinedPathIdentity,
    *,
    source_root: Path,
    source_expected: ConfinedPathIdentity | None = None,
) -> ConfinedPathIdentity:
    parent_fd, destination_name, _ = _open_confined_parent(
        destination,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    source_fd: int | None = None
    stage_wrapper_fd: int | None = None
    stage_wrapper_name: str | None = None
    quarantined: ConfinedQuarantinedPath | None = None
    installed = False
    try:
        _verify_parent_binding(destination, managed_root, parent_fd)
        source_fd, plan, source_identity = _trusted_directory_plan(source, source_root, source_expected)
        stage_wrapper_name, stage_wrapper_fd = _create_private_directory(
            parent_fd,
            f"{destination_name}.stage",
        )
        os.mkdir("tree", source_identity.mode if source_identity.mode is not None else 0o700, dir_fd=stage_wrapper_fd)
        staged_tree_fd = os.open(
            "tree",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=stage_wrapper_fd,
        )
        try:
            _copy_tree_plan(source_fd, staged_tree_fd, plan)
        finally:
            os.close(staged_tree_fd)
        staged_identity = _identity_at(stage_wrapper_fd, "tree")
        if staged_identity.digest != source_identity.digest:
            raise ValueError("confined staged tree digest mismatch")

        if expected.state == "missing":
            _verify_identity_at(parent_fd, destination_name, expected)
        else:
            if expected.kind != "directory":
                raise ValueError("confined tree replacement requires a directory or missing target")
            quarantined = _quarantine_verified_entry(parent_fd, destination_name, expected)
        try:
            _rename_no_replace(stage_wrapper_fd, "tree", parent_fd, destination_name)
        except FileExistsError as exc:
            raise ValueError("confined tree destination changed during promotion") from exc
        installed = True
        os.fsync(stage_wrapper_fd)
        os.fsync(parent_fd)
        installed_identity = _identity_at(parent_fd, destination_name)
        if installed_identity != staged_identity:
            raise ValueError("confined installed tree identity mismatch")
        if quarantined is not None:
            _finish_quarantined_removal(parent_fd, quarantined)
            quarantined = None
        _remove_empty_private_directory(parent_fd, stage_wrapper_name, stage_wrapper_fd)
        stage_wrapper_fd = None
        return installed_identity
    except Exception:
        if quarantined is not None:
            if not installed:
                restored = _restore_quarantined_entry(parent_fd, quarantined)
                if restored or not os.listdir(quarantined.wrapper_fd):
                    _remove_empty_private_directory(parent_fd, quarantined.wrapper_name, quarantined.wrapper_fd)
                else:
                    os.close(quarantined.wrapper_fd)
            else:
                os.close(quarantined.wrapper_fd)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if stage_wrapper_fd is not None:
            try:
                _discard_staged_tree(parent_fd, str(stage_wrapper_name), stage_wrapper_fd)
            except OSError:
                try:
                    os.close(stage_wrapper_fd)
                except OSError:
                    pass
        os.close(parent_fd)


def confined_unlink(path: Path, managed_root: Path, expected: ConfinedPathIdentity) -> None:
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_parent_binding(path, managed_root, parent_fd)
        if expected.kind not in {"file", "symlink"}:
            raise ValueError("confined unlink requires a file or symlink")
        quarantined = _quarantine_verified_entry(parent_fd, name, expected)
        _finish_quarantined_removal(parent_fd, quarantined)
    finally:
        os.close(parent_fd)


def _verify_removal_plan(directory_fd: int, plan: tuple[ConfinedRemovalEntry, ...]) -> None:
    expected_names = sorted(entry.name for entry in plan)
    if sorted(os.listdir(directory_fd)) != expected_names:
        raise ValueError("confined directory contents changed before removal")
    for entry in plan:
        _verify_identity_at(directory_fd, entry.name, entry.identity)


def _verify_open_directory_entry(
    parent_fd: int,
    name: str,
    expected: ConfinedPathIdentity,
    directory_fd: int,
) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(directory_fd)
    current_key = (current.st_dev, current.st_ino)
    expected_key = (expected.device, expected.inode)
    opened_key = (opened.st_dev, opened.st_ino)
    if not stat.S_ISDIR(current.st_mode) or current_key != expected_key or opened_key != expected_key:
        raise ValueError("confined directory entry identity changed")


def _remove_directory_contents(
    directory_fd: int,
    plan: tuple[ConfinedRemovalEntry, ...],
) -> None:
    _verify_removal_plan(directory_fd, plan)
    for entry in plan:
        _verify_identity_at(directory_fd, entry.name, entry.identity)
        quarantined = _quarantine_verified_entry(directory_fd, entry.name, entry.identity)
        _finish_quarantined_removal(directory_fd, quarantined)
    if os.listdir(directory_fd):
        raise ValueError("confined directory changed during removal")
    os.fsync(directory_fd)


def confined_remove_path(path: Path, managed_root: Path, expected: ConfinedPathIdentity) -> None:
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_parent_binding(path, managed_root, parent_fd)
        quarantined = _quarantine_verified_entry(parent_fd, name, expected)
        _finish_quarantined_removal(parent_fd, quarantined)
    finally:
        os.close(parent_fd)


def confined_rmdir(path: Path, managed_root: Path, expected: ConfinedPathIdentity) -> None:
    parent_fd, name, _ = _open_confined_parent(path, managed_root, create_parents=False)
    try:
        _verify_parent_binding(path, managed_root, parent_fd)
        if expected.kind != "directory" or expected.digest != EMPTY_DIRECTORY_DIGEST:
            raise ValueError("confined rmdir requires an empty directory")
        quarantined = _quarantine_verified_entry(parent_fd, name, expected)
        _finish_quarantined_removal(parent_fd, quarantined)
    finally:
        os.close(parent_fd)


def confined_create_symlink(
    path: Path,
    managed_root: Path,
    link_target: str,
    expected: ConfinedPathIdentity,
) -> ConfinedPathIdentity:
    parent_fd, name, _ = _open_confined_parent(
        path,
        managed_root,
        create_parents=False,
        allow_leaf_symlink=True,
    )
    try:
        _verify_parent_binding(path, managed_root, parent_fd)
        if expected.state != "missing":
            raise ValueError("confined symlink creation requires a missing target")
        _verify_identity_at(parent_fd, name, expected)
        os.symlink(link_target, name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        created = _identity_at(parent_fd, name)
        expected_digest = "sha256:" + hashlib.sha256(link_target.encode()).hexdigest()
        if created.kind != "symlink" or created.digest != expected_digest:
            raise ValueError("confined created symlink identity mismatch")
        return created
    finally:
        os.close(parent_fd)


def debug(message: str) -> None:
    if os.environ.get("LAZY_SKILL_ROUTER_DEBUG"):
        print(f"lazy-skill-router: {message}", file=sys.stderr)


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def canonical_hook_command(hook_path: Path, routes_path: Path, *, stop: bool = False) -> str:
    argv = ("python3", str(hook_path), "--config", str(routes_path))
    if stop:
        argv = (*argv, "--hook-event", "stop")
    return shlex.join(argv)


def normalized_command_argv(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        argv = tuple(shlex.split(value))
    except ValueError:
        return None
    return argv or None


def command_matches_any(value: Any, expected_commands: Iterable[str]) -> bool:
    argv = normalized_command_argv(value)
    if argv is None:
        return False
    for command in expected_commands:
        expected = normalized_command_argv(command)
        if expected is not None and argv == expected:
            return True
    return False


def registered_hook_command(registration: Any, event_name: str) -> str | None:
    if not isinstance(registration, dict):
        return None
    if event_name == "UserPromptSubmit":
        entry = registration
    elif event_name == "Stop":
        entry = registration.get("lifecycle")
    else:
        return None
    if not isinstance(entry, dict) or entry.get("event") != event_name:
        return None
    command = entry.get("command")
    return command if normalized_command_argv(command) is not None else None


def load_json_object(path: Path, root_name: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{root_name} must be an object: {path}")
    return data


def load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    data = load_json_object(path, str(path))
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} must contain a hooks object")
    return data


def backup_file(path: Path, managed_root: Path, label: str = "") -> Path | None:
    """Copy a managed regular file without following symlinks.

    The backup is created exclusively beside the source.  The explicit managed
    root is part of the security boundary: callers may not silently widen it to
    an arbitrary source parent.
    """

    target = path.absolute()
    managed = managed_root.absolute()
    if label and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label) is None:
        raise ValueError("backup label contains unsupported characters")
    if managed.is_symlink():
        raise ValueError("managed root is a symlink")
    boundary = _ambient_trusted_boundary(managed)
    managed_relative = managed.relative_to(boundary)
    current_root = boundary
    for part in managed_relative.parts:
        current_root /= part
        if current_root.is_symlink():
            raise ValueError("managed root has a symlinked parent")
    try:
        relative = target.relative_to(managed)
    except ValueError as exc:
        raise ValueError("backup source is outside managed root") from exc
    if not relative.parts or relative == Path("."):
        raise ValueError("backup source cannot be the managed root")

    current = managed
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("backup source has a symlinked parent")
    if target.is_symlink():
        raise ValueError("backup source is a symlink")
    try:
        source_lstat = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(source_lstat.st_mode):
        raise ValueError("backup source is not a regular file")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = _open_directory_chain(
        boundary,
        managed_relative,
        "managed root has a symlinked parent",
    )
    parent_fd = root_fd
    source_fd: int | None = None
    destination_fd: int | None = None
    backup_name: str | None = None
    created_backup_identity: ConfinedPathIdentity | None = None
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=parent_fd)
            except OSError as exc:
                raise ValueError("backup source has a symlinked parent") from exc
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd

        source_fd = os.open(relative.name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or (source_stat.st_dev, source_stat.st_ino) != (
            source_lstat.st_dev,
            source_lstat.st_ino,
        ):
            raise ValueError("backup source is not a regular file")

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        label_part = f"-{label}" if label else ""
        for _ in range(32):
            candidate = f"{relative.name}.bak-lazy-skill-router{label_part}-{stamp}-{secrets.token_hex(6)}"
            try:
                destination_fd = os.open(
                    candidate,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            backup_name = candidate
            break
        if destination_fd is None or backup_name is None:
            raise OSError("could not allocate an exclusive backup file")

        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        final_source_stat = os.fstat(source_fd)
        if (
            final_source_stat.st_dev,
            final_source_stat.st_ino,
            final_source_stat.st_size,
            final_source_stat.st_mtime_ns,
            final_source_stat.st_ctime_ns,
        ) != (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        ):
            raise ValueError("backup source changed while copying")
        os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode))
        os.utime(
            destination_fd,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )
        os.fsync(destination_fd)
        created_backup_identity = _identity_from_open_regular_file(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        if _identity_at(parent_fd, backup_name) != created_backup_identity:
            raise ValueError("backup destination identity changed after creation")
        os.fsync(parent_fd)
        return target.with_name(backup_name)
    except Exception:
        if destination_fd is not None:
            try:
                created_backup_identity = _identity_from_open_regular_file(destination_fd)
            except (OSError, ValueError):
                created_backup_identity = None
            os.close(destination_fd)
            destination_fd = None
        if backup_name is not None:
            current_backup = _identity_at(parent_fd, backup_name)
            if created_backup_identity is not None and current_backup == created_backup_identity:
                quarantined = _quarantine_verified_entry(
                    parent_fd,
                    backup_name,
                    created_backup_identity,
                )
                _finish_quarantined_removal(parent_fd, quarantined)
            elif current_backup.state != "missing":
                debug("preserved replacement at failed backup path")
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _encoded_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def write_json(
    path: Path,
    data: dict[str, Any],
    *,
    managed_root: Path | None = None,
    expected: ConfinedPathIdentity | None = None,
) -> ConfinedPathIdentity | None:
    if managed_root is not None:
        expected_identity = expected if expected is not None else confined_path_identity(path, managed_root)
        return confined_atomic_write_bytes(path, _encoded_json(data), managed_root, expected_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return None


def ensure_safe_write_target(path: Path, managed_root: Path) -> None:
    target = path.absolute()
    managed = managed_root.absolute()
    if target.is_symlink():
        raise ValueError("write target is a symlink")

    ambient_boundary = _ambient_trusted_boundary(managed)
    current_managed = ambient_boundary
    for part in managed.relative_to(ambient_boundary).parts:
        current_managed /= part
        if current_managed.is_symlink():
            raise ValueError("managed root has a symlinked parent")

    candidates = (managed, Path.cwd().absolute(), Path.home().absolute(), Path(tempfile.gettempdir()).absolute())
    boundaries: list[Path] = []
    for candidate in candidates:
        try:
            target.relative_to(candidate)
        except ValueError:
            continue
        boundaries.append(candidate)
    boundary = max(boundaries, key=lambda item: len(item.parts)) if boundaries else Path(target.anchor)
    relative = target.relative_to(boundary)

    current = boundary
    if boundary == managed and current.is_symlink():
        raise ValueError("managed root is a symlink")
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("write target has a symlinked parent")
    try:
        target.parent.resolve(strict=False).relative_to(boundary.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("write target escapes trusted root") from exc


def write_json_atomic(
    path: Path,
    data: dict[str, Any],
    *,
    managed_root: Path | None = None,
    expected: ConfinedPathIdentity | None = None,
) -> ConfinedPathIdentity | None:
    if managed_root is not None:
        expected_identity = expected if expected is not None else confined_path_identity(path, managed_root)
        return confined_atomic_write_bytes(path, _encoded_json(data), managed_root, expected_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return None
