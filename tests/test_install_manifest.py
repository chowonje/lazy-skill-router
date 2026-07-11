from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install
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
