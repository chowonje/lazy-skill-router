from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_cli.cli import source_version

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
        self.assertIn("route", completed.stdout)
        self.assertIn("outcome", completed.stdout)
        self.assertIn("report", completed.stdout)
        self.assertIn("sync", completed.stdout)
        self.assertIn("policy", completed.stdout)
        self.assertIn("catalog", completed.stdout)
        self.assertNotIn("eval", completed.stdout)
        self.assertNotIn("generate-routes", completed.stdout)

    def test_cli_version_matches_project_version(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", CLI_MODULE, "--version"],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"lazy-skill-router {source_version()}")

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

    def test_cli_route_prints_selected_skill_summary(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--config",
                "routes.default.json",
                "GitHub PR에서 CI 실패 고쳐줘",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Route: github-ci", completed.stdout)
        self.assertIn("Activation: activate (eligible)", completed.stdout)
        self.assertIn("Primary skill: github:gh-fix-ci", completed.stdout)
        self.assertIn("Confidence:", completed.stdout)

    def test_cli_route_json_reuses_dry_run_diagnostics(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--json",
                "--config",
                "routes.default.json",
                "PDF 만들어줘",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["shouldInject"])
        self.assertEqual(result["route"], "pdf")
        self.assertEqual(result["primary"], "pdf")
        self.assertFalse(result["shouldActivate"])
        self.assertEqual(result["activationDecision"], "propose")
        self.assertIn("candidates", result)

    def test_cli_route_exposes_activation_ir_contract(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--activation-ir-json",
                "--config",
                "routes.default.json",
                "스킬을 왜 사용하게 되는지 설명해줘",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["schema"], "lazy-skill-router.activation-ir/v1")
        self.assertEqual(result["disposition"], "abstain")
        self.assertEqual(result["reasonCode"], "meta_context")
        self.assertEqual(result["activatedSkills"], [])

    def test_cli_route_explains_meta_abstention_without_recommending_a_skill(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--config",
                "routes.default.json",
                "스킬을 왜 사용하게 되는지 설명해줘",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("No route", completed.stdout)
        self.assertIn("Activation: abstain (meta_context)", completed.stdout)
        self.assertIn("Diagnostic route: skill-routing", completed.stdout)
        self.assertNotIn("Primary skill:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
