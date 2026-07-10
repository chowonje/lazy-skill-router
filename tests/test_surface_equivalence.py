from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from generate_routes import generate_config
from lazy_skill_router_core import dry_run_output

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HOOK = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"
DEFAULT_CONFIG = ROOT / "routes.default.json"
TEMPLATE_CONFIG = ROOT / "routes.template.json"
ROUTED_PROMPT = "PDF 만들어줘"
NO_ROUTE_PROMPT = "hello"
COMPARISON_PROMPTS = (
    "PDF 만들어줘",
    "GitHub PR에서 CI 실패 고쳐줘",
    "프론트엔드 UI 수정해줘",
    "그냥 설명만 PDF 만들어줘",
    NO_ROUTE_PROMPT,
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_json_command(args: list[str], stdin: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def decision_tuple(result: dict[str, Any]) -> tuple[bool, str | None, str | None, bool]:
    return (
        result["shouldInject"],
        result.get("route"),
        result.get("primary"),
        result["answerOnly"],
    )


class SurfaceEquivalenceTest(unittest.TestCase):
    def assert_decision_tuple_equal(self, left: dict[str, Any], right: dict[str, Any]) -> None:
        self.assertEqual(decision_tuple(left), decision_tuple(right))

    def test_source_dry_run_and_packaged_cli_json_return_identical_diagnostics(self) -> None:
        source_result = run_json_command(
            [
                sys.executable,
                str(SOURCE_HOOK),
                "--config",
                str(DEFAULT_CONFIG),
                "--dry-run",
                ROUTED_PROMPT,
            ]
        )
        cli_result = run_json_command(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--json",
                "--config",
                str(DEFAULT_CONFIG),
                ROUTED_PROMPT,
            ]
        )

        self.assertEqual(source_result, cli_result)

    def test_hook_prose_derives_selected_values_from_diagnostics(self) -> None:
        diagnostics = run_json_command(
            [
                sys.executable,
                str(SOURCE_HOOK),
                "--config",
                str(DEFAULT_CONFIG),
                "--dry-run",
                ROUTED_PROMPT,
            ]
        )
        hook_output = run_json_command(
            [
                sys.executable,
                str(SOURCE_HOOK),
                "--config",
                str(DEFAULT_CONFIG),
            ],
            stdin=json.dumps({"prompt": ROUTED_PROMPT}),
        )

        context = hook_output["hookSpecificOutput"]["additionalContext"]
        signals = diagnostics["matchedSignals"]
        signal_text = ", ".join(signals) if signals else "none"
        self.assertIn(f"Route: {diagnostics['route']}", context)
        self.assertIn(f"Primary skill: {diagnostics['primary']}", context)
        self.assertIn(f"Selection score: {diagnostics['score']:.2f}", context)
        self.assertIn(f"Matched signals: {signal_text}", context)

    def test_generated_config_matches_default_decision_tuple_with_explicit_installed_skills(self) -> None:
        default_config = load_json(DEFAULT_CONFIG)
        template = load_json(TEMPLATE_CONFIG)
        installed_skills = set(default_config["allowedSkills"])
        generated = generate_config(template, installed_skills).config

        for prompt in COMPARISON_PROMPTS:
            with self.subTest(prompt=prompt):
                self.assert_decision_tuple_equal(
                    dry_run_output(prompt, default_config),
                    dry_run_output(prompt, generated),
                )

        self.assertEqual(
            decision_tuple(dry_run_output(ROUTED_PROMPT, {"routes": []})),
            (False, None, None, False),
        )

    def test_decision_tuple_comparison_fails_when_returned_field_is_mutated_in_memory(self) -> None:
        default_config = load_json(DEFAULT_CONFIG)
        baseline = dry_run_output(ROUTED_PROMPT, default_config)
        mutated = copy.deepcopy(baseline)
        mutated["primary"] = "mutated-primary"

        with self.assertRaises(AssertionError):
            self.assert_decision_tuple_equal(baseline, mutated)


if __name__ == "__main__":
    unittest.main()
