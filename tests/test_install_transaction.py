from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install


class InstallTransactionTest(unittest.TestCase):
    def test_transaction_cleanup_preserves_replacement_at_private_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            target = codex_home / "hooks" / "runtime.py"
            transaction = install.InstallMutation(codex_home, (target,))
            transaction.__enter__()
            transaction_root = transaction.temp_dir
            moved_transaction = root / "moved-owned-transaction"
            transaction_root.rename(moved_transaction)
            transaction_root.mkdir()
            sentinel = transaction_root / "user-sentinel.txt"
            sentinel_bytes = b"preserve concurrent directory\n"
            sentinel.write_bytes(sentinel_bytes)

            with self.assertRaisesRegex(install.InstallError, "transaction root identity changed"):
                transaction.cleanup_temp_dir()

            self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
            self.assertTrue(moved_transaction.is_dir())
            transaction.temp_dir = None

    def test_transaction_cleanup_preserves_unknown_entry_in_owned_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            target = codex_home / "hooks" / "runtime.py"
            transaction = install.InstallMutation(codex_home, (target,))
            transaction.__enter__()
            transaction_root = transaction.temp_dir
            sentinel = transaction_root / "unknown-user-entry.txt"
            sentinel_bytes = b"preserve same-inode concurrent entry\n"
            sentinel.write_bytes(sentinel_bytes)

            with self.assertRaisesRegex(install.InstallError, "unexpected entries"):
                transaction.cleanup_temp_dir()

            self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
            self.assertTrue(transaction_root.is_dir())
            transaction.temp_dir = None

    def test_transaction_root_creation_rejects_parent_swap_without_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent = root / "install-parent"
            codex_home = parent / "codex"
            target = codex_home / "hooks" / "runtime.py"
            external_parent = root / "external-parent"
            external_parent.mkdir()
            sentinel = external_parent / "sentinel.txt"
            sentinel_bytes = b"external state\n"
            sentinel.write_bytes(sentinel_bytes)
            real_create = install.confined_create_private_directory
            swapped = False

            def swap_parent_before_create(path: Path, prefix: str):
                nonlocal swapped
                if not swapped:
                    saved_parent = root / "saved-install-parent"
                    parent.rename(saved_parent)
                    parent.symlink_to(external_parent, target_is_directory=True)
                    swapped = True
                return real_create(path, prefix)

            transaction = install.InstallMutation(codex_home, (target,))
            with (
                mock.patch.object(
                    install,
                    "confined_create_private_directory",
                    side_effect=swap_parent_before_create,
                ),
                self.assertRaises((install.InstallError, OSError, ValueError)),
            ):
                transaction.__enter__()

            self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
            self.assertEqual(
                tuple(path.name for path in external_parent.iterdir()),
                ("sentinel.txt",),
            )

    def test_recovery_rejects_symlinked_parent_without_path_glob_or_external_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external_parent = root / "external-parent"
            external_parent.mkdir()
            external_transaction = external_parent / f"{install.TRANSACTION_PREFIX}external"
            external_transaction.mkdir()
            sentinel = external_transaction / "journal.json"
            sentinel_bytes = b'{"external":"sentinel"}\n'
            sentinel.write_bytes(sentinel_bytes)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(external_parent, target_is_directory=True)
            linked_codex_home = linked_parent / "codex"

            with (
                mock.patch.object(Path, "glob", side_effect=AssertionError("path glob must not be used")),
                self.assertRaises((install.InstallError, OSError, ValueError)),
            ):
                install.recover_pending_transactions(linked_codex_home)

            self.assertEqual(sentinel.read_bytes(), sentinel_bytes)

    def test_transaction_rollback_preserves_concurrent_replacement_and_restores_other_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            targets = tuple(codex_home / "hooks" / f"runtime-{index}.py" for index in range(2))
            sources = tuple(root / f"source-{index}.py" for index in range(2))
            originals = {path: f"original {index}\n".encode() for index, path in enumerate(targets)}
            for path, content in originals.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for index, source in enumerate(sources):
                source.write_text(f"installed {index}\n", encoding="utf-8")
            transaction = install.InstallMutation(codex_home, targets)
            transaction.__enter__()
            try:
                for source, target in zip(sources, targets):
                    install.copy_file(
                        source,
                        target,
                        dry_run=False,
                        codex_root=codex_home,
                        mutation=transaction,
                    )
                concurrent_bytes = b"concurrent user replacement\n"
                targets[0].unlink()
                targets[0].write_bytes(concurrent_bytes)

                with self.assertRaisesRegex(install.InstallError, "preserving concurrent replacement"):
                    transaction.rollback()

                self.assertEqual(targets[0].read_bytes(), concurrent_bytes)
                self.assertEqual(targets[1].read_bytes(), originals[targets[1]])
            finally:
                transaction.cleanup_temp_dir()

    def test_copy_file_rejects_leaf_and_parent_swap_without_touching_external_state(self) -> None:
        for swap_kind in ("leaf", "parent"):
            with self.subTest(swap_kind=swap_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                source = root / "trusted-runtime.py"
                source.write_text("replacement runtime\n", encoding="utf-8")
                destination = codex_home / "hooks" / "runtime.py"
                destination.parent.mkdir(parents=True)
                destination.write_text("installed runtime\n", encoding="utf-8")
                external_root = root / "external-hooks"
                external_root.mkdir()
                external = root / "outside-runtime.py" if swap_kind == "leaf" else external_root / destination.name
                external_bytes = b"outside sentinel\n"
                external.write_bytes(external_bytes)
                real_check = install.checked_install_path
                swapped = False

                def swap_after_check(
                    codex_root: Path,
                    path: Path,
                    *,
                    allow_leaf_symlink: bool,
                    real_check=real_check,
                    destination=destination,
                    swap_kind=swap_kind,
                    external=external,
                    root=root,
                    external_root=external_root,
                ) -> Path:
                    nonlocal swapped
                    result = real_check(codex_root, path, allow_leaf_symlink=allow_leaf_symlink)
                    if path != destination or swapped:
                        return result
                    if swap_kind == "leaf":
                        destination.unlink()
                        destination.symlink_to(external)
                    else:
                        original_parent = root / "original-hooks"
                        destination.parent.rename(original_parent)
                        destination.parent.symlink_to(external_root, target_is_directory=True)
                    swapped = True
                    return result

                with (
                    mock.patch.object(install, "checked_install_path", side_effect=swap_after_check),
                    self.assertRaises((install.InstallError, OSError, ValueError)),
                ):
                    install.copy_file(source, destination, dry_run=False, codex_root=codex_home)

                self.assertEqual(external.read_bytes(), external_bytes)

    def test_generated_installer_helpers_reject_parent_swap_without_external_writes(self) -> None:
        for helper_kind in ("inventory", "capability-index", "skill"):
            with self.subTest(helper_kind=helper_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                agents_home = root / "agents"
                if helper_kind == "skill":
                    destination = codex_home / "skills" / "personal-skill-router"
                else:
                    name = "skills.manifest.json" if helper_kind == "inventory" else "capability-index.json"
                    destination = codex_home / "lazy-skill-router" / name
                destination.parent.mkdir(parents=True)
                external_parent = root / f"external-{helper_kind}"
                external_parent.mkdir()
                external_destination = external_parent / destination.name
                sentinel_bytes = b"external sentinel\n"
                if helper_kind != "skill":
                    external_destination.write_bytes(sentinel_bytes)
                real_check = install.checked_install_path
                swapped = False

                def swap_parent_after_check(
                    codex_root: Path,
                    path: Path,
                    *,
                    allow_leaf_symlink: bool,
                    real_check=real_check,
                    destination=destination,
                    root=root,
                    helper_kind=helper_kind,
                    external_parent=external_parent,
                ) -> Path:
                    nonlocal swapped
                    result = real_check(codex_root, path, allow_leaf_symlink=allow_leaf_symlink)
                    if path == destination and not swapped:
                        saved_parent = root / f"saved-{helper_kind}"
                        destination.parent.rename(saved_parent)
                        destination.parent.symlink_to(external_parent, target_is_directory=True)
                        swapped = True
                    return result

                with (
                    mock.patch.object(install, "checked_install_path", side_effect=swap_parent_after_check),
                    self.assertRaises((install.InstallError, OSError, ValueError)),
                ):
                    if helper_kind == "inventory":
                        install.write_skill_inventory(
                            destination,
                            codex_home,
                            agents_home,
                            dry_run=False,
                        )
                    elif helper_kind == "capability-index":
                        inventory_data = {
                            "schema": "lazy-skill-router.skill-inventory/v1",
                            "revision": install.inventory_revision([]),
                            "skills": [],
                        }
                        install.write_capability_index(
                            destination,
                            destination.with_name("skills.manifest.json"),
                            codex_home,
                            dry_run=False,
                            inventory_data=inventory_data,
                        )
                    else:
                        install.copy_skill(
                            destination,
                            force=True,
                            dry_run=False,
                            codex_root=codex_home,
                        )

                if helper_kind == "skill":
                    self.assertFalse(external_destination.exists())
                else:
                    self.assertEqual(external_destination.read_bytes(), sentinel_bytes)

    def test_copy_file_rejects_source_leaf_parent_and_root_symlinks(self) -> None:
        for swap_kind in ("leaf", "parent", "root"):
            with self.subTest(swap_kind=swap_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                destination = codex_home / "hooks" / "runtime.py"
                destination.parent.mkdir(parents=True)
                destination_bytes = b"installed original\n"
                destination.write_bytes(destination_bytes)
                real_source_parent = root / "trusted-source"
                real_source_parent.mkdir()
                real_source = real_source_parent / "runtime.py"
                real_source.write_text("trusted source\n", encoding="utf-8")
                external_parent = root / "external-source"
                external_parent.mkdir()
                external_source = external_parent / real_source.name
                external_source.write_text("external poison\n", encoding="utf-8")

                if swap_kind == "root":
                    source_root = root / "symlinked-source-root"
                    source_root.symlink_to(real_source_parent, target_is_directory=True)
                    source = source_root / real_source.name
                    context = contextlib.nullcontext()
                else:
                    source = real_source
                    real_read = install.confined_read_bytes
                    swapped = False

                    def swap_before_confined_read(
                        path,
                        managed_root,
                        expected,
                        swap_kind=swap_kind,
                        real_source=real_source,
                        external_source=external_source,
                        root=root,
                        real_source_parent=real_source_parent,
                        external_parent=external_parent,
                        real_read=real_read,
                    ):
                        nonlocal swapped
                        if not swapped:
                            if swap_kind == "leaf":
                                real_source.unlink()
                                real_source.symlink_to(external_source)
                            else:
                                saved_parent = root / "saved-source"
                                real_source_parent.rename(saved_parent)
                                real_source_parent.symlink_to(external_parent, target_is_directory=True)
                            swapped = True
                        return real_read(path, managed_root, expected)

                    context = mock.patch.object(
                        install,
                        "confined_read_bytes",
                        side_effect=swap_before_confined_read,
                    )

                with context, self.assertRaises((install.InstallError, OSError, ValueError)):
                    install.copy_file(
                        source,
                        destination,
                        dry_run=False,
                        codex_root=codex_home,
                    )

                self.assertEqual(destination.read_bytes(), destination_bytes)

    def test_install_rejects_hooks_leaf_swap_after_backup_without_touching_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            initial_argv = [
                "install.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
            ]
            with (
                mock.patch.object(sys, "argv", initial_argv),
                mock.patch.object(install, "smoke_staged_hook"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(install.main(), 0)

            hooks_path = codex_home / "hooks.json"
            sentinel = root / "outside-hooks.json"
            sentinel_bytes = b'{"sentinel":"keep"}\n'
            sentinel.write_bytes(sentinel_bytes)
            real_write_json = install.write_json

            def swap_leaf_after_preflight(path: Path, data: dict[str, object], **kwargs) -> None:
                if path == hooks_path:
                    path.unlink()
                    path.symlink_to(sentinel)
                return real_write_json(path, data, **kwargs)

            update_argv = [*initial_argv, "--enable-measurement"]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(sys, "argv", update_argv),
                mock.patch.object(install, "smoke_staged_hook"),
                mock.patch.object(install, "write_json", side_effect=swap_leaf_after_preflight),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = install.main()

            sentinel_after = sentinel.read_bytes()
            hooks_is_symlink = hooks_path.is_symlink()
            hooks_after = hooks_path.read_bytes()

        self.assertEqual(result, 1, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(sentinel_after, sentinel_bytes)
        self.assertTrue(hooks_is_symlink)
        self.assertEqual(hooks_after, sentinel_bytes)

    def test_failure_after_runtime_copy_restores_preexisting_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = codex_home / "lazy-skill-router" / "routes.json"
            routes_path.parent.mkdir(parents=True)
            original_routes = (
                b'{"allowedSkills":["personal-skill-router"],"routes":'
                b'[{"name":"only","primary":"personal-skill-router","patterns":["only"]}]}\n'
            )
            routes_path.write_bytes(original_routes)
            stderr = io.StringIO()

            argv = [
                "install.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(install, "write_skill_inventory", side_effect=OSError("injected failure")),
                contextlib.redirect_stderr(stderr),
            ):
                result = install.main()

            target_files = tuple(
                sorted(path.relative_to(codex_home).as_posix() for path in codex_home.rglob("*") if path.is_file())
            )
            restored_routes = routes_path.read_bytes()

        self.assertEqual(result, 1)
        self.assertIn("injected failure", stderr.getvalue())
        self.assertEqual(target_files, ("lazy-skill-router/routes.json",))
        self.assertEqual(restored_routes, original_routes)

    def test_capability_index_failure_rolls_back_inventory_and_new_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = codex_home / "lazy-skill-router" / "routes.json"
            routes_path.parent.mkdir(parents=True)
            original_routes = (
                b'{"allowedSkills":["personal-skill-router"],"routes":'
                b'[{"name":"only","primary":"personal-skill-router","patterns":["only"]}]}\n'
            )
            routes_path.write_bytes(original_routes)
            stderr = io.StringIO()
            argv = [
                "install.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
            ]

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(install, "write_capability_index", side_effect=OSError("index failure")),
                contextlib.redirect_stderr(stderr),
            ):
                result = install.main()

            target_files = tuple(
                sorted(path.relative_to(codex_home).as_posix() for path in codex_home.rglob("*") if path.is_file())
            )

        self.assertEqual(result, 1)
        self.assertIn("index failure", stderr.getvalue())
        self.assertEqual(target_files, ("lazy-skill-router/routes.json",))

    def test_hooks_write_failure_restores_user_hooks_and_removes_transaction_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            hooks_path = codex_home / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            original_hooks = json.dumps({"hooks": {"UserPromptSubmit": []}}, indent=2).encode() + b"\n"
            hooks_path.write_bytes(original_hooks)
            real_write_json = install.write_json

            def fail_hooks_write(path: Path, data: dict[str, object], **kwargs) -> None:
                if path == hooks_path:
                    raise OSError("injected hooks write failure")
                return real_write_json(path, data, **kwargs)

            argv = [
                "install.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(install, "write_json", side_effect=fail_hooks_write),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = install.main()

            target_files = tuple(
                sorted(path.relative_to(codex_home).as_posix() for path in codex_home.rglob("*") if path.is_file())
            )
            restored_hooks = hooks_path.read_bytes()

        self.assertEqual(result, 1)
        self.assertEqual(target_files, ("hooks.json",))
        self.assertEqual(restored_hooks, original_hooks)

    def test_next_install_can_recover_a_persisted_interrupted_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            existing = codex_home / "hooks" / "lazy_skill_router.py"
            created = codex_home / "lazy-skill-router" / "skills.manifest.json"
            existing.parent.mkdir(parents=True)
            existing.write_text("original\n", encoding="utf-8")

            transaction = install.InstallMutation(codex_home, (existing, created))
            transaction.__enter__()
            journal_root = transaction.temp_dir
            existing.write_text("interrupted\n", encoding="utf-8")
            created.parent.mkdir(parents=True)
            created.write_text("partial\n", encoding="utf-8")
            transaction.record_committed(
                existing,
                install.confined_path_identity(existing, codex_home),
            )
            transaction.record_committed(
                created,
                install.confined_path_identity(created, codex_home),
            )

            recovered = install.recover_pending_transactions(codex_home)
            transaction.temp_dir = None

            restored = existing.read_text(encoding="utf-8")
            created_exists = created.exists()
            journal_exists = journal_root.exists()

        self.assertEqual(recovered, 1)
        self.assertEqual(restored, "original\n")
        self.assertFalse(created_exists)
        self.assertFalse(journal_exists)

    def test_recovery_preserves_uncommitted_user_replacement_and_restores_committed_peer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            committed = codex_home / "hooks" / "committed.py"
            uncommitted = codex_home / "hooks" / "uncommitted.py"
            committed.parent.mkdir(parents=True)
            committed_original = b"committed original\n"
            uncommitted_original = b"uncommitted original\n"
            committed.write_bytes(committed_original)
            uncommitted.write_bytes(uncommitted_original)
            source = root / "trusted-source.py"
            source.write_text("installer replacement\n", encoding="utf-8")
            transaction = install.InstallMutation(codex_home, (committed, uncommitted))
            transaction.__enter__()
            install.copy_file(
                source,
                committed,
                dry_run=False,
                codex_root=codex_home,
                mutation=transaction,
            )
            user_bytes = b"user replacement after journal snapshot\n"
            uncommitted.unlink()
            uncommitted.write_bytes(user_bytes)
            journal_root = transaction.temp_dir

            with self.assertRaisesRegex(install.InstallError, "preserving concurrent replacement"):
                install.recover_pending_transactions(codex_home)
            transaction.temp_dir = None

            self.assertEqual(committed.read_bytes(), committed_original)
            self.assertEqual(uncommitted.read_bytes(), user_bytes)
            self.assertTrue(journal_root.is_dir())

    def test_install_dry_run_does_not_recover_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            existing = codex_home / "hooks" / "lazy_skill_router.py"
            created = codex_home / "lazy-skill-router" / "skills.manifest.json"
            existing.parent.mkdir(parents=True)
            existing.write_text("original\n", encoding="utf-8")

            transaction = install.InstallMutation(codex_home, (existing, created))
            transaction.__enter__()
            journal_root = transaction.temp_dir
            existing.write_text("interrupted\n", encoding="utf-8")
            created.parent.mkdir(parents=True)
            created.write_text("partial\n", encoding="utf-8")

            argv = [
                "install.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
                "--dry-run",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = install.main()

            existing_value = existing.read_text(encoding="utf-8")
            created_exists = created.exists()
            created_value = created.read_text(encoding="utf-8") if created_exists else None
            journal_exists = journal_root.exists()

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertIn("would recover 1 interrupted install transaction", stdout.getvalue())
        self.assertEqual(existing_value, "interrupted\n")
        self.assertTrue(created_exists)
        self.assertEqual(created_value, "partial\n")
        self.assertTrue(journal_exists)

    def test_recovery_blocks_a_forged_sibling_journal_without_creating_a_codex_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            codex_home.mkdir()
            forged_target = codex_home / "hooks" / "forged.py"
            journal_root = root / f"{install.TRANSACTION_PREFIX}forged"
            journal_root.mkdir(mode=0o700)
            backup = journal_root / "0"
            backup.write_text("forged payload\n", encoding="utf-8")
            backup_identity = install.confined_path_identity(backup, journal_root)
            transaction_root_identity = install.confined_path_identity(
                journal_root,
                root,
                allow_leaf_symlink=True,
            )
            journal = {
                "schema": install.TRANSACTION_JOURNAL_SCHEMA,
                "root_fingerprint": install.codex_root_fingerprint(codex_home),
                "transaction_root_identity": {
                    "device": transaction_root_identity.device,
                    "inode": transaction_root_identity.inode,
                },
                "snapshots": [
                    {
                        "path": "hooks/forged.py",
                        "kind": "file",
                        "backup": "0",
                        "link_target": None,
                        "state": "available",
                        "device": 1,
                        "inode": 1,
                        "mode": 0o600,
                        "size": backup_identity.size,
                        "digest": backup_identity.digest,
                        "backup_identity": {
                            "state": backup_identity.state,
                            "kind": backup_identity.kind,
                            "device": backup_identity.device,
                            "inode": backup_identity.inode,
                            "mode": backup_identity.mode,
                            "size": backup_identity.size,
                            "digest": backup_identity.digest,
                        },
                    }
                ],
                "created_paths": [],
                "created_parents": ["hooks"],
                "committed": [
                    {
                        "path": "hooks/forged.py",
                        "state": "missing",
                        "kind": None,
                        "device": None,
                        "inode": None,
                        "mode": None,
                        "size": None,
                        "digest": None,
                    }
                ],
            }
            (journal_root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

            with self.assertRaisesRegex(install.InstallError, "legacy interrupted install transaction"):
                install.recover_pending_transactions(codex_home)

            self.assertFalse(forged_target.exists())
            self.assertTrue(journal_root.is_dir())

    def test_recovery_blocks_a_legacy_sibling_journal_instead_of_silently_abandoning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            partial_target = codex_home / "hooks" / "lazy_skill_router.py"
            partial_target.parent.mkdir(parents=True)
            partial_target.write_text("partial v0.4 install\n", encoding="utf-8")
            journal_root = root / f"{install.TRANSACTION_PREFIX}legacy-v04"
            journal_root.mkdir(mode=0o700)
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema": install.TRANSACTION_JOURNAL_SCHEMA,
                        "root_fingerprint": install.codex_root_fingerprint(codex_home),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(install.InstallError, "legacy interrupted install transaction"):
                install.recover_pending_transactions(codex_home)

            self.assertEqual(partial_target.read_text(encoding="utf-8"), "partial v0.4 install\n")
            self.assertTrue(journal_root.is_dir())

    def test_recovery_rejects_journal_paths_outside_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            codex_home.mkdir()
            outside = root / "outside.txt"
            outside.write_text("keep\n", encoding="utf-8")
            journal_root = codex_home / f"{install.TRANSACTION_PREFIX}malicious"
            journal_root.mkdir()
            journal = {
                "schema": install.TRANSACTION_JOURNAL_SCHEMA,
                "root_fingerprint": install.codex_root_fingerprint(codex_home),
                "snapshots": [{"path": "../outside.txt", "kind": "missing", "backup": None, "link_target": None}],
                "created_paths": [],
                "created_parents": [],
            }
            (journal_root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

            with self.assertRaises(install.InstallError):
                install.recover_pending_transactions(codex_home)

            outside_value = outside.read_text(encoding="utf-8")

        self.assertEqual(outside_value, "keep\n")

    def test_recovery_skips_symlinked_transaction_root_or_journal(self) -> None:
        for symlink_kind in ("transaction-root", "journal"):
            with self.subTest(symlink_kind=symlink_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                victim = codex_home / "hooks" / "victim.py"
                victim.parent.mkdir(parents=True)
                victim.write_text("keep\n", encoding="utf-8")
                journal = {
                    "schema": install.TRANSACTION_JOURNAL_SCHEMA,
                    "root_fingerprint": install.codex_root_fingerprint(codex_home),
                    "snapshots": [{"path": "hooks/victim.py", "kind": "missing", "backup": None, "link_target": None}],
                    "created_paths": [],
                    "created_parents": [],
                }
                journal_root = root / f"{install.TRANSACTION_PREFIX}malicious"
                external_root = root / "external-journal"
                external_root.mkdir()
                external_journal = external_root / "journal.json"
                external_journal.write_text(json.dumps(journal), encoding="utf-8")
                if symlink_kind == "transaction-root":
                    journal_root.symlink_to(external_root, target_is_directory=True)
                else:
                    journal_root.mkdir()
                    (journal_root / "journal.json").symlink_to(external_journal)

                recovered = install.recover_pending_transactions(codex_home)

                self.assertEqual(recovered, 0)
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")
                self.assertTrue(external_journal.exists())


if __name__ == "__main__":
    unittest.main()
