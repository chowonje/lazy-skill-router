from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_capability_index import load_capability_index
from lazy_skill_router_common import codex_home
from lazy_skill_router_inventory import InventorySnapshot, load_inventory_manifest
from lazy_skill_router_retrieval import retrieve_capabilities

ALLOWED_FIELDS = {"id", "category", "prompt", "top1", "contains", "notTop1"}


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    category: str
    prompt: str
    top1: str
    contains: tuple[str, ...]
    not_top1: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationFailure:
    case_id: str
    category: str
    message: str

    def format(self) -> str:
        return f"{self.case_id} [{self.category}]: {self.message}"


@dataclass(frozen=True)
class CaseEvaluation:
    case: RetrievalCase
    status: str
    candidate_names: tuple[str, ...]
    failures: tuple[EvaluationFailure, ...]

    @property
    def recall_at_3_passed(self) -> bool:
        return all(name in self.candidate_names[:3] for name in self.case.contains)

    @property
    def top1_passed(self) -> bool:
        return bool(self.candidate_names) and self.candidate_names[0] == self.case.top1


def require_string(raw: dict[str, Any], field: str, path: Path, line_number: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}:{line_number}: {field} must be a non-empty string")
    return value


def require_names(raw: dict[str, Any], field: str, path: Path, line_number: int) -> tuple[str, ...]:
    value = raw.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path}:{line_number}: {field} must be a non-empty string array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{path}:{line_number}: {field} must be a non-empty string array")
    if len(set(value)) != len(value):
        raise ValueError(f"{path}:{line_number}: {field} must not contain duplicates")
    return tuple(value)


def optional_names(raw: dict[str, Any], field: str, path: Path, line_number: int) -> tuple[str, ...]:
    if field not in raw:
        return ()
    return require_names(raw, field, path, line_number)


def parse_case(raw: Any, path: Path, line_number: int) -> RetrievalCase:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
    unknown = sorted(set(raw) - ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"{path}:{line_number}: unknown fields: {', '.join(unknown)}")
    top1 = require_string(raw, "top1", path, line_number)
    contains = require_names(raw, "contains", path, line_number)
    not_top1 = optional_names(raw, "notTop1", path, line_number)
    if top1 not in contains:
        raise ValueError(f"{path}:{line_number}: contains must include top1")
    if top1 in not_top1:
        raise ValueError(f"{path}:{line_number}: notTop1 must not include top1")
    return RetrievalCase(
        case_id=require_string(raw, "id", path, line_number),
        category=require_string(raw, "category", path, line_number),
        prompt=require_string(raw, "prompt", path, line_number),
        top1=top1,
        contains=contains,
        not_top1=not_top1,
    )


def load_cases(path: Path) -> tuple[RetrievalCase, ...]:
    cases: list[RetrievalCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            cases.append(parse_case(raw, path, line_number))
    if not cases:
        raise ValueError(f"{path}: no evaluation cases found")
    case_ids = [case.case_id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError(f"{path}: case ids must be unique")
    return tuple(cases)


def names_from_result(result: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return ()
    for candidate in candidates:
        skill_ref = candidate.get("skillRef") if isinstance(candidate, dict) else None
        name = skill_ref.get("configuredName") if isinstance(skill_ref, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def evaluate_case(case: RetrievalCase, inventory: InventorySnapshot, index_path: Path) -> CaseEvaluation:
    config = {"capabilityRetrieval": {"mode": "shadow", "maxCandidates": 3}}
    result = retrieve_capabilities(
        case.prompt,
        config,
        inventory,
        explicit_index=str(index_path),
    )
    status = str(result.get("status", "invalid"))
    names = names_from_result(result)
    failures: list[EvaluationFailure] = []
    if status != "matched":
        reasons = result.get("reasonCodes")
        failures.append(EvaluationFailure(case.case_id, case.category, f"retrieval status {status!r}: {reasons!r}"))
    if len(names) > 3:
        failures.append(
            EvaluationFailure(case.case_id, case.category, f"returned {len(names)} candidates, maximum is 3")
        )
    observed_top1 = names[0] if names else None
    if observed_top1 != case.top1:
        failures.append(
            EvaluationFailure(case.case_id, case.category, f"top1 expected {case.top1!r}, got {observed_top1!r}")
        )
    missing = tuple(name for name in case.contains if name not in names[:3])
    if missing:
        failures.append(
            EvaluationFailure(
                case.case_id,
                case.category,
                f"Recall@3 missing {', '.join(repr(name) for name in missing)}",
            )
        )
    if observed_top1 in case.not_top1:
        failures.append(EvaluationFailure(case.case_id, case.category, f"top1 must not be {observed_top1!r}"))
    return CaseEvaluation(case, status, names, tuple(failures))


def evaluate_cases(
    cases: tuple[RetrievalCase, ...],
    inventory: InventorySnapshot,
    index_path: Path,
) -> tuple[CaseEvaluation, ...]:
    return tuple(evaluate_case(case, inventory, index_path) for case in cases)


def all_failures(evaluations: tuple[CaseEvaluation, ...]) -> tuple[EvaluationFailure, ...]:
    return tuple(failure for evaluation in evaluations for failure in evaluation.failures)


def category_counts(cases: tuple[RetrievalCase, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    return dict(sorted(counts.items()))


def report_payload(evaluations: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    cases = tuple(evaluation.case for evaluation in evaluations)
    failures = all_failures(evaluations)
    failed_case_ids = {failure.case_id for failure in failures}
    recall_passed = sum(evaluation.recall_at_3_passed for evaluation in evaluations)
    top1_passed = sum(evaluation.top1_passed for evaluation in evaluations)
    max_candidates = max((len(evaluation.candidate_names) for evaluation in evaluations), default=0)
    return {
        "total": len(evaluations),
        "passed": len(evaluations) - len(failed_case_ids),
        "failed": len(failed_case_ids),
        "categories": category_counts(cases),
        "recallAt3": {"passed": recall_passed, "total": len(evaluations)},
        "top1": {"passed": top1_passed, "total": len(evaluations)},
        "maxCandidatesObserved": max_candidates,
        "failures": [
            {"id": failure.case_id, "category": failure.category, "message": failure.message} for failure in failures
        ],
    }


def print_text_report(evaluations: tuple[CaseEvaluation, ...]) -> None:
    report = report_payload(evaluations)
    print(f"Evaluated {report['total']} capability retrieval prompts across {len(report['categories'])} categories")
    for category, count in report["categories"].items():
        print(f"- {category}: {count}")
    print(f"Recall@3: {report['recallAt3']['passed']}/{report['recallAt3']['total']}")
    print(f"Top-1: {report['top1']['passed']}/{report['top1']['total']}")
    print(f"Max candidates observed: {report['maxCandidatesObserved']}")
    if not report["failures"]:
        print("OK: capability retrieval contrast fixtures passed")
        return
    print(f"FAIL: {len(report['failures'])} expectation failure(s)")
    for failure in all_failures(evaluations):
        print(f"- {failure.format()}")


def verified_inputs(inventory_path: Path, index_path: Path) -> InventorySnapshot:
    inventory = load_inventory_manifest(inventory_path)
    if inventory.state != "available" or not inventory.revision:
        reasons = ", ".join(inventory.reason_codes) or inventory.state
        raise ValueError(f"skill inventory is unavailable: {reasons}")
    index = load_capability_index(index_path)
    if index.state != "available":
        reasons = ", ".join(index.reason_codes) or index.state
        raise ValueError(f"capability index is unavailable: {reasons}")
    if index.inventory_revision != inventory.revision:
        raise ValueError("capability index is stale for the selected inventory")
    return inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate capability retrieval contrast fixtures.")
    parser.add_argument(
        "cases",
        nargs="?",
        default="eval/capability_retrieval.jsonl",
        help="Prompt JSONL fixture path.",
    )
    parser.add_argument("--inventory", help="Skill inventory manifest path.")
    parser.add_argument("--index", help="Capability index path; defaults beside the inventory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    inventory_path = (
        Path(args.inventory).expanduser()
        if args.inventory
        else codex_home() / "lazy-skill-router" / "skills.manifest.json"
    )
    index_path = Path(args.index).expanduser() if args.index else inventory_path.with_name("capability-index.json")
    try:
        cases = load_cases(Path(args.cases))
        evaluations = evaluate_cases(cases, verified_inputs(inventory_path, index_path), index_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    report = report_payload(evaluations)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(evaluations)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
