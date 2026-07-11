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

            def fail_hooks_write(path: Path, data: dict[str, object]) -> None:
                if path == hooks_path:
                    raise OSError("injected hooks write failure")
                real_write_json(path, data)

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

            recovered = install.recover_pending_transactions(codex_home)
            transaction.temp_dir = None

            restored = existing.read_text(encoding="utf-8")
            created_exists = created.exists()
            journal_exists = journal_root.exists()

        self.assertEqual(recovered, 1)
        self.assertEqual(restored, "original\n")
        self.assertFalse(created_exists)
        self.assertFalse(journal_exists)

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

    def test_recovery_rejects_journal_paths_outside_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            outside = root / "outside.txt"
            outside.write_text("keep\n", encoding="utf-8")
            journal_root = root / f"{install.TRANSACTION_PREFIX}malicious"
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
