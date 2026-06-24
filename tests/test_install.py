from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "install.py"


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


if __name__ == "__main__":
    unittest.main()
