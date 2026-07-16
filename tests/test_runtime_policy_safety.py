from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import lazy_skill_router_core as core
import lazy_skill_router_policy_ir as policy_ir
import lazy_skill_router_scoring as scoring
from lazy_skill_router_activation import DEFAULT_META_PATTERNS, activation_ir_dict, activation_policy
from lazy_skill_router_contracts import hook_ir_v1, route_result_v2, structured_recommendation_v1
from lazy_skill_router_policy_ir import parse_policy_config, runtime_routes
from lazy_skill_router_scoring import matched_patterns

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "lazy_skill_router.py"
DEFAULT_CONFIG_PATH = ROOT / "routes.default.json"


def v1_route(
    *,
    name: str = "route",
    patterns: list[object] | None = None,
    exclude_patterns: list[object] | None = None,
    **extra: object,
) -> dict[str, object]:
    route: dict[str, object] = {
        "name": name,
        "primary": f"{name}-skill",
        "patterns": patterns if patterns is not None else ["route"],
    }
    if exclude_patterns is not None:
        route["excludePatterns"] = exclude_patterns
    route.update(extra)
    return route


def v2_config(regex: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "policyVersion": "test",
        "selection": {
            "mode": "ranked",
            "maxRecommendations": 1,
            "minMatchStrength": 0.55,
            "minScoreMargin": 0.05,
        },
        "skillBindings": {"work": "work-skill"},
        "routes": [
            {
                "id": "work",
                "intent": "work",
                "capabilityRequirements": {"primary": ["work"]},
                "match": {"any": [{"id": "work.signal", "regex": regex}]},
                "lifecycle": {"state": "active"},
            }
        ],
    }


class PromptBoundaryTest(unittest.TestCase):
    def test_overlong_prompt_abstains_before_policy_or_regex_work(self) -> None:
        prompt = "x" * 4097
        config = {"routes": [v1_route(patterns=["x"])]}

        with mock.patch.object(core, "runtime_policy") as runtime_policy:
            dry_run = core.dry_run_output(prompt, config)

        runtime_policy.assert_not_called()
        self.assertFalse(dry_run["shouldInject"])
        self.assertFalse(dry_run["shouldActivate"])
        self.assertEqual(dry_run["activationDecision"], "abstain")
        self.assertEqual(dry_run["activationReason"], "prompt_too_long")
        self.assertEqual(dry_run["activation"]["reasonCode"], "prompt_too_long")

    def test_overlong_prompt_has_the_same_abstention_across_contracts(self) -> None:
        prompt = "x" * 4097
        config = {"routes": [v1_route(patterns=["x"])]}

        activation = core.activation_for_prompt(prompt, config)
        route_result = route_result_v2(prompt, config)
        recommendation = structured_recommendation_v1(prompt, config)
        hook_ir = hook_ir_v1(prompt, config)

        self.assertEqual(activation_ir_dict(activation)["reasonCode"], "prompt_too_long")
        self.assertEqual(route_result["status"], "abstained")
        self.assertEqual(route_result["activation"]["reasonCode"], "prompt_too_long")
        self.assertEqual(route_result["recommendations"], [])
        self.assertEqual(recommendation["recommendations"], [])
        self.assertEqual(recommendation["route_result_ref"]["activation_reason_code"], "prompt_too_long")
        self.assertEqual(hook_ir["routes"], [])
        self.assertEqual(hook_ir["decision"]["activation_reason_code"], "prompt_too_long")

    def test_dry_run_never_writes_the_measurement_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "measurements.jsonl"
            config = {
                "logging": {"enabled": True, "path": str(journal)},
                "routes": [v1_route(patterns=["route"])],
            }

            core.dry_run_output("route", config)

            self.assertFalse(journal.exists())

    def test_overlong_diagnostic_cli_surfaces_never_write_the_journal(self) -> None:
        prompt = "x" * 4097
        flags = (
            "--dry-run",
            "--route-result-v2",
            "--recommendation-json",
            "--hook-ir-json",
            "--activation-ir-json",
            "--capability-shadow-json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            journal = root / "measurements.jsonl"
            config_path = root / "routes.json"
            config_path.write_text(
                json.dumps(
                    {
                        "logging": {"enabled": True, "path": str(journal)},
                        "routes": [v1_route(patterns=["x"])],
                    }
                ),
                encoding="utf-8",
            )

            for flag in flags:
                with self.subTest(flag=flag):
                    completed = subprocess.run(
                        [sys.executable, str(HOOK_PATH), "--config", str(config_path), flag, "--prompt", prompt],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertFalse(journal.exists())

    def test_actual_hook_logs_only_an_input_rejected_decision_for_overlong_prompt(self) -> None:
        prompt = "private-" + ("x" * 4097)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            journal = root / "measurements.jsonl"
            config_path = root / "routes.json"
            config_path.write_text(
                json.dumps(
                    {
                        "logging": {"enabled": True, "path": str(journal)},
                        "routes": [v1_route(patterns=["x"])],
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(HOOK_PATH), "--config", str(config_path)],
                input=json.dumps({"prompt": prompt, "session_id": "session", "turn_id": "turn"}),
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["decisionStatus"], "input-rejected")
            self.assertIsNone(events[0]["activationDisposition"])
            self.assertIsNone(events[0]["activationReason"])
            self.assertIsNone(events[0]["route"])
            self.assertNotIn(prompt, json.dumps(events[0]))

    def test_default_policy_adversarial_prompt_latency_stays_bounded(self) -> None:
        config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        latencies_ms: list[float] = []

        for index in range(100):
            prompt = (("x" * 4090) + f"{index:06d}")[:4096]
            started = time.perf_counter()
            core.route_matches(prompt, config)
            latencies_ms.append((time.perf_counter() - started) * 1000)

        ordered = sorted(latencies_ms)
        self.assertLessEqual(ordered[94], 50.0)
        self.assertLessEqual(ordered[-1], 250.0)


class SchemaAwareRankingTest(unittest.TestCase):
    def test_v1_contract_tie_uses_config_order_like_runtime(self) -> None:
        config = {
            "routes": [
                v1_route(name="z-route", patterns=["python"]),
                v1_route(name="a-route", patterns=["python"]),
            ]
        }

        dry_run = core.dry_run_output("python", config)
        route_result = route_result_v2("python", config)
        recommendation = structured_recommendation_v1("python", config)
        hook_ir = hook_ir_v1("python", config)
        activation = core.activation_for_prompt("python", config)

        self.assertEqual(dry_run["route"], "z-route")
        self.assertEqual(route_result["recommendations"][0]["route_id"], "z-route")
        self.assertEqual(recommendation["recommendations"][0]["route_id"], "z-route")
        self.assertEqual(hook_ir["routes"][0]["route_id"], "z-route")
        self.assertEqual(activation.route_id, "z-route")

    def test_v1_shadow_equal_score_would_win_only_when_config_order_is_first(self) -> None:
        shadow = v1_route(name="shadow", patterns=["python"], lifecycle={"state": "shadow"})
        active = v1_route(name="active", patterns=["python"], lifecycle={"state": "active"})

        shadow_first = core.dry_run_output("python", {"routes": [shadow, active]})
        active_first = core.dry_run_output("python", {"routes": [active, shadow]})

        self.assertEqual(shadow_first["shadowPromotionWinners"], ["shadow"])
        self.assertEqual(active_first["shadowPromotionWinners"], [])


class PolicyScalarValidationTest(unittest.TestCase):
    def test_lifecycle_requires_state_and_rejects_unknown_fields(self) -> None:
        cases = (
            {"lifecycle": {}},
            {"lifecycle": {"stat": "shadow"}},
            {"lifecycle": {"state": "active", "unexpected": True}},
            {"lifecycle": {"state": "active", "previousState": []}},
        )

        for lifecycle in cases:
            with self.subTest(lifecycle=lifecycle):
                parsed = parse_policy_config({"routes": [v1_route(**lifecycle)]})

                self.assertFalse(parsed.valid)
                self.assertEqual(runtime_routes(parsed.policy)[0].lifecycle_state, "disabled")

    def test_non_finite_and_out_of_range_numbers_are_rejected(self) -> None:
        cases = (
            ({"minConfidence": math.inf, "routes": [v1_route()]}, "min_confidence_invalid"),
            ({"minConfidence": math.nan, "routes": [v1_route()]}, "min_confidence_invalid"),
            ({"routes": [v1_route(priority=21)]}, "route_priority_invalid"),
            ({"routes": [v1_route(priority=-21)]}, "route_priority_invalid"),
            ({"routes": [v1_route(weight=1.01)]}, "route_weight_invalid"),
            ({"routes": [v1_route(weight=-1.01)]}, "route_weight_invalid"),
            (
                {"routes": [v1_route(patterns=[{"regex": "route", "weight": 3.01}])]},
                "route_pattern_weight_invalid",
            ),
            (
                {"routes": [v1_route(patterns=[{"regex": "route", "weight": math.inf}])]},
                "route_pattern_weight_invalid",
            ),
        )

        for config, expected_code in cases:
            with self.subTest(expected_code=expected_code, config=config):
                parsed = parse_policy_config(config)

                self.assertFalse(parsed.valid)
                self.assertIn(expected_code, {finding.code for finding in parsed.findings})

    def test_json_loader_rejects_non_standard_and_overflowed_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, value in (("nan", "NaN"), ("infinity", "Infinity"), ("overflow", "1e309")):
                with self.subTest(name=name):
                    path = root / f"{name}.json"
                    path.write_text(f'{{"minConfidence": {value}, "routes": []}}', encoding="utf-8")

                    self.assertIsNone(core.load_json(path))

    def test_unhashable_retrieval_mode_is_a_finding_not_an_exception(self) -> None:
        parsed = parse_policy_config(
            {
                "capabilityRetrieval": {"mode": [], "maxCandidates": 3},
                "routes": [v1_route()],
            }
        )

        self.assertIn("capability_retrieval_mode_invalid", {finding.code for finding in parsed.findings})

    def test_only_v1_routes_may_omit_lifecycle(self) -> None:
        v1 = parse_policy_config({"routes": [v1_route()]})
        v2_config_without_lifecycle = v2_config("work")
        v2_config_without_lifecycle["routes"][0].pop("lifecycle")
        v2 = parse_policy_config(v2_config_without_lifecycle)

        self.assertTrue(v1.valid, v1.findings)
        self.assertEqual(v1.policy.routes[0].lifecycle_state, "active")
        self.assertFalse(v2.valid)
        self.assertIn("route_lifecycle_missing", {finding.code for finding in v2.findings})
        self.assertEqual(v2.policy.routes[0].lifecycle_state, "disabled")

    def test_v1_explicit_null_lifecycle_is_invalid_not_legacy_active(self) -> None:
        parsed = parse_policy_config({"routes": [v1_route(lifecycle=None)]})

        self.assertFalse(parsed.valid)
        self.assertIn("route_lifecycle_invalid", {finding.code for finding in parsed.findings})
        self.assertEqual(parsed.policy.routes[0].lifecycle_state, "disabled")

    def test_v2_explicit_null_lifecycle_is_invalid_not_missing(self) -> None:
        config = v2_config("work")
        config["routes"][0]["lifecycle"] = None

        parsed = parse_policy_config(config)

        self.assertFalse(parsed.valid)
        codes = {finding.code for finding in parsed.findings}
        self.assertIn("route_lifecycle_invalid", codes)
        self.assertNotIn("route_lifecycle_missing", codes)
        self.assertEqual(parsed.policy.routes[0].lifecycle_state, "disabled")

    def test_custom_subset_of_shipped_activation_bundle_is_not_trusted(self) -> None:
        config = {
            "activation": {"mode": "inject", "metaPatterns": [DEFAULT_META_PATTERNS[0]]},
            "routes": [v1_route()],
        }

        parsed = parse_policy_config(config)
        runtime = activation_policy(config)

        self.assertFalse(parsed.valid)
        self.assertIn("activation_meta_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(runtime.meta_patterns, DEFAULT_META_PATTERNS)


class RouteRegexSafetyTest(unittest.TestCase):
    def test_runtime_default_answer_only_patterns_pass_shared_policy_safety(self) -> None:
        config = {
            "answerOnlyPatterns": list(core.DEFAULT_ANSWER_ONLY_PATTERNS),
            "routes": [v1_route(patterns=["safe-pattern"])],
        }

        parsed = parse_policy_config(config)

        self.assertTrue(parsed.valid, parsed.findings)

    def test_unsafe_route_regexes_are_rejected_before_runtime_search(self) -> None:
        unsafe_patterns = (
            r"(a+)+$",
            r"(a|aa)+$",
            r"a*a*b",
            r"(a)\1",
            r"(a)?(?(1)b|c)",
            r"a{257}",
            r"a{1,257}",
            "a" * 301,
        )

        for pattern in unsafe_patterns:
            with self.subTest(pattern=pattern[:40]):
                parsed = parse_policy_config({"routes": [v1_route(patterns=[pattern])]})

                self.assertFalse(parsed.valid)
                self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

    def test_route_and_policy_pattern_budgets_are_enforced(self) -> None:
        route_overflow = parse_policy_config(
            {
                "routes": [
                    v1_route(
                        patterns=[f"include-{index}" for index in range(17)],
                        exclude_patterns=[f"exclude-{index}" for index in range(16)],
                    )
                ]
            }
        )
        policy_overflow = parse_policy_config(
            {
                "routes": [
                    v1_route(name=f"route-{index}", patterns=[f"token-{index}-{item}" for item in range(32)])
                    for index in range(17)
                ]
            }
        )

        self.assertIn("route_pattern_limit_exceeded", {finding.code for finding in route_overflow.findings})
        self.assertIn("policy_pattern_limit_exceeded", {finding.code for finding in policy_overflow.findings})

    def test_v1_leading_positive_lookahead_is_anchored_with_warning(self) -> None:
        regex = r"(?=.*python)(?=.*test)"

        parsed = parse_policy_config({"routes": [v1_route(patterns=[regex])]})

        self.assertTrue(parsed.valid, parsed.findings)
        self.assertEqual(parsed.policy.routes[0].patterns[0].regex, "^" + regex)
        self.assertIn("route_pattern_regex_anchored", {finding.code for finding in parsed.findings})

    def test_v1_unanchored_shipped_docs_exception_normalizes_to_the_exact_trusted_pattern(self) -> None:
        default_config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        docs = next(route for route in default_config["routes"] if route["name"] == "docs")
        trusted = docs["excludePatterns"][0]
        self.assertTrue(trusted.startswith("^(?="))
        legacy = trusted[1:]
        config = {
            "routes": [
                v1_route(
                    name="docs",
                    patterns=["docs"],
                    exclude_patterns=[legacy],
                )
            ]
        }

        parsed = parse_policy_config(config)

        self.assertTrue(parsed.valid, parsed.findings)
        self.assertEqual(parsed.policy.routes[0].exclude_patterns[0].regex, trusted)
        self.assertIn("route_pattern_regex_anchored", {finding.code for finding in parsed.findings})

    def test_shipped_route_exception_requires_exact_route_field_and_regex_tuple(self) -> None:
        default_config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        docs = next(route for route in default_config["routes"] if route["name"] == "docs")
        trusted = docs["excludePatterns"][0]

        wrong_route = parse_policy_config(
            {"routes": [v1_route(name="not-docs", patterns=["docs"], exclude_patterns=[trusted])]}
        )
        wrong_field = parse_policy_config({"routes": [v1_route(name="docs", patterns=[trusted])]})

        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in wrong_route.findings})
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in wrong_field.findings})

    def test_exact_route_exception_still_counts_toward_policy_work_budget(self) -> None:
        default_config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        github = next(route for route in default_config["routes"] if route["name"] == "github-ci")
        trusted = next(pattern for pattern in github["patterns"] if "action(s)?" in pattern["regex"])
        config = {"routes": [v1_route(name="github-ci", patterns=[trusted] * 12)]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("actions " * 512, config)

        self.assertFalse(parsed.valid)
        self.assertIn("policy_regex_work_limit_exceeded", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_v2_rejects_new_lookaround(self) -> None:
        parsed = parse_policy_config(v2_config(r"(?=.*python)(?=.*test)"))

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

    def test_v1_activation_leading_positive_lookahead_is_anchored_for_runtime(self) -> None:
        regex = r"(?=.*fix)"
        config = {
            "activation": {"mode": "inject", "actionPatterns": [regex]},
            "routes": [v1_route()],
        }

        parsed = parse_policy_config(config)
        runtime = activation_policy(config)

        self.assertTrue(parsed.valid, parsed.findings)
        self.assertIn("activation_action_pattern_regex_anchored", {finding.code for finding in parsed.findings})
        self.assertEqual(runtime.action_patterns, ("^" + regex,))

    def test_v2_activation_rejects_new_lookaround(self) -> None:
        config = v2_config("work")
        config["activation"] = {"mode": "inject", "actionPatterns": [r"(?=.*fix)"]}

        parsed = parse_policy_config(config)

        self.assertFalse(parsed.valid)
        self.assertIn("activation_action_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

    def test_regex_compile_resource_errors_are_findings_not_exceptions(self) -> None:
        with mock.patch.object(policy_ir.re, "compile", side_effect=RecursionError("too deep")):
            parsed = parse_policy_config({"routes": [v1_route(patterns=["route"])]})

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_invalid", {finding.code for finding in parsed.findings})

    def test_regex_search_resource_errors_fail_open(self) -> None:
        for error in (re.error("bad"), RecursionError("deep"), OverflowError("large")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(scoring.re, "search", side_effect=error):
                    self.assertEqual(matched_patterns("route", ("route",)), ())

    def test_unanchored_variable_repeat_before_suffix_has_a_64_character_limit(self) -> None:
        for regex in (r"A.*B", r"A[\s\S]*B", r"Aa*B", r"A.{0,65}B"):
            with self.subTest(regex=regex):
                parsed = parse_policy_config({"routes": [v1_route(patterns=[regex])]})

                self.assertFalse(parsed.valid)
                self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

        for regex in (r"A.{0,64}B", r"^A.*B", r"\AA.*B", r"A.*"):
            with self.subTest(regex=regex):
                parsed = parse_policy_config({"routes": [v1_route(patterns=[regex])]})

                self.assertTrue(parsed.valid, parsed.findings)

    def test_unanchored_repeat_suffix_limit_covers_nested_branch_and_assertion_forms(self) -> None:
        for regex in (r"A(?:.*B)", r"(A.*)B", r"(?:A.*B|C)", r"A.*$"):
            with self.subTest(regex=regex):
                parsed = parse_policy_config({"routes": [v1_route(patterns=[regex])]})

                self.assertFalse(parsed.valid)
                self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

    def test_repeated_unanchored_variable_repeat_is_rejected_before_runtime_search(self) -> None:
        patterns = [{"id": f"retry-{index}", "regex": r"A.*B"} for index in range(32)]
        config = {"routes": [v1_route(patterns=patterns)]}

        with mock.patch.object(scoring.re, "search") as search:
            parsed = parse_policy_config(config)
            matches = core.route_matches("A" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        search.assert_not_called()

    def test_serial_optional_repeats_are_rejected_before_runtime_search(self) -> None:
        regex = ("a?" * 20) + "b"
        config = {"routes": [v1_route(patterns=[regex])]}

        parsed = parse_policy_config(config)
        with mock.patch.object(scoring.re, "search") as search:
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        search.assert_not_called()

    def test_combined_bounded_repeat_budget_rejects_catastrophic_backtracking_before_runtime(self) -> None:
        regex = r"a{0,256}a{0,256}a{0,256}b"
        config = {"routes": [v1_route(patterns=[regex])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 768, config)

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_unanchored_retry_work_rejects_eight_small_bounded_repeats_before_runtime(self) -> None:
        regex = (r"a{0,2}" * 8) + "b"
        config = {"routes": [v1_route(patterns=[regex])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_policy_aggregate_regex_work_rejects_four_individually_bounded_patterns(self) -> None:
        prefix = r"a{0,2}" * 6
        config = {"routes": [v1_route(patterns=[prefix + suffix for suffix in "bcde"])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("policy_regex_work_limit_exceeded", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_unquantified_alternation_sums_repeat_work_across_branches(self) -> None:
        branches = [((character + r"{0,2}") * 6) + suffix for character, suffix in zip("ace", "bdf")]
        regex = "(?:" + "|".join(branches) + ")"
        config = {"routes": [v1_route(patterns=[regex])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_interleaved_optional_repeats_cannot_reset_static_work(self) -> None:
        for atom in ("a?a", "[a]?a"):
            with self.subTest(atom=atom):
                regex = (atom * 12) + "b"
                config = {"routes": [v1_route(patterns=[regex])]}

                with (
                    mock.patch.object(policy_ir.re, "compile") as compile_regex,
                    mock.patch.object(scoring.re, "search") as search,
                ):
                    parsed = parse_policy_config(config)
                    matches = core.route_matches("a" * 4096, config)

                self.assertFalse(parsed.valid)
                self.assertIn("route_pattern_regex_unsafe", {finding.code for finding in parsed.findings})
                self.assertEqual(matches, ())
                compile_regex.assert_not_called()
                search.assert_not_called()

    def test_policy_aggregate_counts_interleaved_optional_work_across_patterns(self) -> None:
        prefix = "a?a" * 8
        config = {"routes": [v1_route(patterns=[prefix + suffix for suffix in "bcde"])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("policy_regex_work_limit_exceeded", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()

    def test_policy_aggregate_charges_leading_unbounded_search_retries(self) -> None:
        config = {"routes": [v1_route(patterns=[rf"[a-z]*b{suffix}" for suffix in "cdef"])]}

        with (
            mock.patch.object(policy_ir.re, "compile") as compile_regex,
            mock.patch.object(scoring.re, "search") as search,
        ):
            parsed = parse_policy_config(config)
            matches = core.route_matches("a" * 4096, config)

        self.assertFalse(parsed.valid)
        self.assertIn("policy_regex_work_limit_exceeded", {finding.code for finding in parsed.findings})
        self.assertEqual(matches, ())
        compile_regex.assert_not_called()
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
