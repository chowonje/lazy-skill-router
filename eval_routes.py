from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_core import answer_only_patterns, dry_run_output, text_matches


ALLOWED_EXPECTATIONS = {
    "answerOnly",
    "confidenceLabel",
    "primary",
    "route",
    "score",
    "shouldInject",
    "supporting",
    "verification",
}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    prompt: str
    expect: dict[str, Any]


@dataclass(frozen=True)
class EvaluationFailure:
    case_id: str
    category: str
    message: str

    def format(self) -> str:
        return f"{self.case_id} [{self.category}]: {self.message}"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"config root must be an object: {path}")
    return loaded


def require_string(raw: dict[str, Any], field: str, path: Path, line_number: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}:{line_number}: {field} must be a non-empty string")
    return value


def require_expect(raw: dict[str, Any], path: Path, line_number: int) -> dict[str, Any]:
    value = raw.get("expect")
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{path}:{line_number}: expect must be a non-empty object")
    unknown = sorted(set(value) - ALLOWED_EXPECTATIONS)
    if unknown:
        raise ValueError(f"{path}:{line_number}: unknown expectation keys: {', '.join(unknown)}")
    return value


def parse_case(raw: Any, path: Path, line_number: int) -> EvaluationCase:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
    return EvaluationCase(
        case_id=require_string(raw, "id", path, line_number),
        category=require_string(raw, "category", path, line_number),
        prompt=require_string(raw, "prompt", path, line_number),
        expect=require_expect(raw, path, line_number),
    )


def load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    cases: list[EvaluationCase] = []
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
    return tuple(cases)


def actual_result(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    result = dry_run_output(prompt, config)
    result["answerOnly"] = text_matches(prompt, answer_only_patterns(config))
    return result


def compare_case(case: EvaluationCase, config: dict[str, Any]) -> tuple[EvaluationFailure, ...]:
    actual = actual_result(case.prompt, config)
    failures: list[EvaluationFailure] = []
    for key, expected in case.expect.items():
        observed = actual.get(key)
        if observed != expected:
            failures.append(EvaluationFailure(case.case_id, case.category, f"{key} expected {expected!r}, got {observed!r}"))
    return tuple(failures)


def evaluate_cases(cases: tuple[EvaluationCase, ...], config: dict[str, Any]) -> tuple[EvaluationFailure, ...]:
    failures: list[EvaluationFailure] = []
    for case in cases:
        failures.extend(compare_case(case, config))
    return tuple(failures)


def category_counts(cases: tuple[EvaluationCase, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    return dict(sorted(counts.items()))


def print_text_report(cases: tuple[EvaluationCase, ...], failures: tuple[EvaluationFailure, ...]) -> None:
    print(f"Evaluated {len(cases)} prompts across {len(category_counts(cases))} categories")
    for category, count in category_counts(cases).items():
        print(f"- {category}: {count}")
    if not failures:
        print("OK: route eval fixtures passed")
        return
    print(f"FAIL: {len(failures)} expectation failure(s)")
    for failure in failures:
        print(f"- {failure.format()}")


def json_report(cases: tuple[EvaluationCase, ...], failures: tuple[EvaluationFailure, ...]) -> dict[str, Any]:
    return {
        "total": len(cases),
        "failed": len(failures),
        "passed": len(cases) - len({failure.case_id for failure in failures}),
        "categories": category_counts(cases),
        "failures": [
            {"id": failure.case_id, "category": failure.category, "message": failure.message}
            for failure in failures
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate lazy-skill-router golden prompt fixtures.")
    parser.add_argument("cases", nargs="?", default="eval/prompts.jsonl", help="Prompt JSONL fixture path.")
    parser.add_argument("--config", default="routes.default.json", help="Routes JSON file.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    failures = evaluate_cases(cases, load_config(Path(args.config)))
    if args.json:
        print(json.dumps(json_report(cases, failures), ensure_ascii=False, indent=2))
    else:
        print_text_report(cases, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
