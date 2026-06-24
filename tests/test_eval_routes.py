from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVAL_MODULE_PATH = ROOT / "eval_routes.py"
EVAL_FIXTURES_PATH = ROOT / "eval" / "prompts.jsonl"
CONFIG_PATH = ROOT / "routes.default.json"


def load_eval_module():
    spec = importlib.util.spec_from_file_location("eval_routes", EVAL_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load eval_routes module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_routes"] = module
    spec.loader.exec_module(module)
    return module


eval_routes = load_eval_module()


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class EvalRoutesTest(unittest.TestCase):
    def test_default_prompt_fixtures_pass(self) -> None:
        cases = eval_routes.load_cases(EVAL_FIXTURES_PATH)
        failures = eval_routes.evaluate_cases(cases, load_config())
        self.assertEqual([failure.format() for failure in failures], [])

    def test_evaluator_reports_primary_mismatch(self) -> None:
        case = eval_routes.EvaluationCase(
            case_id="wrong-primary",
            category="unit",
            prompt="PDF 만들어줘",
            expect={"shouldInject": True, "primary": "writing-polish"},
        )
        failures = eval_routes.evaluate_cases((case,), load_config())
        self.assertEqual(len(failures), 1)
        self.assertIn("primary expected 'writing-polish'", failures[0].message)

    def test_load_cases_rejects_missing_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.jsonl"
            path.write_text('{"id":"missing","category":"unit","prompt":"PDF"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                eval_routes.load_cases(path)


if __name__ == "__main__":
    unittest.main()
