from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_install_manifest import build_install_manifest, load_install_manifest, manifest_revision

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


class InstallManifestTest(unittest.TestCase):
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

            runtime = codex_home / "hooks" / "lazy_skill_router_core.py"
            runtime.write_text(runtime.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            doctor = run_command(DOCTOR_PATH, codex_home, agents_home=agents_home)

        self.assertEqual(doctor.returncode, 1)
        self.assertIn("[FAIL] install ownership manifest validates: managed artifact drift", doctor.stdout)

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


if __name__ == "__main__":
    unittest.main()
