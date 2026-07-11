from __future__ import annotations

import copy
import unittest

from lazy_skill_router_core import shadow_route_matches
from lazy_skill_router_policy import compile_policy, promoted_config, shadow_route, stage_policy
from tests.test_schema_v2 import schema_v2_config


def normalized_v2_proposal() -> dict[str, object]:
    return {
        "schema": "lazy-skill-router.policy-proposal/v2",
        "inventoryRevision": "inventory-revision",
        "hostCatalogRevision": None,
        "generatedBy": {"host": "codex", "model": "app-llm", "promptVersion": "v2"},
        "routes": [
            {
                "id": "pdf-generated",
                "intent": "work_with_pdf_generated",
                "primary": "pdf",
                "supporting": ["writing-polish"],
                "verification": "verification-gate",
                "reason": "Matched a validated app-LLM policy route.",
                "patterns": [
                    {"id": "pdf.generated", "regex": "pdf generated", "label": "pdf.generated", "weight": 1.0}
                ],
                "excludePatterns": [],
                "positiveExamples": ["pdf generated"],
                "negativeExamples": ["github ci"],
                "resolvedBindings": {
                    "primary": {"canonicalId": "host/codex/skills/pdf", "configuredName": "pdf"},
                    "supporting": [
                        {
                            "canonicalId": "host/codex/skills/writing-polish",
                            "configuredName": "writing-polish",
                        }
                    ],
                    "verification": {
                        "canonicalId": "host/codex/skills/verification-gate",
                        "configuredName": "verification-gate",
                    },
                },
            }
        ],
        "retireRoutes": [],
    }


class PolicyV2CompileTest(unittest.TestCase):
    def test_compiler_preserves_v2_base_and_adds_canonical_shadow_bindings(self) -> None:
        base = schema_v2_config()
        original_routes = copy.deepcopy(base["routes"])

        candidate = compile_policy(base, normalized_v2_proposal(), "proposal-revision")

        self.assertEqual(candidate["schemaVersion"], 2)
        self.assertEqual(candidate["routes"][: len(original_routes)], original_routes)
        added = candidate["routes"][-1]
        self.assertEqual(added["id"], "pdf-generated")
        self.assertEqual(added["lifecycle"], {"state": "shadow", "proposalRevision": "proposal-revision"})
        primary_capability = added["capabilityRequirements"]["primary"][0]
        self.assertEqual(primary_capability, "generated.pdf-generated.primary.0")
        self.assertEqual(
            candidate["skillBindings"][primary_capability],
            {"skill": "pdf", "canonicalId": "host/codex/skills/pdf"},
        )
        self.assertNotIn("reason", added)
        self.assertNotIn("label", added["match"]["any"][0])

        added_count, retired_count, proposal_revision = stage_policy(
            base,
            candidate,
            "inventory-revision",
            None,
        )
        self.assertEqual((added_count, retired_count, proposal_revision), (1, 0, "proposal-revision"))
        self.assertIsNotNone(shadow_route(candidate, "pdf-generated"))

    def test_v2_shadow_route_can_be_promoted_without_changing_schema(self) -> None:
        candidate = compile_policy(schema_v2_config(), normalized_v2_proposal(), "proposal-revision")
        gate = {
            "schema": "lazy-skill-router.policy-promotion-gate/v1",
            "samples": 5,
            "helpfulRate": 1.0,
            "harmful": 0,
        }

        promoted = promoted_config(candidate, "pdf-generated", gate)

        self.assertEqual(promoted["schemaVersion"], 2)
        self.assertEqual(promoted["routes"][-1]["lifecycle"]["state"], "active")

    def test_v2_compiler_preserves_activation_facets(self) -> None:
        proposal = normalized_v2_proposal()
        proposal["routes"][0]["patterns"] = [
            {
                "id": "pdf.target",
                "regex": "pdf",
                "label": "pdf.target",
                "weight": 1.0,
                "facet": "target",
            },
            {
                "id": "pdf.action",
                "regex": "generated",
                "label": "pdf.action",
                "weight": 1.0,
                "facet": "action",
            },
        ]
        proposal["routes"][0]["activation"] = {
            "requiredFacets": ["target", "action"],
            "scope": "phase",
            "mode": "propose-only",
        }

        candidate = compile_policy(schema_v2_config(), proposal, "proposal-revision")

        added = candidate["routes"][-1]
        self.assertEqual(added["activation"], proposal["routes"][0]["activation"])
        self.assertEqual(
            [pattern["facet"] for pattern in added["match"]["any"]],
            ["target", "action"],
        )

    def test_v2_compiler_extends_existing_allowlist_for_shadow_route(self) -> None:
        base = schema_v2_config()
        base["allowedSkills"] = ["personal-skill-router"]

        candidate = compile_policy(base, normalized_v2_proposal(), "proposal-revision")

        self.assertEqual(
            candidate["allowedSkills"],
            ["pdf", "personal-skill-router", "verification-gate", "writing-polish"],
        )
        self.assertEqual(shadow_route_matches("pdf generated", candidate)[0].route.name, "pdf-generated")
        self.assertEqual(stage_policy(base, candidate, "inventory-revision", None)[:2], (1, 0))

    def test_v2_compiler_can_retire_an_existing_route(self) -> None:
        base = schema_v2_config()
        proposal = normalized_v2_proposal()
        proposal["routes"] = []
        proposal["retireRoutes"] = [{"id": "pdf", "reason": "No longer available."}]

        candidate = compile_policy(base, proposal, "retirement-revision")

        self.assertEqual(candidate["routes"][0]["id"], "pdf")
        self.assertEqual(candidate["routes"][0]["lifecycle"]["state"], "disabled")
        self.assertEqual(stage_policy(base, candidate, "inventory-revision", None)[:2], (0, 1))


if __name__ == "__main__":
    unittest.main()
