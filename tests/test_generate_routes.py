from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATE_PATH = ROOT / "generate_routes.py"
VALIDATOR_PATH = ROOT / "validate_routes.py"


def load_generate_module():
    spec = importlib.util.spec_from_file_location("generate_routes", GENERATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load generate_routes module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_routes"] = module
    spec.loader.exec_module(module)
    return module


generate_routes = load_generate_module()


def load_validator_module():
    spec = importlib.util.spec_from_file_location("validate_routes", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load validate_routes module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_routes"] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator_module()


def write_skill(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")


class GenerateRoutesTest(unittest.TestCase):
    def test_generates_routes_from_installed_candidate_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            write_skill(codex_home / "skills" / "ponytail-lite" / "SKILL.md", "ponytail-lite")
            write_skill(codex_home / "skills" / "code-navigation" / "SKILL.md", "code-navigation")
            write_skill(codex_home / "skills" / "verification-gate" / "SKILL.md", "verification-gate")

            template = {
                "version": 1,
                "minConfidence": 0.55,
                "defaultVerificationCandidates": ["verification-gate"],
                "logging": {"enabled": False, "path": ""},
                "answerOnlyPatterns": ["설명만"],
                "routes": [
                    {
                        "name": "programming",
                        "primaryCandidates": ["custom:programming", "ponytail-lite"],
                        "supportingCandidates": ["ponytail-lite", "code-navigation", "custom:lsp"],
                        "verificationCandidates": ["verification-gate"],
                        "reason": "Implementation language was detected.",
                        "patterns": ["구현", {"regex": "fix", "label": "Fix keyword"}],
                        "excludePatterns": ["설명만"],
                        "priority": 2,
                        "weight": 0.1,
                        "fallback": True,
                    },
                    {
                        "name": "missing-primary",
                        "primaryCandidates": ["custom:frontend"],
                        "patterns": ["ui"],
                    },
                ],
            }

            installed = generate_routes.installed_skill_names(codex_home, agents_home)
            result = generate_routes.generate_config(template, installed)

        self.assertEqual(result.skipped_routes, ("missing-primary",))
        self.assertEqual(result.config["allowedSkills"], ["code-navigation", "ponytail-lite", "verification-gate"])
        self.assertEqual(result.config["defaultVerification"], "verification-gate")
        self.assertEqual(
            result.config["routes"],
            [
                {
                    "name": "programming",
                    "primary": "ponytail-lite",
                    "supporting": ["code-navigation"],
                    "verification": "verification-gate",
                    "reason": "Implementation language was detected.",
                    "patterns": ["구현", {"regex": "fix", "label": "Fix keyword"}],
                    "excludePatterns": ["설명만"],
                    "priority": 2,
                    "weight": 0.1,
                    "fallback": True,
                }
            ],
        )
        self.assertEqual([finding.message for finding in validator.validate_config(result.config)], [])

    def test_plugin_skill_frontmatter_names_can_satisfy_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            write_skill(
                codex_home / "plugins" / "cache" / "acme" / "custom" / "4.13.0" / "skills" / "programming" / "SKILL.md",
                "programming",
            )
            template = {
                "routes": [
                    {
                        "name": "code",
                        "primaryCandidates": ["custom:programming", "ponytail-lite"],
                        "patterns": ["code"],
                    }
                ]
            }

            installed = generate_routes.installed_skill_names(codex_home, agents_home)
            result = generate_routes.generate_config(template, installed)

        self.assertEqual(result.config["allowedSkills"], ["custom:programming"])
        self.assertEqual(result.config["routes"][0]["primary"], "custom:programming")

    def test_ambiguous_duplicate_skill_names_are_not_automatic_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            write_skill(codex_home / "skills" / "same" / "SKILL.md", "same")
            write_skill(agents_home / "skills" / "same" / "SKILL.md", "same")

            installed = generate_routes.installed_skill_names(codex_home, agents_home)
            result = generate_routes.generate_config(
                {"routes": [{"name": "same", "primaryCandidates": ["same"], "patterns": ["same"]}]},
                installed,
            )

        self.assertNotIn("same", installed)
        self.assertEqual(result.config["routes"], [])
        self.assertEqual(result.skipped_routes, ("same",))

    def test_cli_writes_generated_routes_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            template_path = root / "routes.template.json"
            output_path = root / "routes.json"
            write_skill(codex_home / "skills" / "pdf" / "SKILL.md", "pdf")
            template_path.write_text(
                json.dumps(
                    {
                        "routes": [
                            {
                                "name": "pdf",
                                "primaryCandidates": ["pdf"],
                                "patterns": ["pdf"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATE_PATH),
                    "--template",
                    str(template_path),
                    "--output",
                    str(output_path),
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
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["routes"][0]["primary"], "pdf")
            self.assertIn("generated 1 routes", completed.stdout)


if __name__ == "__main__":
    unittest.main()
