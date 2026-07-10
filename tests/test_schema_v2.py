from __future__ import annotations

import copy
import unittest

from lazy_skill_router_contracts import route_result_v2, structured_recommendation_v1
from lazy_skill_router_core import dry_run_output
from validate_routes import validate_config


def schema_v2_config() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "policyVersion": "2026-07-10.1",
        "selection": {
            "mode": "ranked",
            "maxRecommendations": 3,
            "minMatchStrength": 0.55,
            "minScoreMargin": 0.05,
        },
        "skillBindings": {
            "pdf-work": "pdf",
            "writing": "writing-polish",
            "change-verification": "verification-gate",
            "general-assistance": "personal-skill-router",
        },
        "fallbackRouteId": "general",
        "routes": [
            {
                "id": "pdf",
                "intent": "work_with_pdf",
                "capabilityRequirements": {
                    "primary": ["pdf-work"],
                    "supporting": ["writing"],
                    "verification": ["change-verification"],
                },
                "reason": "PDF work detected.",
                "match": {
                    "any": [{"id": "pdf.token", "regex": "pdf", "label": "PDF token", "weight": 2}],
                    "none": ["explain-only-never"],
                },
            },
            {
                "id": "general",
                "intent": "general_assistance",
                "capabilityRequirements": {"primary": ["general-assistance"]},
            },
        ],
    }


class SchemaV2Test(unittest.TestCase):
    def test_v2_routes_and_preserves_intent_capability_contract(self) -> None:
        config = schema_v2_config()

        legacy = dry_run_output("PDF 만들어줘", config)
        structured = structured_recommendation_v1("PDF 만들어줘", config)

        self.assertTrue(legacy["shouldInject"])
        self.assertEqual(legacy["route"], "pdf")
        self.assertEqual(legacy["primary"], "pdf")
        recommendation = structured["recommendations"][0]
        self.assertEqual(recommendation["intent"], "work_with_pdf")
        self.assertEqual(
            recommendation["unresolved_capabilities"],
            ["pdf-work", "writing", "change-verification"],
        )
        self.assertEqual(recommendation["match"]["evidence_ids"], ["pdf.token"])

    def test_v2_fallback_is_post_selection_without_match_patterns(self) -> None:
        result = route_result_v2("hello", schema_v2_config())

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["recommendations"][0]["route_id"], "general")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_reason"], "no_matching_normal_route")

    def test_v2_ranking_is_stable_across_route_order(self) -> None:
        config = schema_v2_config()
        competing = copy.deepcopy(config["routes"][0])
        competing["id"] = "alternate-pdf"
        competing["intent"] = "alternate_pdf"
        config["routes"].insert(0, competing)

        forward = route_result_v2("pdf", config)
        reverse = route_result_v2("pdf", {**config, "routes": list(reversed(config["routes"]))})

        self.assertEqual(forward, reverse)
        self.assertEqual([item["route_id"] for item in forward["recommendations"][:2]], ["alternate-pdf", "pdf"])
        self.assertTrue(forward["ambiguous"])

    def test_unknown_schema_major_fails_open(self) -> None:
        config = schema_v2_config()
        config["schemaVersion"] = 3

        result = dry_run_output("pdf", config)

        self.assertFalse(result["shouldInject"])
        self.assertEqual(result["candidates"], [])

    def test_v2_route_with_missing_primary_binding_is_skipped(self) -> None:
        config = schema_v2_config()
        del config["skillBindings"]["pdf-work"]
        config["fallbackRouteId"] = None
        config["routes"] = [config["routes"][0]]

        result = dry_run_output("pdf", config)

        self.assertFalse(result["shouldInject"])

    def test_validator_accepts_v2_and_rejects_contract_breaks(self) -> None:
        valid = validate_config(schema_v2_config())
        self.assertEqual([finding.message for finding in valid if finding.severity == "ERROR"], [])

        broken = schema_v2_config()
        broken["routes"][0]["match"]["any"].append({"id": "pdf.token", "regex": "[", "weight": "heavy"})
        del broken["skillBindings"]["pdf-work"]
        errors = [finding.message for finding in validate_config(broken) if finding.severity == "ERROR"]

        self.assertTrue(any("duplicate pattern id" in message for message in errors))
        self.assertTrue(any("invalid regex" in message for message in errors))
        self.assertTrue(any("weight must be a positive number" in message for message in errors))
        self.assertTrue(any("missing skill binding" in message for message in errors))


if __name__ == "__main__":
    unittest.main()
