from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import preserve_router_ab_bundle
from tests.test_router_ab_eval import manifest, write_experiment_inputs


def experiment_files(root: Path) -> tuple[Path, Path, Path, Path]:
    config, inventory, index, frozen = write_experiment_inputs(root)
    manifest_path = root / "experiment.json"
    manifest_path.write_text(json.dumps(manifest(frozen, "Private replay prompt")), encoding="utf-8")
    return config, inventory, index, manifest_path


class PreserveRouterABBundleTest(unittest.TestCase):
    def test_preserves_verified_private_bundle_and_deduplicates_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            store = root / "private-store"
            config, inventory, index, manifest_path = experiment_files(source)

            first = preserve_router_ab_bundle.preserve_bundle(
                store=store,
                name="control",
                config=config,
                inventory=inventory,
                index=index,
                manifest=manifest_path,
            )
            second = preserve_router_ab_bundle.preserve_bundle(
                store=store,
                name="replay",
                config=config,
                inventory=inventory,
                index=index,
                manifest=manifest_path,
            )
            refs = sorted((store / "refs").glob("*.json"))
            blobs = sorted((store / "blobs" / "sha256").iterdir())
            blob_modes = [path.stat().st_mode & 0o777 for path in blobs]

        self.assertEqual(first["validationStatus"], "passed")
        self.assertEqual(second["validationStatus"], "passed")
        self.assertNotEqual(first["descriptorRevision"], second["descriptorRevision"])
        self.assertEqual(first["experimentManifestRevision"], second["experimentManifestRevision"])
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(blobs), 4)
        self.assertTrue(all(mode == 0o600 for mode in blob_modes))
        self.assertFalse(first["sourcePathsStored"])

    def test_rejects_symlink_source_and_existing_blob_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            store = root / "private-store"
            config, inventory, index, manifest_path = experiment_files(source)
            symlink = source / "config-link.json"
            symlink.symlink_to(config)

            with self.assertRaisesRegex(ValueError, "symlink"):
                preserve_router_ab_bundle.preserve_bundle(
                    store=store,
                    name="unsafe",
                    config=symlink,
                    inventory=inventory,
                    index=index,
                    manifest=manifest_path,
                )

            result = preserve_router_ab_bundle.preserve_bundle(
                store=store,
                name="control",
                config=config,
                inventory=inventory,
                index=index,
                manifest=manifest_path,
            )
            descriptor_digest = result["descriptorRevision"].removeprefix("sha256:")
            descriptor = json.loads(
                (store / "manifests" / "sha256" / f"{descriptor_digest}.json").read_text(encoding="utf-8")
            )
            config_blob = next(item["blob"] for item in descriptor["artifacts"] if item["role"] == "config")
            (store / config_blob).write_text("tampered", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "content verification"):
                preserve_router_ab_bundle.preserve_bundle(
                    store=store,
                    name="second",
                    config=config,
                    inventory=inventory,
                    index=index,
                    manifest=manifest_path,
                )

    def test_ref_name_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            store = root / "private-store"
            config, inventory, index, manifest_path = experiment_files(source)
            preserve_router_ab_bundle.preserve_bundle(
                store=store,
                name="control",
                config=config,
                inventory=inventory,
                index=index,
                manifest=manifest_path,
            )
            extra = source / "extra.json"
            extra.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "different content"):
                preserve_router_ab_bundle.preserve_bundle(
                    store=store,
                    name="control",
                    config=config,
                    inventory=inventory,
                    index=index,
                    manifest=manifest_path,
                    extras={"provenance": extra},
                )

    def test_concurrent_immutable_writers_cannot_both_win(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "same.json"
            barrier = threading.Barrier(2)
            original_link = os.link
            outcomes: list[str] = []

            def racing_link(source: Path, destination: Path, **kwargs: object) -> None:
                if Path(destination) == target:
                    barrier.wait(timeout=5)
                original_link(source, destination, **kwargs)

            def write(content: bytes) -> None:
                try:
                    preserve_router_ab_bundle.write_immutable(target, content)
                except ValueError:
                    outcomes.append("rejected")
                else:
                    outcomes.append("stored")

            with mock.patch.object(preserve_router_ab_bundle.os, "link", side_effect=racing_link):
                threads = [threading.Thread(target=write, args=(content,)) for content in (b"first\n", b"second\n")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertEqual(sorted(outcomes), ["rejected", "stored"])
            self.assertIn(target.read_bytes(), {b"first\n", b"second\n"})

    def test_source_symlink_swap_is_rejected_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.json"
            outside = root / "outside.json"
            source.write_text("source", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")
            original_resolve = Path.resolve
            original_open = os.open
            swapped = False

            def swap_source() -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    source.unlink()
                    source.symlink_to(outside)

            def racing_resolve(path: Path, *args: object, **kwargs: object) -> Path:
                if path == source:
                    swap_source()
                return original_resolve(path, *args, **kwargs)

            def racing_open(path: os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
                if Path(path) == source:
                    swap_source()
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(Path, "resolve", racing_resolve),
                mock.patch.object(preserve_router_ab_bundle.os, "open", side_effect=racing_open),
                self.assertRaisesRegex(ValueError, "symlink"),
            ):
                preserve_router_ab_bundle.source_fd(source)

    def test_store_directory_swap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = root / "store"
            outside = root / "outside"
            store.mkdir()
            outside.mkdir()
            original_resolve = Path.resolve
            swapped = False

            def racing_resolve(path: Path, *args: object, **kwargs: object) -> Path:
                nonlocal swapped
                if path == store and not swapped:
                    swapped = True
                    store.rmdir()
                    store.symlink_to(outside, target_is_directory=True)
                return original_resolve(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "resolve", racing_resolve),
                self.assertRaisesRegex(ValueError, "changed"),
            ):
                preserve_router_ab_bundle.ensure_private_directory(store)

    def test_mkstemp_failure_closes_source_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.json"
            blobs = root / "blobs"
            source.write_text("source", encoding="utf-8")
            blobs.mkdir()
            opened: list[int] = []
            original_source_fd = preserve_router_ab_bundle.source_fd

            def tracked_source_fd(path: Path) -> tuple[int, os.stat_result]:
                file_fd, file_stat = original_source_fd(path)
                opened.append(file_fd)
                return file_fd, file_stat

            with (
                mock.patch.object(preserve_router_ab_bundle, "source_fd", side_effect=tracked_source_fd),
                mock.patch.object(preserve_router_ab_bundle.tempfile, "mkstemp", side_effect=OSError("unavailable")),
                self.assertRaises(OSError),
            ):
                preserve_router_ab_bundle.store_blob("source", source, blobs)

            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])

    def test_existing_blob_change_during_verification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blob = Path(temp_dir) / "blob"
            blob.write_bytes(b"original")
            digest = preserve_router_ab_bundle.hashlib.sha256(b"original").hexdigest()
            original_read = preserve_router_ab_bundle.read_fd_digest

            def racing_read(
                file_fd: int,
                maximum: int = preserve_router_ab_bundle.MAX_REPLAY_ARTIFACT_BYTES,
            ) -> tuple[str, int]:
                result = original_read(file_fd, maximum)
                blob.write_bytes(b"tampered-longer")
                return result

            with (
                mock.patch.object(preserve_router_ab_bundle, "read_fd_digest", side_effect=racing_read),
                self.assertRaisesRegex(ValueError, "changed"),
            ):
                preserve_router_ab_bundle.verify_existing_blob(blob, digest, len(b"original"))

    def test_validation_failure_leaves_no_canonical_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            store = root / "private-store"
            config, inventory, index, manifest_path = experiment_files(source)
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw["frozen"]["configRevision"] = "sha256:" + "0" * 64
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "frozen input mismatch"):
                preserve_router_ab_bundle.preserve_bundle(
                    store=store,
                    name="invalid",
                    config=config,
                    inventory=inventory,
                    index=index,
                    manifest=manifest_path,
                )

            self.assertEqual(list((store / "blobs" / "sha256").iterdir()), [])
            self.assertEqual(list(store.glob(".stage-*")), [])


if __name__ == "__main__":
    unittest.main()
