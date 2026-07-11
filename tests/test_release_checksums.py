from __future__ import annotations

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release_checksums as checksums


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def manifest_line(path: str, payload: bytes) -> str:
    return f"{digest(payload)}  {path}\n"


class ReleaseChecksumsTest(unittest.TestCase):
    def verify(self, root: Path, manifest: Path) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = checksums.verify_manifest(root, manifest)
        return result, output.getvalue()

    def assert_rejected_before_hashing(self, root: Path, manifest: Path, marker: str) -> None:
        with mock.patch.object(checksums, "digest_file", side_effect=AssertionError("unexpected digest")):
            result, output = self.verify(root, manifest)
        self.assertEqual(result, 1)
        self.assertIn(marker, output)
        self.assertNotIn("OK:", output)

    def test_verify_manifest_rejects_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "dist"
            write_file(root / "package.whl", b"wheel\n")
            manifest = temp / "SHA256SUMS"
            manifest.write_text("", encoding="utf-8")

            self.assert_rejected_before_hashing(root, manifest, "EMPTY")

    def test_verify_manifest_rejects_incomplete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "dist"
            first = b"wheel\n"
            write_file(root / "package.whl", first)
            write_file(root / "package.tar.gz", b"sdist\n")
            manifest = temp / "SHA256SUMS"
            manifest.write_text(manifest_line("package.whl", first), encoding="utf-8")

            self.assert_rejected_before_hashing(root, manifest, "UNLISTED package.tar.gz")

    def test_verify_manifest_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "dist"
            root.mkdir()
            outside = temp / "outside.whl"
            payload = b"outside\n"
            write_file(outside, payload)
            manifest = temp / "SHA256SUMS"
            manifest.write_text(manifest_line(str(outside), payload), encoding="utf-8")

            self.assert_rejected_before_hashing(root, manifest, "UNSAFE")

    def test_verify_manifest_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "dist"
            root.mkdir()
            payload = b"outside\n"
            write_file(temp / "outside.whl", payload)
            manifest = temp / "SHA256SUMS"
            manifest.write_text(manifest_line("../outside.whl", payload), encoding="utf-8")

            self.assert_rejected_before_hashing(root, manifest, "UNSAFE")

    def test_verify_manifest_rejects_symlinked_leaf_and_parent(self) -> None:
        for case in ("leaf", "parent"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                root = temp / "dist"
                root.mkdir()
                payload = b"outside\n"
                if case == "leaf":
                    outside = temp / "outside.whl"
                    write_file(outside, payload)
                    (root / "package.whl").symlink_to(outside)
                    manifest_path = "package.whl"
                else:
                    outside_dir = temp / "outside"
                    write_file(outside_dir / "package.whl", payload)
                    (root / "linked").symlink_to(outside_dir, target_is_directory=True)
                    manifest_path = "linked/package.whl"
                manifest = temp / "SHA256SUMS"
                manifest.write_text(manifest_line(manifest_path, payload), encoding="utf-8")

                self.assert_rejected_before_hashing(root, manifest, "UNSAFE")

    def test_write_manifest_rejects_symlinked_leaf_and_parent_before_hashing(self) -> None:
        for case in ("leaf", "parent"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                root = temp / "dist"
                root.mkdir()
                if case == "leaf":
                    outside = temp / "outside.whl"
                    write_file(outside, b"outside\n")
                    (root / "package.whl").symlink_to(outside)
                else:
                    outside_dir = temp / "outside"
                    write_file(outside_dir / "package.whl", b"outside\n")
                    (root / "linked").symlink_to(outside_dir, target_is_directory=True)
                manifest = temp / "SHA256SUMS"

                with mock.patch.object(checksums, "digest_file", side_effect=AssertionError("unexpected digest")):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        checksums.write_manifest(root, manifest)

                self.assertFalse(manifest.exists())

    def test_verify_manifest_rejects_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "dist"
            payload = b"wheel\n"
            write_file(root / "package.whl", payload)
            line = manifest_line("package.whl", payload)
            manifest = temp / "SHA256SUMS"
            manifest.write_text(line + line, encoding="utf-8")

            self.assert_rejected_before_hashing(root, manifest, "DUPLICATE package.whl")

    def test_checksum_manifest_round_trip_excludes_manifest_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_file(root / "package.whl", b"wheel\n")
            write_file(root / "package.tar.gz", b"sdist\n")
            manifest = root / "release.sha256"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                checksums.write_manifest(root, manifest)
                result = checksums.verify_manifest(root, manifest)

            self.assertEqual(result, 0)
            self.assertIn("Wrote 2 checksums", output.getvalue())
            self.assertIn("OK: verified 2 checksums from", output.getvalue())


if __name__ == "__main__":
    unittest.main()
