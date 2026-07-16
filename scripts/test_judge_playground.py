#!/usr/bin/env python3
"""Characterization tests for the source-only judge playground."""

import contextlib
import io
import json
import unittest
from unittest import mock

import judge_playground


class JudgePlaygroundTests(unittest.TestCase):
    def build_report(self, prompt=None):
        return judge_playground.build_report(prompt=prompt, verify_fixture=False)

    def test_default_cases_cover_propose_activate_and_abstain(self):
        cases = {case["id"]: case for case in self.build_report()["cases"]}
        expected = {
            "mindmap": ("propose", "answer_only", "project-mindmap"),
            "ponytail": ("activate", "eligible", "ponytail"),
            "abstain": ("abstain", "no_candidate", None),
        }

        self.assertEqual(set(cases), set(expected))
        for case_id, (disposition, reason, skill) in expected.items():
            case = cases[case_id]
            activation = case["routeResult"]["activation"]
            recommendations = case["routeResult"]["recommendations"]
            self.assertEqual(activation["disposition"], disposition)
            self.assertEqual(activation["reasonCode"], reason)
            self.assertEqual(case["authority"]["authority"], "none")
            self.assertFalse(case["authority"]["execution_requested"])
            self.assertTrue(case["authority"]["must_reauthorize_side_effects"])
            if skill is None:
                self.assertEqual(recommendations, [])
                self.assertNotIn("project-mindmap", json.dumps(case))
                self.assertNotIn("ponytail", json.dumps(case))
            else:
                self.assertEqual(
                    recommendations[0]["legacy_skill_projection"]["primary"],
                    skill,
                )

    def test_json_omits_prompt_paths_regex_and_latency(self):
        report = self.build_report()
        payload = json.dumps(report, sort_keys=True)

        for prompt in judge_playground.DEFAULT_PROMPTS.values():
            self.assertNotIn(prompt, payload)
        self.assertNotIn(str(judge_playground.REPO_ROOT), payload)
        self.assertNotIn('"prompt":', payload)
        self.assertNotIn('"rawPrompt"', payload)
        self.assertNotIn('"regex"', payload)
        self.assertNotIn('"latency"', payload)
        self.assertNotIn("/Users/", payload)

    def test_same_input_is_deterministic(self):
        prompt = "Make the smallest correct change and add no dependency."
        first = self.build_report(prompt)
        second = self.build_report(prompt)
        self.assertEqual(first, second)

    def test_custom_security_prompt_keeps_optional_route_coverage(self):
        report = self.build_report(
            "Scan this CI relay for a concrete exploitable security vulnerability. Do not modify files."
        )
        case = report["cases"][0]
        route_result = case["routeResult"]

        self.assertEqual(route_result["status"], "matched")
        self.assertEqual(route_result["activation"]["disposition"], "propose")
        self.assertEqual(route_result["activation"]["reasonCode"], "answer_only")
        self.assertEqual(
            route_result["recommendations"][0]["legacy_skill_projection"]["primary"],
            "codex-security:security-scan",
        )
        self.assertEqual(case["authority"]["authority"], "none")
        self.assertEqual(case["expectation"]["status"], "not-applicable")

    def test_oversized_prompt_abstains_before_routing(self):
        report = self.build_report("x" * 4097)
        route_result = report["cases"][0]["routeResult"]

        self.assertEqual(route_result["status"], "abstained")
        self.assertEqual(route_result["recommendations"], [])
        self.assertEqual(route_result["activation"]["disposition"], "abstain")
        self.assertEqual(
            route_result["activation"]["reasonCode"],
            "prompt_too_long",
        )

    def test_fixture_failure_is_not_reported_as_pass(self):
        failed = {
            "fixture": "ci-relay-demo",
            "status": "failed",
            "checks": {
                "unitTests": 0,
                "compile": "failed",
                "sampleEvent": "failed",
            },
            "sideEffects": {},
            "semantics": {},
        }
        with mock.patch.object(
            judge_playground,
            "_run_fixture_verification",
            return_value=failed,
        ):
            report = judge_playground.build_report(verify_fixture=True)

        self.assertEqual(report["fixtureVerification"]["status"], "failed")
        self.assertEqual(judge_playground.report_exit_code(report), 1)

    def test_skip_fixture_verification_does_not_invoke_verifier(self):
        with mock.patch.object(judge_playground, "_run_fixture_verification") as run:
            report = judge_playground.build_report(verify_fixture=False)

        run.assert_not_called()
        self.assertEqual(report["fixtureVerification"]["status"], "skipped")

    def test_custom_prompt_is_read_from_stdin_without_echoing_it(self):
        report = {
            "schema": "lazy-skill-router.judge-playground/v1",
            "mode": "local-source-only",
            "policyVersion": "test",
            "cases": [],
            "fixtureVerification": {"status": "skipped"},
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(judge_playground.sys, "stdin", io.StringIO("private custom prompt\n")),
            mock.patch.object(judge_playground, "build_report", return_value=report) as build,
            contextlib.redirect_stdout(stdout),
        ):
            status = judge_playground.main(["--prompt-stdin", "--skip-fixture-verification", "--json"])

        self.assertEqual(status, 0)
        build.assert_called_once_with(prompt="private custom prompt", verify_fixture=False)
        self.assertNotIn("private custom prompt", stdout.getvalue())

    def test_positional_custom_prompt_is_rejected_without_echoing_it(self):
        secret = "private positional prompt"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = judge_playground.main([secret])

        self.assertEqual(status, 2)
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
