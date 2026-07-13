from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_capability_index import build_capability_index
from lazy_skill_router_inventory import InventorySnapshot

ROOT = Path(__file__).resolve().parents[1]
EVAL_MODULE_PATH = ROOT / "eval_capability_retrieval.py"
EVAL_FIXTURES_PATH = ROOT / "eval" / "capability_retrieval.jsonl"


def load_eval_module():
    spec = importlib.util.spec_from_file_location("eval_capability_retrieval", EVAL_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load eval_capability_retrieval module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_capability_retrieval"] = module
    spec.loader.exec_module(module)
    return module


eval_capability_retrieval = load_eval_module()


SKILL_DESCRIPTIONS = {
    "ponytail": (
        "Use ponytail for the smallest working code change, minimal implementation, YAGNI, "
        "and no unnecessary dependency."
    ),
    "personal-skill-router": (
        "Inspect installed skills, decide which skill applies, and synchronize lazy-skill-router policy."
    ),
    "superpowers": (
        "Brainstorm options, compare tradeoffs, debug, and make a step-by-step implementation plan before coding."
    ),
    "security-threat-model": (
        "Create or explain a repository-grounded security threat model with assets, trust boundaries, "
        "attacker capabilities, and abuse paths."
    ),
    "codex-security:security-scan": (
        "Run a standard single-pass security audit or scan of an entire repository, not a pull request diff."
    ),
    "skill-creator": (
        "Create a new Codex skill or update an existing skill, SKILL.md, workflow, and specialized instructions."
    ),
    "skill-installer": "Install a curated Codex skill into CODEX_HOME skills from a GitHub repository path.",
    "github:gh-address-comments": (
        "Inspect unresolved GitHub pull request review threads and address requested inline review comments."
    ),
    "code-review": "Review a pull request diff for regressions, correctness issues, and code-quality risks.",
    "github:github": (
        "Summarize GitHub repository and pull request status or context before choosing a specific workflow."
    ),
}


def inventory() -> InventorySnapshot:
    skills = tuple(
        {
            "configured_name": name,
            "canonical_id": f"test/skills/{name}",
            "description": description,
            "aliases": [],
            "capabilities": [],
            "phases": [],
            "availability": {"status": "available"},
        }
        for name, description in SKILL_DESCRIPTIONS.items()
    )
    return InventorySnapshot("available", "sha256:retrieval-eval-fixture", skills)


def write_index(root: Path, source: InventorySnapshot) -> Path:
    index_path = root / "capability-index.json"
    index_path.write_text(
        json.dumps(
            build_capability_index(source, generated_at="2026-07-12T00:00:00Z"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return index_path


class CapabilityRetrievalEvalTest(unittest.TestCase):
    def test_all_contrast_fixtures_pass_with_crafted_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = inventory()
            cases = eval_capability_retrieval.load_cases(EVAL_FIXTURES_PATH)
            evaluations = eval_capability_retrieval.evaluate_cases(cases, source, write_index(Path(temp_dir), source))
            report = eval_capability_retrieval.report_payload(evaluations)

        self.assertEqual(len(cases), 12)
        self.assertEqual(report["failed"], 0, report["failures"])
        self.assertEqual(report["recallAt3"], {"passed": 12, "total": 12})
        self.assertEqual(report["top1"], {"passed": 12, "total": 12})
        self.assertLessEqual(report["maxCandidatesObserved"], 3)

    def test_evaluator_reports_top1_and_recall_mismatches(self) -> None:
        source = inventory()
        case = eval_capability_retrieval.RetrievalCase(
            case_id="wrong-expectations",
            category="unit",
            prompt="Install a curated Codex skill from GitHub.",
            top1="skill-creator",
            contains=("skill-creator", "security-threat-model"),
            not_top1=(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluation = eval_capability_retrieval.evaluate_case(case, source, write_index(Path(temp_dir), source))

        messages = [failure.message for failure in evaluation.failures]
        self.assertTrue(any("top1 expected 'skill-creator'" in message for message in messages))
        self.assertTrue(any("Recall@3 missing 'security-threat-model'" in message for message in messages))

    def test_load_cases_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.jsonl"
            path.write_text(
                '{"id":"bad","category":"unit","prompt":"x","top1":"a","contains":["a"],"activation":true}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown fields: activation"):
                eval_capability_retrieval.load_cases(path)


if __name__ == "__main__":
    unittest.main()
