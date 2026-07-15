from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from lazy_skill_router_contracts import hook_ir_v1, route_result_v2, structured_recommendation_v1
from lazy_skill_router_core import dry_run_output

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"
DEFAULT_CONFIG = ROOT / "routes.default.json"
ROUTED_PROMPT = "GitHub PR에서 CI 실패 고쳐줘"


def load_default_config() -> dict[str, Any]:
    return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def run_json(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(args, check=False, capture_output=True, text=True, cwd=ROOT)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class RouteResultV2Test(unittest.TestCase):
    def test_meta_abstention_suppresses_skill_lists_across_structured_contracts(self) -> None:
        prompt = "스킬을 왜 사용하게 되는지 설명해줘"
        config = load_default_config()

        route_result = route_result_v2(prompt, config)
        recommendation = structured_recommendation_v1(prompt, config)
        hook_ir = hook_ir_v1(prompt, config)
        encoded = json.dumps(
            {"routeResult": route_result, "recommendation": recommendation, "hookIr": hook_ir},
            ensure_ascii=False,
        )

        self.assertEqual(route_result["status"], "abstained")
        self.assertEqual(route_result["activation"]["reasonCode"], "meta_context")
        self.assertEqual(route_result["recommendations"], [])
        self.assertEqual(recommendation["recommendations"], [])
        self.assertEqual(hook_ir["routes"], [])
        self.assertNotIn("personal-skill-router", encoded)
        self.assertNotIn("superpowers", encoded)
        self.assertNotIn("verification-gate", encoded)

    def test_activation_ir_is_identical_across_source_and_packaged_cli(self) -> None:
        source = run_json(
            [
                sys.executable,
                str(HOOK_PATH),
                "--config",
                str(DEFAULT_CONFIG),
                "--activation-ir-json",
                "PDF 만들어줘",
            ]
        )
        packaged = run_json(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--activation-ir-json",
                "--config",
                str(DEFAULT_CONFIG),
                "PDF 만들어줘",
            ]
        )

        self.assertEqual(source, packaged)
        self.assertEqual(source["disposition"], "propose")
        self.assertEqual(source["reasonCode"], "weak_evidence")

    def test_v2_preserves_v1_top1_and_uses_non_probability_strength(self) -> None:
        config = load_default_config()
        legacy = dry_run_output(ROUTED_PROMPT, config)

        result = route_result_v2(ROUTED_PROMPT, config)

        self.assertEqual(result["contract_version"], 2)
        self.assertEqual(result["match_strength_semantics"], "not_probability")
        self.assertEqual(result["recommendations"][0]["route_id"], legacy["route"])
        self.assertEqual(result["recommendations"][0]["match_strength"], legacy["confidence"])
        self.assertLessEqual(len(result["recommendations"]), 3)
        self.assertEqual(result["activation"]["disposition"], "activate")

    def test_v2_evidence_ids_are_stable_and_do_not_expose_prompt_or_regex(self) -> None:
        config = load_default_config()
        first = route_result_v2(ROUTED_PROMPT, config)
        second = route_result_v2(ROUTED_PROMPT, copy.deepcopy(config))

        self.assertEqual(first, second)
        encoded = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(ROUTED_PROMPT, encoded)
        self.assertNotIn("(?=.*", encoded)
        evidence_ids = first["recommendations"][0]["matched_pattern_ids"]
        self.assertTrue(evidence_ids)
        self.assertTrue(all(pattern_id.startswith("github-ci.") for pattern_id in evidence_ids))

    def test_v2_tie_is_deterministic_and_explicitly_ambiguous(self) -> None:
        routes = [
            {
                "id": "z-route",
                "intent": "z-route",
                "capabilityRequirements": {"primary": ["z-work"]},
                "match": {"any": [{"id": "z.signal", "regex": "python"}]},
                "lifecycle": {"state": "active"},
            },
            {
                "id": "a-route",
                "intent": "a-route",
                "capabilityRequirements": {"primary": ["a-work"]},
                "match": {"any": [{"id": "a.signal", "regex": "python"}]},
                "lifecycle": {"state": "active"},
            },
        ]
        config = {
            "schemaVersion": 2,
            "policyVersion": "test",
            "selection": {
                "mode": "ranked",
                "maxRecommendations": 3,
                "minMatchStrength": 0.55,
                "minScoreMargin": 0.05,
            },
            "skillBindings": {"z-work": "z-skill", "a-work": "a-skill"},
            "routes": routes,
        }

        forward = route_result_v2("python", config)
        reverse = route_result_v2("python", {**config, "routes": list(reversed(routes))})

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["status"], "ambiguous")
        self.assertTrue(forward["ambiguous"])
        self.assertEqual([item["route_id"] for item in forward["recommendations"]], ["a-route", "z-route"])
        self.assertEqual(forward["recommendations"][0]["score_margin"], 0.0)

    def test_v2_applies_fallback_only_when_no_normal_route_matches(self) -> None:
        config = {
            "routes": [
                {"name": "fallback", "primary": "fallback-skill", "patterns": ["python"], "fallback": True},
                {"name": "normal", "primary": "normal-skill", "patterns": ["python"]},
            ]
        }

        normal = route_result_v2("python", config)
        fallback = route_result_v2("python", {"routes": [config["routes"][0]]})

        self.assertEqual([item["route_id"] for item in normal["recommendations"]], ["normal"])
        self.assertFalse(normal["fallback_used"])
        self.assertEqual([item["route_id"] for item in fallback["recommendations"]], ["fallback"])
        self.assertTrue(fallback["fallback_used"])
        self.assertEqual(fallback["fallback_reason"], "no_matching_normal_route")

    def test_v2_no_match_is_structured_fail_open(self) -> None:
        result = route_result_v2("hello", {"routes": []})

        self.assertEqual(result["status"], "no-match")
        self.assertEqual(result["recommendations"], [])
        self.assertFalse(result["fallback_used"])
        self.assertFalse(result["ambiguous"])

    def test_source_and_packaged_cli_expose_identical_opt_in_v2_result(self) -> None:
        source = run_json(
            [
                sys.executable,
                str(HOOK_PATH),
                "--config",
                str(DEFAULT_CONFIG),
                "--route-result-v2",
                ROUTED_PROMPT,
            ]
        )
        packaged = run_json(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--route-result-v2",
                "--config",
                str(DEFAULT_CONFIG),
                ROUTED_PROMPT,
            ]
        )

        self.assertEqual(source, packaged)

    def test_versioned_contracts_exclude_disabled_and_shadow_routes(self) -> None:
        config = {
            "routes": [
                {
                    "name": "disabled",
                    "primary": "pdf",
                    "patterns": ["pdf"],
                    "lifecycle": {"state": "disabled"},
                },
                {
                    "name": "shadow",
                    "primary": "pdf",
                    "patterns": ["pdf"],
                    "lifecycle": {"state": "shadow"},
                },
            ]
        }

        route_result = route_result_v2("pdf", config)
        recommendation = structured_recommendation_v1("pdf", config)
        hook_ir = hook_ir_v1("pdf", config)

        self.assertEqual(route_result["status"], "no-match")
        self.assertEqual(route_result["recommendations"], [])
        self.assertEqual(recommendation["recommendations"], [])
        self.assertEqual(hook_ir["routes"], [])


class StructuredRecommendationV1Test(unittest.TestCase):
    def test_contract_is_self_describing_and_has_no_authority(self) -> None:
        result = structured_recommendation_v1(ROUTED_PROMPT, load_default_config())

        self.assertEqual(result["contract"], {"name": "lazy-skill-router.skill-recommendation", "version": "1.0"})
        self.assertEqual(result["producer"]["id"], "lazy-skill-router")
        self.assertEqual(
            result["semantics"],
            {
                "mode": "recommendation_only",
                "authority": "none",
                "agent_may_override": True,
                "must_reinspect_user_request": True,
                "must_not_override_higher_priority_instructions": True,
                "availability_is_authorization": False,
                "config_trust_is_authorization": False,
                "must_reauthorize_side_effects": True,
                "execution_requested": False,
            },
        )

    def test_contract_uses_bounded_ranked_routes_and_unknown_inventory_state(self) -> None:
        result = structured_recommendation_v1(ROUTED_PROMPT, load_default_config())

        self.assertEqual(result["route_result_ref"]["contract_version"], 2)
        self.assertLessEqual(len(result["recommendations"]), 3)
        self.assertEqual(result["recommendations"][0]["route_id"], "github-ci")
        skills = result["recommendations"][0]["skills"]
        self.assertTrue(skills)
        self.assertTrue(all(skill["availability"]["status"] == "unknown" for skill in skills))
        self.assertTrue(all(skill["availability"]["authorization"] is False for skill in skills))

    def test_configured_skill_names_get_deterministic_path_free_refs(self) -> None:
        result = structured_recommendation_v1("PDF 만들어줘", load_default_config())
        primary = result["recommendations"][0]["skills"][0]["skill_ref"]

        self.assertEqual(primary["configured_name"], "pdf")
        self.assertEqual(primary["canonical_id"], "configured/local/default/pdf")
        self.assertNotIn("/Users/", json.dumps(result))

        plugin_result = structured_recommendation_v1("GitHub PR에서 CI 실패 고쳐줘", load_default_config())
        plugin_ref = plugin_result["recommendations"][0]["skills"][0]["skill_ref"]
        self.assertEqual(plugin_ref["canonical_id"], "plugin/github/github/gh-fix-ci")

    def test_contract_does_not_include_raw_prompt_or_regex(self) -> None:
        result = structured_recommendation_v1(ROUTED_PROMPT, load_default_config())
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertNotIn(ROUTED_PROMPT, encoded)
        self.assertNotIn("(?=.*", encoded)
        self.assertIn("signal:github-ci.", encoded)

    def test_no_match_contract_is_empty_and_fail_open(self) -> None:
        result = structured_recommendation_v1("hello", {"routes": []})

        self.assertEqual(result["route_result_ref"]["status"], "no-match")
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["producer"]["inventory_state"], "missing")

    def test_source_and_packaged_cli_expose_identical_structured_contract(self) -> None:
        source = run_json(
            [
                sys.executable,
                str(HOOK_PATH),
                "--config",
                str(DEFAULT_CONFIG),
                "--recommendation-json",
                ROUTED_PROMPT,
            ]
        )
        packaged = run_json(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--recommendation-json",
                "--config",
                str(DEFAULT_CONFIG),
                ROUTED_PROMPT,
            ]
        )

        self.assertEqual(source, packaged)


class HookIrV1Test(unittest.TestCase):
    def test_hook_ir_is_compact_advisory_and_uses_safe_phases(self) -> None:
        result = hook_ir_v1("PDF 만들어줘", load_default_config())

        self.assertEqual(result["schema"], "lazy-skill-router.hook-ir/v1")
        self.assertEqual(result["decision"]["status"], "matched")
        self.assertTrue(result["routes"])
        phases = {route["role"]: route["phase"] for route in result["routes"] if route["rank"] == 1}
        self.assertEqual(phases["primary"], "inspect")
        self.assertEqual(phases["verification"], "verify")
        self.assertNotIn("mutate", phases.values())
        self.assertNotIn("publish", phases.values())
        self.assertTrue(all(route["risk_hint"] == "advisory" for route in result["routes"]))

    def test_answer_only_hook_ir_uses_explain_phase(self) -> None:
        result = hook_ir_v1("PDF 만드는 법 설명만 해줘", load_default_config())

        self.assertTrue(result["routes"])
        self.assertTrue(all(route["phase"] == "explain" for route in result["routes"] if route["rank"] == 1))

    def test_activation_no_action_pattern_drives_hook_ir_explain_phase(self) -> None:
        prompt = "explain how to fix pdf"
        config = {
            "answerOnlyPatterns": ["legacy-answer-only-never-match"],
            "activation": {
                "mode": "inject",
                "metaPatterns": ["router meta only"],
                "actionPatterns": [r"\bfix\b"],
                "noActionPatterns": [r"\bexplain how to fix\b"],
            },
            "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
        }

        result = hook_ir_v1(prompt, config)
        structured = structured_recommendation_v1(prompt, config)

        self.assertEqual(structured["route_result_ref"]["activation_reason_code"], "answer_only")
        self.assertEqual(structured["route_result_ref"]["activation_request_mode"], "answer-only")
        self.assertTrue(result["routes"])
        self.assertTrue(all(route["phase"] == "explain" for route in result["routes"] if route["rank"] == 1))

    def test_hook_ir_no_match_is_fail_open_and_contains_no_prompt(self) -> None:
        prompt = "private-no-route-value"
        result = hook_ir_v1(prompt, {"routes": []})

        self.assertEqual(result["decision"]["status"], "no-match")
        self.assertEqual(result["routes"], [])
        self.assertNotIn(prompt, json.dumps(result))

    def test_source_and_packaged_cli_expose_identical_hook_ir(self) -> None:
        source = run_json(
            [
                sys.executable,
                str(HOOK_PATH),
                "--config",
                str(DEFAULT_CONFIG),
                "--hook-ir-json",
                ROUTED_PROMPT,
            ]
        )
        packaged = run_json(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "route",
                "--hook-ir-json",
                "--config",
                str(DEFAULT_CONFIG),
                ROUTED_PROMPT,
            ]
        )

        self.assertEqual(source, packaged)


if __name__ == "__main__":
    unittest.main()
