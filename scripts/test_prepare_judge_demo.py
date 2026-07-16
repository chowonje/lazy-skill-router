#!/usr/bin/env python3
"""Focused tests for the local judge-demo materializer."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_three_independent_matching_copies(self) -> None:
        output = self.root / "output"

        session = prepare_demo(self.source, output, source_revision="abc123")

        expected = _tree_digest(self.source)
        for name in ("01-mindmap", "02-ponytail", "03-security"):
            self.assertEqual(_tree_digest(session / name), expected)
            self.assertFalse((session / name / "package" / "__pycache__").exists())
        (session / "01-mindmap" / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual((session / "02-ponytail" / "README.md").read_text(encoding="utf-8"), "fixture\n")

    def test_rerun_preserves_the_first_session(self) -> None:
        output = self.root / "output"
        first = prepare_demo(self.source, output, source_revision="abc123")
        before = _tree_digest(first)

        second = prepare_demo(self.source, output, source_revision="abc123")

        self.assertEqual(first.name, "session-001")
        self.assertEqual(second.name, "session-002")
        self.assertEqual(_tree_digest(first), before)
        self.assertEqual((output / "CURRENT.txt").read_text(encoding="utf-8"), "session-002\n")

    def test_rejects_source_symlink(self) -> None:
        target = self.root / "outside.txt"
        target.write_text("sentinel\n", encoding="utf-8")
        os.symlink(target, self.source / "linked.txt")

        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare_demo(self.source, self.root / "output", source_revision="abc123")

        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_rejects_unowned_existing_output(self) -> None:
        output = self.root / "output"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "not owned"):
            prepare_demo(self.source, output, source_revision="abc123")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
