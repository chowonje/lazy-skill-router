from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI_MODULE = "lazy_skill_router_cli.cli"


class CliTest(unittest.TestCase):
    def test_cli_help_exposes_only_public_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", CLI_MODULE, "--help"],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("install", completed.stdout)
        self.assertIn("doctor", completed.stdout)
        self.assertIn("uninstall", completed.stdout)
        self.assertNotIn("eval", completed.stdout)
        self.assertNotIn("generate-routes", completed.stdout)

    def test_cli_install_doctor_and_uninstall_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            install_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "install",
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(install_result.returncode, 0, install_result.stderr)
            self.assertIn("added hook entry", install_result.stdout)

            doctor_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "doctor",
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(doctor_result.returncode, 0, doctor_result.stderr)
            self.assertIn("[OK] UserPromptSubmit hook registered", doctor_result.stdout)

            uninstall_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "uninstall",
                    "--codex-home",
                    str(codex_home),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(uninstall_result.returncode, 0, uninstall_result.stderr)
            self.assertIn("remove 1 hook entry", uninstall_result.stdout)


if __name__ == "__main__":
    unittest.main()
