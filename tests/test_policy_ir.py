from __future__ import annotations

import unittest

from lazy_skill_router_contracts import structured_recommendation_v1
from lazy_skill_router_core import route_prompt
from lazy_skill_router_inventory import InventorySnapshot
from lazy_skill_router_policy_ir import (
    parse_policy_config,
    policy_references,
    resolve_policy,
    runtime_routes,
    select_smoke_primary,
)


def available_skill(name: str, canonical_id: str) -> dict[str, object]:
    return {
        "configured_name": name,
        "canonical_id": canonical_id,
        "availability": {"status": "available"},
    }


class PolicyIRTest(unittest.TestCase):
    def test_legacy_base_identifiers_remain_compatible(self) -> None:
        v1 = {
            "routes": [
                {
                    "name": "PDF 문서",
                    "intent": "문서 만들기",
                    "primary": "pdf",
                    "patterns": ["pdf"],
                }
            ]
        }
        v2 = {
            "schemaVersion": 2,
            "policyVersion": "legacy",
            "selection": {
                "mode": "ranked",
                "maxRecommendations": 1,
                "minMatchStrength": 0.55,
                "minScoreMargin": 0.05,
            },
            "skillBindings": {"pdf-work": "pdf"},
            "routes": [
                {
                    "id": "PDF 문서",
                    "intent": "문서 만들기",
                    "capabilityRequirements": {"primary": ["pdf-work"]},
                    "match": {"any": [{"id": "pdf/create", "regex": "pdf"}]},
                }
            ],
        }

        parsed_v1 = parse_policy_config(v1)
        parsed_v2 = parse_policy_config(v2)

        self.assertTrue(parsed_v1.valid, parsed_v1.findings)
        self.assertTrue(parsed_v2.valid, parsed_v2.findings)
        self.assertEqual(parsed_v2.policy.routes[0].patterns[0].pattern_id, "pdf/create")
        self.assertIsNotNone(route_prompt("pdf", v1))
        self.assertIsNotNone(route_prompt("pdf", v2))

    def test_equivalent_v1_and_v2_configs_normalize_to_the_same_runtime_route(self) -> None:
        v1 = {
            "schemaVersion": 1,
            "allowedSkills": ["pdf", "verification-gate"],
            "routes": [
                {
                    "name": "pdf",
                    "intent": "work_with_pdf",
                    "primary": "pdf",
                    "verification": "verification-gate",
                    "patterns": [{"id": "pdf.token", "regex": "pdf", "label": "PDF token", "weight": 2}],
                }
            ],
        }
        v2 = {
            "schemaVersion": 2,
            "policyVersion": "test",
            "selection": {
                "mode": "ranked",
                "maxRecommendations": 1,
                "minMatchStrength": 0.55,
                "minScoreMargin": 0.05,
            },
            "skillBindings": {
                "pdf-work": {"skill": "pdf", "canonicalId": "host/codex/skills/pdf"},
                "verify": "verification-gate",
            },
            "routes": [
                {
                    "id": "pdf",
                    "intent": "work_with_pdf",
                    "capabilityRequirements": {"primary": ["pdf-work"], "verification": ["verify"]},
                    "match": {"any": [{"id": "pdf.token", "regex": "pdf", "label": "PDF token", "weight": 2}]},
                }
            ],
        }

        parsed_v1 = parse_policy_config(v1)
        parsed_v2 = parse_policy_config(v2)

        self.assertTrue(parsed_v1.valid)
        self.assertTrue(parsed_v2.valid)
        route_v1 = runtime_routes(parsed_v1.policy)[0]
        route_v2 = runtime_routes(parsed_v2.policy)[0]
        self.assertEqual(
            (route_v1.name, route_v1.intent, route_v1.primary, route_v1.verification),
            (route_v2.name, route_v2.intent, route_v2.primary, route_v2.verification),
        )
        self.assertEqual(route_v1.patterns, route_v2.patterns)

    def test_v2_references_and_smoke_primary_use_resolved_bindings(self) -> None:
        config = {
            "schemaVersion": 2,
            "skillBindings": {"pdf-work": {"skill": "pdf", "canonicalId": "host/codex/skills/pdf"}},
            "routes": [
                {
                    "id": "shadow",
                    "intent": "shadow_pdf",
                    "capabilityRequirements": {"primary": ["pdf-work"]},
                    "match": {"any": [{"id": "shadow.pdf", "regex": "pdf"}]},
                    "lifecycle": {"state": "shadow"},
                },
                {
                    "id": "active",
                    "intent": "active_pdf",
                    "capabilityRequirements": {"primary": ["pdf-work"]},
                    "match": {"any": [{"id": "active.pdf", "regex": "pdf"}]},
                },
            ],
        }
        parsed = parse_policy_config(config)

        self.assertEqual(select_smoke_primary(parsed.policy), "pdf")
        self.assertEqual(
            [(reference.route_id, reference.skill.configured_name) for reference in policy_references(parsed.policy)],
            [("active", "pdf")],
        )
        self.assertEqual(
            [
                (reference.route_id, reference.skill.configured_name)
                for reference in policy_references(parsed.policy, include_shadow=True)
            ],
            [("shadow", "pdf"), ("active", "pdf")],
        )

    def test_v1_reference_field_names_preserve_legacy_json_contract(self) -> None:
        parsed = parse_policy_config(
            {
                "routes": [
                    {
                        "name": "pdf",
                        "primary": "pdf",
                        "supporting": ["writing-polish", "documents"],
                        "verification": "verification-gate",
                        "patterns": ["pdf"],
                    }
                ]
            }
        )

        self.assertEqual(
            [reference.field for reference in policy_references(parsed.policy)],
            ["primary", "supporting", "supporting", "verification"],
        )

    def test_runtime_fails_open_when_configured_inventory_is_invalid(self) -> None:
        config = {"routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}]}
        inventory = InventorySnapshot("invalid", None, (), ("revision_mismatch",))

        self.assertIsNone(route_prompt("pdf", config, inventory))

    def test_resolver_rejects_a_canonical_binding_mismatch(self) -> None:
        config = {
            "schemaVersion": 2,
            "skillBindings": {"pdf-work": {"skill": "pdf", "canonicalId": "plugin/wrong/pdf"}},
            "routes": [
                {
                    "id": "pdf",
                    "intent": "work_with_pdf",
                    "capabilityRequirements": {"primary": ["pdf-work"]},
                    "match": {"any": [{"id": "pdf.token", "regex": "pdf"}]},
                }
            ],
        }
        parsed = parse_policy_config(config)
        inventory = InventorySnapshot(
            "available",
            "revision",
            (available_skill("pdf", "host/codex/skills/pdf"),),
        )

        resolved = resolve_policy(parsed.policy, inventory)

        self.assertFalse(resolved.valid)
        self.assertIn("skill_canonical_id_mismatch", {finding.code for finding in resolved.findings})
        self.assertEqual(resolved.references[0].status, "canonical_mismatch")
        self.assertEqual(resolved.references[0].resolved_canonical_id, "host/codex/skills/pdf")
        self.assertIsNone(route_prompt("pdf", config, inventory))
        self.assertEqual(structured_recommendation_v1("pdf", config, inventory)["recommendations"], [])

    def test_runtime_drops_an_unresolved_default_verification(self) -> None:
        config = {
            "allowedSkills": ["pdf", "evil-verifier"],
            "defaultVerification": "evil-verifier",
            "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
        }
        inventory = InventorySnapshot(
            "available",
            "revision",
            (available_skill("pdf", "host/codex/skills/pdf"),),
        )

        resolved = resolve_policy(parse_policy_config(config).policy, inventory)
        context = route_prompt("pdf", config, inventory)

        self.assertIsNone(resolved.policy.default_verification)
        self.assertIsNotNone(context)
        self.assertIn("Verification skill: none", context)
        self.assertNotIn("evil-verifier", context)

    def test_resolver_distinguishes_missing_inactive_and_ambiguous_skills(self) -> None:
        config = {
            "routes": [
                {
                    "name": "skills",
                    "primary": "missing",
                    "supporting": ["inactive", "ambiguous"],
                    "patterns": ["skills"],
                }
            ]
        }
        inventory = InventorySnapshot(
            "available",
            "revision",
            (
                {**available_skill("inactive", "host/inactive"), "availability": {"status": "inactive"}},
                available_skill("ambiguous", "host/ambiguous-a"),
                available_skill("ambiguous", "host/ambiguous-b"),
            ),
        )

        resolved = resolve_policy(parse_policy_config(config).policy, inventory)

        self.assertEqual(
            {finding.code for finding in resolved.findings},
            {"skill_missing", "skill_inactive", "skill_ambiguous"},
        )
        self.assertEqual(
            [reference.status for reference in resolved.references],
            ["missing", "inactive", "ambiguous"],
        )


if __name__ == "__main__":
    unittest.main()
