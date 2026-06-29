from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "install.py"
DOCTOR_PATH = ROOT / "doctor.py"


def write_skill(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


class InstallTest(unittest.TestCase):
    def test_installer_generates_routes_smokes_hook_then_registers_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("generate routes", completed.stdout)
            self.assertIn("validate generated routes", completed.stdout)
            self.assertIn("smoke test hook", completed.stdout)
            self.assertIn("added hook entry", completed.stdout)

            routes = json.loads((codex_home / "lazy-skill-router" / "routes.json").read_text(encoding="utf-8"))
            self.assertEqual(routes["allowedSkills"], ["personal-skill-router"])
            self.assertEqual(routes["routes"][0]["primary"], "personal-skill-router")

            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            hook_command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            self.assertIn("lazy_skill_router.py", hook_command)

    def test_installer_can_enable_visible_router_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--show-router-notice",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("enable visible router notice", completed.stdout)
            routes = json.loads((codex_home / "lazy-skill-router" / "routes.json").read_text(encoding="utf-8"))
            self.assertEqual(routes["display"], {"showRouterNotice": True})

            hook = subprocess.run(
                [
                    sys.executable,
                    str(codex_home / "hooks" / "lazy_skill_router.py"),
                    "--config",
                    str(codex_home / "lazy-skill-router" / "routes.json"),
                    "--prompt",
                    "스킬 추천해줘",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertIn("Visible notice requested", hook.stdout)
            self.assertIn("`lazy-skill-router`", hook.stdout)
            self.assertNotIn("lazy-skill-router:", hook.stdout)

    def test_installer_does_not_register_hook_when_template_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            template_path = root / "routes.template.json"
            template_path.write_text(
                json.dumps(
                    {
                        "routes": [
                            {
                                "name": "missing",
                                "primaryCandidates": ["not-installed"],
                                "patterns": ["missing"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--template",
                    str(template_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("generated 0 routes", completed.stderr)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_installer_dry_run_prints_planned_hooks_diff_without_writing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Planned hooks.json diff:", completed.stdout)
            self.assertIn('+    "UserPromptSubmit": [', completed.stdout)
            self.assertIn("lazy_skill_router.py", completed.stdout)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_doctor_reports_installed_hook_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            install = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            doctor = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn("[OK] Codex home found", doctor.stdout)
            self.assertIn("[OK] routes.json validates", doctor.stdout)
            self.assertIn("[OK] UserPromptSubmit hook registered", doctor.stdout)
            self.assertIn("[OK] hook dry-run smoke test passed", doctor.stdout)
            self.assertIn("[OK] skill sync checked", doctor.stdout)

    def test_doctor_warns_duplicate_skill_names_with_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            install = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            write_skill(agents_home / "skills" / "personal-skill-router-copy" / "SKILL.md", "personal-skill-router")

            doctor = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn("[WARN] skill sync checked: 1 duplicate skill name", doctor.stdout)
            self.assertIn("not an install failure", doctor.stdout)
            self.assertIn("examples: personal-skill-router (2 copies:", doctor.stdout)
            self.assertIn("run sync_skills.py --json for full paths", doctor.stdout)

    def test_doctor_fails_when_hook_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            doctor = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(doctor.returncode, 1)
            self.assertIn("[FAIL] Codex home found", doctor.stdout)
            self.assertIn("[FAIL] UserPromptSubmit hook registered", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
