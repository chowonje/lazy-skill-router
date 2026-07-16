#!/usr/bin/env python3
"""Run the source-only Build Week judge playground."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lazy_skill_router_common import MAX_ROUTABLE_PROMPT_CHARS  # noqa: E402
from lazy_skill_router_contracts import (  # noqa: E402
    policy_version,
    route_result_v2,
    structured_recommendation_v1,
)
from lazy_skill_router_policy_ir import parse_policy_config  # noqa: E402

POLICY_PATH = REPO_ROOT / "docs" / "build-week" / "routes.judge-demo.json"
VERIFY_SCRIPT = REPO_ROOT / "examples" / "ci-relay-demo" / "scripts" / "verify.sh"

DEFAULT_PROMPTS = {
    "mindmap": ("Map this repository as a project mind map. Show the data flow. Do not modify files."),
    "ponytail": ("Add one retry on TimeoutError. Make the smallest correct change and add no dependency."),
    "abstain": "Refactor the database layer and add caching.",
}

_CASE_LABELS = {
    "mindmap": "Project Mindmap",
    "ponytail": "Ponytail minimal change",
    "abstain": "Unsupported task",
    "custom": "Custom prompt",
}

_DEFAULT_EXPECTATIONS = {
    "mindmap": ("matched", "propose", "answer_only", "project-mindmap"),
    "ponytail": ("matched", "activate", "eligible", "ponytail"),
    "abstain": ("no-match", "abstain", "no_candidate", None),
}


def _load_policy() -> dict[str, Any]:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("judge policy must be an object")
    if not isinstance(policy.get("policyVersion"), str):
        raise ValueError("judge policy version is missing")
    if not parse_policy_config(policy).valid:
        raise ValueError("judge policy is invalid")
    return policy


def _primary_skill(route_result: dict[str, Any]) -> str | None:
    recommendations = route_result.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return None
    projection = recommendations[0].get("legacy_skill_projection")
    if not isinstance(projection, dict):
        return None
    primary = projection.get("primary")
    return primary if isinstance(primary, str) else None


def _availability(recommendation: dict[str, Any]) -> str:
    recommendations = recommendation.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return "not-applicable"
    skills = recommendations[0].get("skills")
    if not isinstance(skills, list) or not skills:
        return "unknown"
    availability = skills[0].get("availability")
    if not isinstance(availability, dict):
        return "unknown"
    status = availability.get("status")
    return status if isinstance(status, str) else "unknown"


def _matches_expectation(case_id: str, route_result: dict[str, Any]) -> bool:
    expected = _DEFAULT_EXPECTATIONS[case_id]
    activation = route_result.get("activation")
    if not isinstance(activation, dict):
        return False
    return (
        route_result.get("status"),
        activation.get("disposition"),
        activation.get("reasonCode"),
        _primary_skill(route_result),
    ) == expected


def _build_case(
    case_id: str,
    prompt: str,
    policy: dict[str, Any],
    *,
    check_expectation: bool,
) -> dict[str, Any]:
    route_result = route_result_v2(prompt, policy)
    recommendation = structured_recommendation_v1(prompt, policy)
    expectation_status = (
        ("passed" if _matches_expectation(case_id, route_result) else "failed")
        if check_expectation
        else "not-applicable"
    )
    return {
        "id": case_id,
        "label": _CASE_LABELS[case_id],
        "input": {
            "promptStored": False,
            "characterCount": len(prompt),
        },
        "routeResult": route_result,
        "authority": recommendation["semantics"],
        "availability": _availability(recommendation),
        "expectation": {"status": expectation_status},
    }


def _fixture_metadata(status: str, *, unit_tests: int = 0) -> dict[str, Any]:
    passed = status == "passed"
    return {
        "fixture": "ci-relay-demo",
        "status": status,
        "checks": {
            "unitTests": unit_tests,
            "compile": "passed" if passed else status,
            "sampleEvent": "passed" if passed else status,
        },
        "sideEffects": {
            "hostInstall": False,
            "network": False,
            "codexHomeWrite": False,
            "repositoryWrite": False,
            "persistentExternalWrite": False,
            "temporaryWorkspace": status != "skipped",
        },
        "semantics": {
            "testsProveBaselineBehaviorOnly": True,
            "securityApproval": False,
        },
    }


def _run_fixture_verification() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="lazy-skill-router-judge-") as temporary:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPYCACHEPREFIX": str(Path(temporary) / "pycache"),
            "TMPDIR": temporary,
        }
        try:
            result = subprocess.run(
                [str(VERIFY_SCRIPT)],
                cwd=REPO_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _fixture_metadata("failed")

    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\bRan\s+(\d+)\s+tests?\b", output)
    unit_tests = int(match.group(1)) if match else 0
    verified = result.returncode == 0 and unit_tests == 6 and "CI Relay demo verification passed." in result.stdout
    return (
        _fixture_metadata("passed", unit_tests=6)
        if verified
        else _fixture_metadata(
            "failed",
            unit_tests=unit_tests,
        )
    )


def build_report(
    prompt: str | None = None,
    *,
    verify_fixture: bool = True,
) -> dict[str, Any]:
    policy = _load_policy()
    if prompt is None:
        cases = [
            _build_case(case_id, case_prompt, policy, check_expectation=True)
            for case_id, case_prompt in DEFAULT_PROMPTS.items()
        ]
    else:
        cases = [_build_case("custom", prompt, policy, check_expectation=False)]

    fixture = _run_fixture_verification() if verify_fixture else _fixture_metadata("skipped")
    return {
        "schema": "lazy-skill-router.judge-playground/v1",
        "mode": "local-source-only",
        "policyVersion": policy_version(policy),
        "cases": cases,
        "fixtureVerification": fixture,
    }


def report_exit_code(report: dict[str, Any]) -> int:
    cases = report.get("cases", [])
    if any(case.get("expectation", {}).get("status") == "failed" for case in cases):
        return 1
    fixture_status = report.get("fixtureVerification", {}).get("status")
    return 1 if fixture_status == "failed" else 0


def _render_text(report: dict[str, Any]) -> str:
    lines = ["Lazy Skill Router — Judge Playground", ""]
    for index, case in enumerate(report["cases"], start=1):
        route_result = case["routeResult"]
        activation = route_result["activation"]
        recommendations = route_result["recommendations"]
        evidence = recommendations[0]["matched_pattern_ids"] if recommendations else []
        lines.extend(
            [
                f"[{index}] {case['label']}",
                f"Input: not stored ({case['input']['characterCount']} chars)",
                (f"Router disposition: {activation['disposition'].upper()} ({activation['reasonCode']})"),
                f"Recommended skill: {_primary_skill(route_result) or 'none'}",
                f"Availability: {case['availability']}",
                f"Evidence: {', '.join(evidence) if evidence else 'none'}",
                f"Execution authority: {case['authority']['authority'].upper()}",
                "",
            ]
        )

    fixture = report["fixtureVerification"]
    if fixture["status"] == "passed":
        lines.append("CI Relay: PASS — 6 tests, compile, sample event")
    elif fixture["status"] == "skipped":
        lines.append("CI Relay: SKIPPED")
    else:
        lines.append("CI Relay: FAIL")
    lines.append("Note: baseline verification only; not security approval.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_prompt", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit the redacted JSON report")
    parser.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="read one custom prompt from stdin instead of using the three built-in cases",
    )
    parser.add_argument(
        "--skip-fixture-verification",
        action="store_true",
        help="skip the disposable CI Relay fixture checks",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.legacy_prompt:
        print("Custom prompts must be supplied with --prompt-stdin.", file=sys.stderr)
        return 2
    prompt = None
    if args.prompt_stdin:
        prompt = sys.stdin.read(MAX_ROUTABLE_PROMPT_CHARS + 1).rstrip("\r\n")
        if not prompt.strip():
            print("No custom prompt was received on stdin.", file=sys.stderr)
            return 2
    try:
        report = build_report(
            prompt=prompt,
            verify_fixture=not args.skip_fixture_verification,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "lazy-skill-router.judge-playground/v1",
                        "status": "structural-error",
                    },
                    sort_keys=True,
                )
            )
        else:
            print("Judge Playground could not load its local contracts.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
