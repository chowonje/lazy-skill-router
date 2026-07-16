#!/usr/bin/env python3
"""Focused tests for the local judge-demo materializer."""

import contextlib
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import prepare_judge_demo as demo_module
from prepare_judge_demo import prepare_demo


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


class PrepareJudgeDemoTests(unittest.TestCase):
    def test_cli_success_output_does_not_expose_absolute_output_paths(self) -> None:
        output = Path("/Users/private-user/Desktop/Private Demo")
        session = output / "session-001"
        stdout = io.StringIO()

        with (
            mock.patch.object(demo_module, "prepare_demo", return_value=session),
            mock.patch.object(demo_module, "_source_revision", return_value="abc123"),
            contextlib.redirect_stdout(stdout),
        ):
            status = demo_module.main(["--output-root", str(output)])

        rendered = stdout.getvalue()
        self.assertEqual(status, 0)
        self.assertNotIn("/Users/private-user", rendered)
        self.assertNotIn("Private Demo", rendered)
        self.assertIn("session-001", rendered)
        self.assertIn("01-mindmap", rendered)

    def setUp(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root)
        self.root = Path(self.temporary.name)
        self.source = self.root / "fixture"
        self.source.mkdir()
        (self.source / "README.md").write_text("fixture\n", encoding="utf-8")
        package = self.source / "package"
        package.mkdir()
        (package / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "app.pyc").write_bytes(b"generated")
        self.allowed_files = ("README.md", "package/app.py")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_three_independent_matching_copies(self) -> None:
        output = self.root / "output"

        session = prepare_demo(
            self.source,
            output,
            source_revision="abc123",
            allowed_files=self.allowed_files,
        )

        expected = _tree_digest(self.source)
        for name in ("01-mindmap", "02-ponytail", "03-security"):
            self.assertEqual(_tree_digest(session / name), expected)
            self.assertFalse((session / name / "package" / "__pycache__").exists())
        (session / "01-mindmap" / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual((session / "02-ponytail" / "README.md").read_text(encoding="utf-8"), "fixture\n")

    def test_rerun_preserves_the_first_session(self) -> None:
        output = self.root / "output"
        first = prepare_demo(
            self.source,
            output,
            source_revision="abc123",
            allowed_files=self.allowed_files,
        )
        before = _tree_digest(first)

        second = prepare_demo(
            self.source,
            output,
            source_revision="abc123",
            allowed_files=self.allowed_files,
        )

        self.assertEqual(first.name, "session-001")
        self.assertEqual(second.name, "session-002")
        self.assertEqual(_tree_digest(first), before)
        self.assertEqual((output / "CURRENT.txt").read_text(encoding="utf-8"), "session-002\n")

    def test_rejects_source_symlink(self) -> None:
        target = self.root / "outside.txt"
        target.write_text("sentinel\n", encoding="utf-8")
        os.symlink(target, self.source / "linked.txt")

        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare_demo(
                self.source,
                self.root / "output",
                source_revision="abc123",
                allowed_files=self.allowed_files,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_rejects_unowned_existing_output(self) -> None:
        output = self.root / "output"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "not owned"):
            prepare_demo(
                self.source,
                output,
                source_revision="abc123",
                allowed_files=self.allowed_files,
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_copies_only_explicitly_allowed_files(self) -> None:
        secret = self.source / ".env.local"
        secret.write_text("TOKEN=do-not-copy\n", encoding="utf-8")

        session = prepare_demo(
            self.source,
            self.root / "output",
            source_revision="abc123",
            allowed_files=self.allowed_files,
        )

        for name in ("01-mindmap", "02-ponytail", "03-security"):
            self.assertFalse((session / name / secret.name).exists())
            self.assertEqual((session / name / "README.md").read_text(encoding="utf-8"), "fixture\n")

    def test_rejects_overlapping_source_and_output_before_writing(self) -> None:
        for output in (self.source / "generated-demo", self.source.parent):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "overlap"):
                prepare_demo(
                    self.source,
                    output,
                    source_revision="abc123",
                    allowed_files=self.allowed_files,
                )

        self.assertFalse((self.source / "generated-demo").exists())

    def test_tracked_source_change_after_clean_check_never_reaches_copies(self) -> None:
        repository = self.root / "repository"
        fixture = repository / "examples" / "demo"
        fixture.mkdir(parents=True)
        tracked = fixture / "README.md"
        tracked.write_text("committed fixture\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture Test",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=repository,
            check=True,
        )

        real_run = subprocess.run
        changed = False

        def mutate_after_clean_check(*args, **kwargs):
            nonlocal changed
            result = real_run(*args, **kwargs)
            command = args[0] if args else kwargs.get("args")
            if not changed and command[:3] == ["git", "diff", "--quiet"] and result.returncode == 0:
                tracked.write_text("changed after clean check\n", encoding="utf-8")
                changed = True
            return result

        with (
            mock.patch.object(demo_module, "_repo_root", return_value=repository),
            mock.patch.object(demo_module.subprocess, "run", side_effect=mutate_after_clean_check),
        ):
            session = prepare_demo(
                fixture,
                self.root / "output",
                source_revision="abc123",
            )

        self.assertTrue(changed)
        for scenario in ("01-mindmap", "02-ponytail", "03-security"):
            copied = (session / scenario / "README.md").read_text(encoding="utf-8")
            self.assertEqual(copied, "committed fixture\n")

    def test_output_root_replacement_after_marker_validation_gets_zero_writes(self) -> None:
        output = self.root / "output"
        output.mkdir()
        (output / demo_module._MARKER).write_text(demo_module._MARKER_CONTENT, encoding="utf-8")
        displaced = self.root / "validated-output"
        sentinel = output / "sentinel.txt"
        original_ensure = demo_module._ensure_owned_root

        def replace_after_validation(path: Path):
            identity = original_ensure(path)
            path.rename(displaced)
            path.mkdir()
            sentinel.write_text("unowned\n", encoding="utf-8")
            return identity

        with (
            mock.patch.object(demo_module, "_ensure_owned_root", side_effect=replace_after_validation),
            self.assertRaisesRegex(ValueError, "changed|identity|owned"),
        ):
            prepare_demo(
                self.source,
                output,
                source_revision="abc123",
                allowed_files=self.allowed_files,
            )

        self.assertEqual({path.name for path in output.iterdir()}, {"sentinel.txt"})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unowned\n")

    def test_output_swap_after_identity_check_never_writes_to_replacement(self) -> None:
        output = self.root / "output"
        output.mkdir()
        (output / demo_module._MARKER).write_text(demo_module._MARKER_CONTENT, encoding="utf-8")
        displaced = self.root / "validated-output"
        sentinel = output / "sentinel.txt"
        original_require = demo_module._require_output_root_identity
        checks = 0

        def replace_after_check(path: Path, expected_identity: tuple[int, int]) -> None:
            nonlocal checks
            original_require(path, expected_identity)
            checks += 1
            if checks == 2:
                path.rename(displaced)
                path.mkdir()
                sentinel.write_text("unowned\n", encoding="utf-8")

        with (
            mock.patch.object(demo_module, "_require_output_root_identity", side_effect=replace_after_check),
            self.assertRaisesRegex(ValueError, "changed|identity|owned"),
        ):
            prepare_demo(
                self.source,
                output,
                source_revision="abc123",
                allowed_files=self.allowed_files,
            )

        self.assertEqual({path.name for path in output.iterdir()}, {"sentinel.txt"})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unowned\n")

    def test_tracked_fixture_files_exclude_untracked_and_reject_dirty_content(self) -> None:
        repository = self.root / "repository"
        fixture = repository / "examples" / "demo"
        fixture.mkdir(parents=True)
        (fixture / "README.md").write_text("fixture\n", encoding="utf-8")
        package = fixture / "package"
        package.mkdir()
        (package / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture Test",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=repository,
            check=True,
        )
        (fixture / ".env.local").write_text("TOKEN=untracked\n", encoding="utf-8")

        self.assertEqual(
            demo_module._tracked_fixture_files(repository, fixture),
            ("README.md", "package/app.py"),
        )

        (fixture / "README.md").write_text("dirty secret\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            demo_module._tracked_fixture_files(repository, fixture)


if __name__ == "__main__":
    unittest.main()
