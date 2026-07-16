from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import doctor as doctor_module
import install
import lazy_skill_router_common as common
import uninstall
from lazy_skill_router_install_manifest import (
    build_install_manifest,
    load_install_manifest,
    manifest_revision,
    refresh_generated_artifact_digests,
    sha256_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "install.py"
DOCTOR_PATH = ROOT / "doctor.py"
UNINSTALL_PATH = ROOT / "uninstall.py"


def run_command(
    path: Path,
    codex_home: Path,
    *args: str,
    agents_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(path), "--codex-home", str(codex_home)]
    if agents_home is not None:
        command.extend(["--agents-home", str(agents_home)])
    command.extend(args)
    return subprocess.run(command, check=False, capture_output=True, text=True, cwd=ROOT)


def run_install_main(
    codex_home: Path,
    agents_home: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "install.py",
        "--codex-home",
        str(codex_home),
        "--agents-home",
        str(agents_home),
        *args,
    ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(install, "smoke_staged_hook"),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        returncode = install.main()
    return subprocess.CompletedProcess(argv, returncode, stdout.getvalue(), stderr.getvalue())


def seed_installed_skill(codex_home: Path, ownership: str) -> tuple[Path, Path]:
    skill = codex_home / "skills" / "personal-skill-router"
    skill.mkdir(parents=True)
    marker = skill / "legacy-marker.txt"
    marker.write_text("legacy skill\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: personal-skill-router\n---\nlegacy skill\n",
        encoding="utf-8",
    )
    manifest = build_install_manifest(
        codex_home,
        ((skill, ownership),),
        "python3 old-hook.py",
        generated_at="2026-07-10T00:00:00Z",
    )
    manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return skill, marker


def skill_ownership(codex_home: Path) -> str:
    snapshot = load_install_manifest(codex_home / "lazy-skill-router" / "install.manifest.json")
    return next(
        str(artifact["ownership"])
        for artifact in snapshot.artifacts
        if artifact.get("path") == "skills/personal-skill-router"
    )


class InstallManifestTest(unittest.TestCase):
    def test_failed_stage_preserves_replacement_at_exclusive_temp_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_bytes(b"original\n")
            expected = common.confined_path_identity(target, root)
            replacement_bytes = b"replacement at failed stage name\n"
            replacement_path: Path | None = None
            held_path = root / ".held-stage-created-by-call"

            def swap_then_fail_fchmod(descriptor: int, mode: int) -> None:
                nonlocal replacement_path
                replacement_path = next(root.glob(".target.json.confined-*"))
                replacement_path.rename(held_path)
                replacement_path.write_bytes(replacement_bytes)
                raise OSError("injected staged metadata failure")

            with (
                mock.patch.object(common.os, "fchmod", side_effect=swap_then_fail_fchmod),
                self.assertRaisesRegex(OSError, "staged metadata failure"),
            ):
                common.confined_stage_bytes(target, b"candidate\n", root, expected)

            self.assertEqual(replacement_path.read_bytes(), replacement_bytes)
            self.assertTrue(held_path.is_file())

    def test_failed_backup_preserves_replacement_at_exclusive_backup_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.json"
            source.write_bytes(b"source\n")
            replacement_bytes = b"replacement at failed backup name\n"
            replacement_path: Path | None = None
            held_path = root / ".held-backup-created-by-call"

            def swap_then_fail_fchmod(descriptor: int, mode: int) -> None:
                nonlocal replacement_path
                replacement_path = next(root.glob("source.json.bak-lazy-skill-router-*"))
                replacement_path.rename(held_path)
                replacement_path.write_bytes(replacement_bytes)
                raise OSError("injected backup metadata failure")

            with (
                mock.patch.object(common.os, "fchmod", side_effect=swap_then_fail_fchmod),
                self.assertRaisesRegex(OSError, "backup metadata failure"),
            ):
                common.backup_file(source, root)

            self.assertEqual(replacement_path.read_bytes(), replacement_bytes)
            self.assertTrue(held_path.is_file())

    def test_confined_discard_preserves_replacement_at_staged_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_bytes(b"original\n")
            expected = common.confined_path_identity(target, root)
            staged = common.confined_stage_bytes(target, b"candidate\n", root, expected)
            staged_path = root / staged.temp_name
            held_path = root / ".held-original-stage"
            os.rename(staged.temp_name, held_path.name, src_dir_fd=staged.parent_fd, dst_dir_fd=staged.parent_fd)
            replacement_bytes = b"concurrent temp replacement\n"
            staged_path.write_bytes(replacement_bytes)

            removed = common.confined_discard_staged(staged)

            self.assertFalse(removed)
            self.assertEqual(staged_path.read_bytes(), replacement_bytes)
            self.assertTrue(held_path.is_file())

    def test_confined_promote_rejects_temp_and_destination_swaps(self) -> None:
        for swap_kind in ("temp", "destination"):
            with self.subTest(swap_kind=swap_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                target = root / "target.json"
                original_bytes = b"original destination\n"
                target.write_bytes(original_bytes)
                expected = common.confined_path_identity(target, root)
                staged = common.confined_stage_bytes(target, b"candidate\n", root, expected)
                real_verify = common._verify_identity_at
                swapped = False
                concurrent_bytes = f"concurrent {swap_kind}\n".encode()

                def swap_after_final_verify(
                    parent_fd: int,
                    name: str,
                    identity,
                    real_verify=real_verify,
                    swap_kind=swap_kind,
                    destination_name=staged.destination_name,
                    temp_name=staged.temp_name,
                    concurrent_bytes=concurrent_bytes,
                ) -> None:
                    nonlocal swapped
                    real_verify(parent_fd, name, identity)
                    selected_name = temp_name if swap_kind == "temp" else destination_name
                    if name != selected_name or swapped:
                        return
                    held_name = f".held-{swap_kind}"
                    os.rename(name, held_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    replacement_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        os.write(replacement_fd, concurrent_bytes)
                        os.fsync(replacement_fd)
                    finally:
                        os.close(replacement_fd)
                    swapped = True

                try:
                    with (
                        mock.patch.object(common, "_verify_identity_at", side_effect=swap_after_final_verify),
                        self.assertRaisesRegex(ValueError, "identity changed|verification failed|concurrent"),
                    ):
                        common.confined_replace_staged(staged)
                finally:
                    common.confined_discard_staged(staged)

                if swap_kind == "destination":
                    self.assertEqual(target.read_bytes(), concurrent_bytes)
                else:
                    self.assertEqual(target.read_bytes(), original_bytes)

    def test_atomic_write_records_success_and_preserves_temp_replacement_during_discard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_bytes(b"original\n")
            expected = common.confined_path_identity(target, root)
            real_discard = common.confined_discard_staged
            replacement_bytes = b"replacement created before discard\n"
            replacement_path: Path | None = None

            def replace_temp_then_discard(staged):
                nonlocal replacement_path
                replacement_path = root / staged.temp_name
                replacement_path.write_bytes(replacement_bytes)
                return real_discard(staged)

            with mock.patch.object(
                common,
                "confined_discard_staged",
                side_effect=replace_temp_then_discard,
            ):
                installed = common.confined_atomic_write_bytes(
                    target,
                    b"installed candidate\n",
                    root,
                    expected,
                )

            self.assertEqual(installed, common.confined_path_identity(target, root))
            self.assertEqual(target.read_bytes(), b"installed candidate\n")
            self.assertEqual(replacement_path.read_bytes(), replacement_bytes)

    def test_atomic_write_rejects_target_outside_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "managed"
            managed_root.mkdir()
            outside = root / "outside.txt"
            original = b"outside sentinel\n"
            outside.write_bytes(original)
            outside_identity = common.confined_path_identity(outside, root)

            with self.assertRaisesRegex(ValueError, "escapes managed root"):
                common.confined_atomic_write_bytes(
                    outside,
                    b"unexpected replacement\n",
                    managed_root,
                    outside_identity,
                )

            self.assertEqual(outside.read_bytes(), original)

    def test_managed_root_creation_removes_partial_ancestor_chain_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            managed_root = first / "second"
            real_mkdir = common.os.mkdir

            def fail_second_directory(path, mode=0o777, *, dir_fd=None):
                if path == "second":
                    raise OSError("injected managed-root creation failure")
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(common.os, "mkdir", side_effect=fail_second_directory),
                self.assertRaisesRegex(OSError, "injected managed-root creation failure"),
            ):
                common.confined_ensure_managed_root(managed_root)

            self.assertFalse(first.exists())

    def test_parent_creation_removes_partial_chain_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "managed"
            managed_root.mkdir()
            first = managed_root / "first"
            target = first / "second" / "output.json"
            real_mkdir = common.os.mkdir

            def fail_second_directory(path, mode=0o777, *, dir_fd=None):
                if path == "second":
                    raise OSError("injected parent creation failure")
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(common.os, "mkdir", side_effect=fail_second_directory),
                self.assertRaisesRegex(OSError, "injected parent creation failure"),
            ):
                common.confined_ensure_parent(target, managed_root)

            self.assertFalse(first.exists())

    def test_parent_creation_removes_directory_when_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "managed"
            managed_root.mkdir()
            created = managed_root / "created"
            target = created / "output.json"
            real_fsync = common.os.fsync
            calls = 0

            def fail_first_fsync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected parent fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch.object(common.os, "fsync", side_effect=fail_first_fsync),
                self.assertRaisesRegex(OSError, "injected parent fsync failure"),
            ):
                common.confined_ensure_parent(target, managed_root)

            self.assertFalse(created.exists())

    def test_managed_root_creation_removes_directory_when_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "managed"
            real_fsync = common.os.fsync
            calls = 0

            def fail_first_fsync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected managed-root fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch.object(common.os, "fsync", side_effect=fail_first_fsync),
                self.assertRaisesRegex(OSError, "injected managed-root fsync failure"),
            ):
                common.confined_ensure_managed_root(managed_root)

            self.assertFalse(managed_root.exists())

    def test_private_wrapper_is_removed_before_its_fd_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            wrapper_name, wrapper_fd = common._create_private_directory(parent_fd, "wrapper")
            wrapper_path = root / wrapper_name
            held_path = root / ".held-wrapper"
            real_close = common.os.close
            swapped = False

            def swap_on_early_close(descriptor: int) -> None:
                nonlocal swapped
                real_close(descriptor)
                if descriptor == wrapper_fd and wrapper_path.exists():
                    wrapper_path.rename(held_path)
                    wrapper_path.mkdir()
                    swapped = True

            try:
                with mock.patch.object(common.os, "close", side_effect=swap_on_early_close):
                    common._remove_empty_private_directory(parent_fd, wrapper_name, wrapper_fd)
            finally:
                real_close(parent_fd)

            self.assertFalse(swapped)
            self.assertFalse(held_path.exists())
            self.assertFalse(wrapper_path.exists())

    def test_confined_read_rejects_swap_to_regular_file_and_swap_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "managed.json"
            original_bytes = b'{"trusted":true}\n'
            poison_bytes = b'{"poison":true}\n'
            target.write_bytes(original_bytes)
            expected = common.confined_path_identity(target, root)
            real_open = common.os.open
            swapped = False
            target_opens = 0

            def open_swapped_leaf(path, flags, *args, dir_fd=None, **kwargs):
                nonlocal swapped, target_opens
                if path == target.name and dir_fd is not None:
                    target_opens += 1
                if path == target.name and dir_fd is not None and target_opens == 2 and not swapped:
                    held_name = ".managed.original"
                    os.rename(target.name, held_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    poison_fd = real_open(
                        target.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(poison_fd, poison_bytes)
                    finally:
                        os.close(poison_fd)
                    opened = real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
                    os.unlink(target.name, dir_fd=dir_fd)
                    os.rename(held_name, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    swapped = True
                    return opened
                return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

            with (
                mock.patch.object(common.os, "open", side_effect=open_swapped_leaf),
                self.assertRaisesRegex(ValueError, "opened file identity changed"),
            ):
                common.confined_read_bytes(target, root, expected)

            self.assertEqual(target.read_bytes(), original_bytes)

    def test_confined_directory_removal_rejects_late_file_and_symlink_replacement(self) -> None:
        for replacement_kind in ("file", "symlink"):
            for swap_after_verification in (2, 3):
                with (
                    self.subTest(
                        replacement_kind=replacement_kind,
                        swap_after_verification=swap_after_verification,
                    ),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    root = Path(temp_dir)
                    directory = root / "owned-directory"
                    directory.mkdir()
                    victim = directory / "victim.txt"
                    victim.write_text("owned\n", encoding="utf-8")
                    sentinel = root / "outside-sentinel.txt"
                    sentinel_bytes = b"outside sentinel\n"
                    sentinel.write_bytes(sentinel_bytes)
                    expected = common.confined_path_identity(directory, root)
                    real_verify = common._verify_identity_at
                    victim_verifications = 0

                    def replace_after_loop_precheck(
                        parent_fd: int,
                        name: str,
                        identity,
                        real_verify=real_verify,
                        victim_name=victim.name,
                        replacement_kind=replacement_kind,
                        sentinel=sentinel,
                        swap_after_verification=swap_after_verification,
                    ) -> None:
                        nonlocal victim_verifications
                        real_verify(parent_fd, name, identity)
                        if name != victim_name:
                            return
                        victim_verifications += 1
                        if victim_verifications != swap_after_verification:
                            return
                        os.unlink(name, dir_fd=parent_fd)
                        if replacement_kind == "file":
                            descriptor = os.open(
                                name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=parent_fd,
                            )
                            try:
                                os.write(descriptor, b"late replacement\n")
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        else:
                            os.symlink(sentinel, name, dir_fd=parent_fd)
                        os.fsync(parent_fd)

                    with (
                        mock.patch.object(common, "_verify_identity_at", side_effect=replace_after_loop_precheck),
                        self.assertRaisesRegex(ValueError, "identity changed"),
                    ):
                        common.confined_remove_path(directory, root, expected)

                    self.assertTrue(victim.exists() or victim.is_symlink())
                    self.assertEqual(sentinel.read_bytes(), sentinel_bytes)

    def test_doctor_requires_exact_generated_file_ownership_for_inventory_and_index(self) -> None:
        for malformed_kind in ("missing", "duplicate", "ownership", "kind"):
            with self.subTest(malformed_kind=malformed_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                agents_home = root / "agents"
                installed = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                inventory_relative = "lazy-skill-router/skills.manifest.json"
                index_relative = "lazy-skill-router/capability-index.json"
                if malformed_kind == "missing":
                    manifest["artifacts"] = [
                        artifact for artifact in manifest["artifacts"] if artifact.get("path") != inventory_relative
                    ]
                elif malformed_kind == "duplicate":
                    index_record = next(
                        artifact for artifact in manifest["artifacts"] if artifact.get("path") == index_relative
                    )
                    manifest["artifacts"].append(dict(index_record))
                elif malformed_kind == "ownership":
                    inventory_record = next(
                        artifact for artifact in manifest["artifacts"] if artifact.get("path") == inventory_relative
                    )
                    inventory_record["ownership"] = "preserved"
                else:
                    index_record = next(
                        artifact for artifact in manifest["artifacts"] if artifact.get("path") == index_relative
                    )
                    index_record["kind"] = "directory"
                manifest["revision"] = manifest_revision(manifest["artifacts"], manifest["registration"])
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

                doctor = run_command(DOCTOR_PATH, codex_home, agents_home=agents_home)

                self.assertEqual(doctor.returncode, 1, doctor.stdout)
                self.assertIn("requires exactly one generated regular-file ownership record", doctor.stdout)

    def test_uninstall_rejects_hooks_leaf_swap_after_backup_without_touching_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            installed = run_install_main(codex_home, agents_home)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            hooks_path = codex_home / "hooks.json"
            sentinel = root / "outside-hooks.json"
            sentinel_bytes = b'{"sentinel":"keep"}\n'
            sentinel.write_bytes(sentinel_bytes)
            real_write_json = uninstall.write_json

            def swap_leaf_after_preflight(path: Path, data: dict[str, object], **kwargs) -> None:
                if path == hooks_path:
                    path.unlink()
                    path.symlink_to(sentinel)
                real_write_json(path, data, **kwargs)

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = ["uninstall.py", "--codex-home", str(codex_home)]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(uninstall, "write_json", side_effect=swap_leaf_after_preflight),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = uninstall.main()

            sentinel_after = sentinel.read_bytes()

        self.assertEqual(result, 1, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(sentinel_after, sentinel_bytes)

    def test_uninstall_rejects_directory_content_addition_immediately_before_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            installed = run_install_main(codex_home, agents_home)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            skill_directory = codex_home / "skills" / "personal-skill-router"
            late_file = skill_directory / "late-user-file.txt"
            real_remove = uninstall.confined_remove_path
            injected = False

            def add_content_before_remove(path: Path, managed_root: Path, expected) -> None:
                nonlocal injected
                if path == skill_directory and not injected:
                    late_file.write_text("keep\n", encoding="utf-8")
                    injected = True
                real_remove(path, managed_root, expected)

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = ["uninstall.py", "--codex-home", str(codex_home), "--remove-files"]
            with (
                mock.patch.object(uninstall, "confined_remove_path", side_effect=add_content_before_remove),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = uninstall.main()

            late_file_exists = late_file.exists()

        self.assertEqual(result, 1, stdout.getvalue() + stderr.getvalue())
        self.assertTrue(late_file_exists)

    def test_refresh_generated_artifact_digests_preserves_ownership_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex"
            inventory = codex_home / "lazy-skill-router" / "skills.manifest.json"
            index = inventory.with_name("capability-index.json")
            inventory.parent.mkdir(parents=True)
            inventory.write_text('{"old":"inventory"}\n', encoding="utf-8")
            index.write_text('{"old":"index"}\n', encoding="utf-8")
            manifest = build_install_manifest(
                codex_home,
                ((inventory, "generated"), (index, "generated")),
                "python3 hook.py",
                generated_at="2026-07-10T00:00:00Z",
            )
            manifest_path = inventory.with_name("install.manifest.json")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = load_install_manifest(manifest_path)

            refreshed = refresh_generated_artifact_digests(
                snapshot,
                {
                    "lazy-skill-router/skills.manifest.json": sha256_bytes(b"new inventory\n"),
                    "lazy-skill-router/capability-index.json": sha256_bytes(b"new index\n"),
                },
                generated_at="2026-07-11T00:00:00Z",
            )
            manifest_path.write_text(json.dumps(refreshed), encoding="utf-8")
            reloaded = load_install_manifest(manifest_path)

        self.assertEqual(reloaded.state, "available")
        self.assertEqual(reloaded.registration, snapshot.registration)
        self.assertEqual({artifact["ownership"] for artifact in reloaded.artifacts}, {"generated"})
        digests = {artifact["path"]: artifact["digest"] for artifact in reloaded.artifacts}
        self.assertEqual(
            digests["lazy-skill-router/skills.manifest.json"],
            sha256_bytes(b"new inventory\n"),
        )
        self.assertEqual(
            digests["lazy-skill-router/capability-index.json"],
            sha256_bytes(b"new index\n"),
        )

    def test_refresh_generated_artifact_digests_rejects_unowned_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex"
            managed = codex_home / "hooks" / "lazy_skill_router.py"
            managed.parent.mkdir(parents=True)
            managed.write_text("pass\n", encoding="utf-8")
            manifest = build_install_manifest(codex_home, ((managed, "managed"),), "python3 hook.py")
            manifest_path = codex_home / "install.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = load_install_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "not a generated file"):
                refresh_generated_artifact_digests(
                    snapshot,
                    {"hooks/lazy_skill_router.py": sha256_bytes(b"changed\n")},
                )

    def test_manifest_rejects_codex_root_as_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "install.manifest.json"
            artifacts = [
                {
                    "path": ".",
                    "kind": "directory",
                    "ownership": "managed",
                    "digest": "sha256:ignored",
                }
            ]
            registration = {"event": "UserPromptSubmit", "command": "python3 hook.py"}
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "lazy-skill-router.install-manifest/v1",
                        "revision": manifest_revision(artifacts, registration),
                        "artifacts": artifacts,
                        "registration": registration,
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_install_manifest(manifest_path)

        self.assertEqual(snapshot.state, "invalid")
        self.assertEqual(snapshot.reason_codes, ("install_manifest_path_invalid",))

    def test_manifest_rejects_noncanonical_and_casefold_alias_paths(self) -> None:
        for alias_kind in ("dot", "casefold"):
            with self.subTest(alias_kind=alias_kind), tempfile.TemporaryDirectory() as temp_dir:
                manifest_path = Path(temp_dir) / "install.manifest.json"
                paths = ("./hooks/runtime.py",) if alias_kind == "dot" else ("hooks/runtime.py", "hooks/Runtime.py")
                artifacts = [
                    {
                        "path": path,
                        "kind": "file",
                        "ownership": "managed",
                        "digest": sha256_bytes(path.encode()),
                    }
                    for path in paths
                ]
                registration = {"event": "UserPromptSubmit", "command": "python3 hook.py"}
                manifest_path.write_text(
                    json.dumps(
                        {
                            "schema": "lazy-skill-router.install-manifest/v1",
                            "revision": manifest_revision(artifacts, registration),
                            "artifacts": artifacts,
                            "registration": registration,
                        }
                    ),
                    encoding="utf-8",
                )

                snapshot = load_install_manifest(manifest_path)

                self.assertEqual(snapshot.state, "invalid")
                self.assertEqual(snapshot.reason_codes, ("install_manifest_path_invalid",))

    def test_doctor_rejects_hardlink_aliases_in_install_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            installed = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original_record = next(
                artifact for artifact in manifest["artifacts"] if artifact.get("path") == "hooks/lazy_skill_router.py"
            )
            original_path = codex_home / str(original_record["path"])
            alias_path = codex_home / "hooks" / "lazy_skill_router_alias.py"
            os.link(original_path, alias_path)
            alias_record = dict(original_record)
            alias_record["path"] = "hooks/lazy_skill_router_alias.py"
            manifest["artifacts"].append(alias_record)
            manifest["revision"] = manifest_revision(manifest["artifacts"], manifest["registration"])
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            result = doctor_module.check_install_manifest(manifest_path, codex_home)

            self.assertEqual(result.status, doctor_module.CheckStatus.FAIL)
            self.assertIn("physical identity alias", result.message)

    def test_install_rejects_hardlink_ownership_alias_with_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            installed = run_install_main(codex_home, agents_home)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            runtime_record = next(
                artifact for artifact in manifest["artifacts"] if artifact.get("path") == "hooks/lazy_skill_router.py"
            )
            alias_path = codex_home / "hooks" / "lazy_skill_router_alias.py"
            os.link(codex_home / str(runtime_record["path"]), alias_path)
            alias_record = dict(runtime_record)
            alias_record["path"] = "hooks/lazy_skill_router_alias.py"
            manifest["artifacts"].append(alias_record)
            manifest["revision"] = manifest_revision(manifest["artifacts"], manifest["registration"])
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            tracked_paths = tuple(path for path in codex_home.rglob("*") if path.is_file() and path != alias_path)
            before = {path: path.read_bytes() for path in tracked_paths}

            result = run_install_main(codex_home, agents_home)

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("physical aliases", result.stderr)
            self.assertEqual({path: path.read_bytes() for path in tracked_paths}, before)

    def test_manifest_revision_is_stable_and_contains_only_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "private-codex"
            runtime = codex_home / "hooks" / "lazy_skill_router.py"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("print('ok')\n", encoding="utf-8")

            first = build_install_manifest(
                codex_home,
                ((runtime, "managed"),),
                "python3 hook.py",
                generated_at="2026-07-10T00:00:00Z",
            )
            second = build_install_manifest(
                codex_home,
                ((runtime, "managed"),),
                "python3 hook.py",
                generated_at="2026-07-11T00:00:00Z",
            )

        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["artifacts"][0]["path"], "hooks/lazy_skill_router.py")
        self.assertNotIn(str(codex_home), json.dumps(first))

    def test_install_writes_manifest_and_doctor_detects_managed_runtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
            snapshot = load_install_manifest(manifest_path)
            self.assertEqual(snapshot.state, "available")
            artifact_paths = {artifact["path"] for artifact in snapshot.artifacts}
            self.assertIn("hooks/lazy_skill_router_contracts.py", artifact_paths)
            self.assertIn("hooks/lazy_skill_router_inventory.py", artifact_paths)
            self.assertIn("hooks/lazy_skill_router_capability_index.py", artifact_paths)
            self.assertIn("hooks/lazy_skill_router_retrieval.py", artifact_paths)
            self.assertIn("lazy-skill-router/capability-index.json", artifact_paths)
            capability_artifact = next(
                artifact
                for artifact in snapshot.artifacts
                if artifact["path"] == "lazy-skill-router/capability-index.json"
            )
            self.assertEqual(capability_artifact["ownership"], "generated")

            runtime = codex_home / "hooks" / "lazy_skill_router_core.py"
            runtime.write_text(runtime.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            doctor = run_command(DOCTOR_PATH, codex_home, agents_home=agents_home)

        self.assertEqual(doctor.returncode, 1)
        self.assertIn("[FAIL] install ownership manifest validates: managed artifact drift", doctor.stdout)

    def test_install_auto_upgrades_a_matching_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill, marker = seed_installed_skill(codex_home, "managed")

            dry_run = run_install_main(codex_home, agents_home, "--dry-run")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("would upgrade existing skill", dry_run.stdout)
            self.assertTrue(marker.is_file())

            with mock.patch.object(install, "write_skill_inventory", side_effect=OSError("injected failure")):
                failed_install = run_install_main(codex_home, agents_home)
            self.assertEqual(failed_install.returncode, 1)
            self.assertIn("injected failure", failed_install.stderr)
            self.assertTrue(marker.is_file())

            completed = run_install_main(codex_home, agents_home)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("upgraded existing skill", completed.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(
                (skill / "SKILL.md").read_bytes(),
                (ROOT / "skills" / "personal-skill-router" / "SKILL.md").read_bytes(),
            )
            self.assertEqual(skill_ownership(codex_home), "managed")

    def test_install_preserves_a_modified_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            _, marker = seed_installed_skill(codex_home, "managed")
            marker.write_text("user-modified skill\n", encoding="utf-8")

            dry_run = run_install_main(codex_home, agents_home, "--dry-run")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("would keep existing skill", dry_run.stdout)

            completed = run_install_main(codex_home, agents_home)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("kept existing skill", completed.stdout)
            self.assertEqual(marker.read_text(encoding="utf-8"), "user-modified skill\n")
            self.assertEqual(skill_ownership(codex_home), "preserved")
            second_dry_run = run_install_main(codex_home, agents_home, "--dry-run")
            self.assertEqual(second_dry_run.returncode, 0, second_dry_run.stderr)
            self.assertIn("would keep existing skill", second_dry_run.stdout)

    def test_install_preserves_a_skill_with_preserved_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            _, marker = seed_installed_skill(codex_home, "preserved")

            dry_run = run_install_main(codex_home, agents_home, "--dry-run")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("would keep existing skill", dry_run.stdout)
            self.assertTrue(marker.is_file())

            completed = run_install_main(codex_home, agents_home)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(marker.is_file())
            self.assertEqual(skill_ownership(codex_home), "preserved")

    def test_force_replaces_a_preserved_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            _, marker = seed_installed_skill(codex_home, "preserved")

            dry_run = run_install_main(codex_home, agents_home, "--force", "--dry-run")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("would upgrade existing skill", dry_run.stdout)
            self.assertTrue(marker.is_file())

            completed = run_install_main(codex_home, agents_home, "--force")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("upgraded existing skill", completed.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(skill_ownership(codex_home), "managed")

    def test_doctor_does_not_execute_a_modified_managed_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            marker = root / "executed.txt"
            install = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            hook = codex_home / "hooks" / "lazy_skill_router.py"
            hook.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "print('{}')\n",
                encoding="utf-8",
            )

            doctor = run_command(DOCTOR_PATH, codex_home, agents_home=agents_home)

        self.assertEqual(doctor.returncode, 1)
        self.assertIn("managed artifact drift", doctor.stdout)
        self.assertIn("hook smoke test skipped: install ownership manifest is unhealthy", doctor.stdout)
        self.assertFalse(marker.exists())

    def test_uninstall_refuses_a_symlinked_hooks_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            codex_home.mkdir()
            outside = root / "outside-hooks.json"
            command = f"python3 {codex_home}/hooks/lazy_skill_router.py"
            original = {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": command}]},
                    ]
                }
            }
            original_bytes = (json.dumps(original, indent=2) + "\n").encode()
            outside.write_bytes(original_bytes)
            os.symlink(outside, codex_home / "hooks.json")

            uninstall = run_command(UNINSTALL_PATH, codex_home)
            outside_after = outside.read_bytes()

        self.assertEqual(uninstall.returncode, 1)
        self.assertIn("unsafe or unreadable hooks.json", uninstall.stderr)
        self.assertEqual(outside_after, original_bytes)

    def test_install_rejects_symlinked_hooks_json_without_disclosure_in_dry_and_live_modes(self) -> None:
        for extra_args in (("--dry-run",), ()):
            with self.subTest(extra_args=extra_args), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "workspace with spaces"
                codex_home = root / "Codex Home"
                agents_home = root / "Agents Home"
                codex_home.mkdir(parents=True)
                outside = root / "outside-hooks.json"
                sentinel = "BENIGN_PRIVATE_SENTINEL"
                original_bytes = (json.dumps({"hooks": {}, "api_token": sentinel}, indent=2) + "\n").encode()
                outside.write_bytes(original_bytes)
                os.symlink(outside, codex_home / "hooks.json")

                completed = run_install_main(codex_home, agents_home, *extra_args)

                self.assertEqual(completed.returncode, 1)
                self.assertIn("unsafe install target path", completed.stderr)
                self.assertNotIn(sentinel, completed.stdout)
                self.assertNotIn(sentinel, completed.stderr)
                self.assertEqual(outside.read_bytes(), original_bytes)

    def test_uninstall_removes_only_exact_owned_hook_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace with spaces"
            codex_home = root / "Codex Home"
            hook_path = codex_home / "hooks" / "lazy_skill_router.py"
            routes_path = codex_home / "lazy-skill-router" / "routes.json"
            owned_prompt = shlex.join(["python3", str(hook_path), "--config", str(routes_path), "--legacy"])
            owned_stop = shlex.join(
                ["python3", str(hook_path), "--config", str(routes_path), "--legacy", "--hook-event", "stop"]
            )
            foreign_prompt = shlex.join(["python3", "foreign guard.py", "--note", "lazy_skill_router.py"])
            foreign_stop = shlex.join(["python3", "foreign stop.py", "--note", "lazy_skill_router.py"])
            hooks_path = codex_home / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": foreign_prompt},
                                        {"type": "command", "command": owned_prompt},
                                    ]
                                }
                            ],
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": foreign_stop},
                                        {"type": "command", "command": owned_stop},
                                    ]
                                }
                            ],
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = build_install_manifest(
                codex_home,
                (),
                owned_prompt,
                stop_hook_command=owned_stop,
            )
            manifest_path = codex_home / "lazy-skill-router" / "install.manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            completed = run_command(UNINSTALL_PATH, codex_home)
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            prompt_commands = [item["command"] for item in hooks["hooks"]["UserPromptSubmit"][0]["hooks"]]
            stop_commands = [item["command"] for item in hooks["hooks"]["Stop"][0]["hooks"]]

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(prompt_commands, [foreign_prompt])
        self.assertEqual(stop_commands, [foreign_stop])

    def test_uninstall_preserves_modified_file_and_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            modified = codex_home / "hooks" / "lazy_skill_router_core.py"
            modified.write_text(modified.read_text(encoding="utf-8") + "# user change\n", encoding="utf-8")
            symlink = codex_home / "hooks" / "lazy_skill_router_logging.py"
            symlink.unlink()
            target = root / "outside-target.py"
            target.write_text("outside\n", encoding="utf-8")
            os.symlink(target, symlink)

            uninstall = run_command(UNINSTALL_PATH, codex_home, "--remove-files")

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(modified.is_file())
            self.assertTrue(symlink.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "outside\n")
            self.assertFalse((codex_home / "hooks" / "lazy_skill_router.py").exists())

        self.assertIn("kept modified managed artifact", uninstall.stdout)
        self.assertIn("kept symlink managed artifact", uninstall.stdout)

    def test_install_refuses_symlinked_runtime_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            outside_hooks = root / "outside-hooks"
            codex_home.mkdir()
            outside_hooks.mkdir()
            os.symlink(outside_hooks, codex_home / "hooks")

            install = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)

            outside_files = tuple(outside_hooks.iterdir())

        self.assertEqual(install.returncode, 1)
        self.assertIn("unsafe install target path", install.stderr)
        self.assertEqual(outside_files, ())

    def test_doctor_and_uninstall_do_not_follow_symlinked_artifact_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_command(INSTALL_PATH, codex_home, agents_home=agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            installed_hooks = codex_home / "hooks"
            outside_hooks = root / "outside-hooks"
            shutil.copytree(installed_hooks, outside_hooks)
            shutil.rmtree(installed_hooks)
            os.symlink(outside_hooks, installed_hooks)
            outside_hook = outside_hooks / "lazy_skill_router.py"
            outside_bytes = outside_hook.read_bytes()

            doctor = run_command(DOCTOR_PATH, codex_home, agents_home=agents_home)
            uninstall = run_command(UNINSTALL_PATH, codex_home, "--remove-files")

            outside_after = outside_hook.read_bytes()
            parent_is_symlink = installed_hooks.is_symlink()

        self.assertEqual(doctor.returncode, 1)
        self.assertNotIn("Traceback", doctor.stderr)
        self.assertIn("(unsafe)", doctor.stdout)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("kept unsafe artifact path hooks/lazy_skill_router.py", uninstall.stdout)
        self.assertTrue(parent_is_symlink)
        self.assertEqual(outside_after, outside_bytes)

    def test_uninstall_rejects_manifest_reached_through_symlinked_parent_or_leaf(self) -> None:
        for symlink_kind in ("parent", "leaf"):
            with self.subTest(symlink_kind=symlink_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "workspace with spaces"
                codex_home = root / "Codex Home"
                victim = codex_home / "hooks" / "owned.py"
                victim.parent.mkdir(parents=True)
                victim.write_text("owned\n", encoding="utf-8")
                external_manifest_dir = root / "external manifest"
                external_manifest_dir.mkdir(parents=True)
                manifest = build_install_manifest(
                    codex_home,
                    ((victim, "managed"),),
                    "python3 old-hook.py",
                )
                external_manifest = external_manifest_dir / "install.manifest.json"
                external_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
                manifest_parent = codex_home / "lazy-skill-router"
                if symlink_kind == "parent":
                    os.symlink(external_manifest_dir, manifest_parent)
                    active_link = manifest_parent
                else:
                    manifest_parent.mkdir()
                    active_link = manifest_parent / "install.manifest.json"
                    os.symlink(external_manifest, active_link)

                completed = run_command(UNINSTALL_PATH, codex_home, "--remove-files")

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("ownership manifest path is unsafe", completed.stdout)
                self.assertTrue(victim.exists())
                self.assertTrue(external_manifest.exists())
                self.assertTrue(active_link.is_symlink())


if __name__ == "__main__":
    unittest.main()
