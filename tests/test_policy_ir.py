from __future__ import annotations

import copy
import unittest
from unittest import mock

from lazy_skill_router_activation import (
    DEFAULT_ACTION_PATTERNS,
    DEFAULT_META_PATTERNS,
    DEFAULT_NO_ACTION_PATTERNS,
)
from lazy_skill_router_contracts import structured_recommendation_v1
from lazy_skill_router_core import route_prompt
from lazy_skill_router_inventory import InventorySnapshot
from lazy_skill_router_policy_ir import (
    activation_pattern_risk,
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
    def test_v1_policy_text_boundaries_fail_open_without_truncation(self) -> None:
        def base_config() -> dict[str, object]:
            return {
                "routes": [
                    {
                        "name": "route",
                        "intent": "intent",
                        "primary": "primary",
                        "supporting": ["supporting"],
                        "verification": "verification",
                        "patterns": [
                            {
                                "id": "pattern",
                                "regex": "needle",
                                "label": "label",
                                "facet": "facet",
                            }
                        ],
                        "activation": {"requiredFacets": ["facet"]},
                        "lifecycle": {"state": "active", "proposalRevision": "revision"},
                        "reason": "reason",
                    }
                ]
            }

        def route(config: dict[str, object]) -> dict[str, object]:
            return config["routes"][0]  # type: ignore[index,return-value]

        cases = (
            ("route", "route_id_invalid", lambda config, value: route(config).__setitem__("name", value)),
            ("intent", "route_intent_invalid", lambda config, value: route(config).__setitem__("intent", value)),
            ("primary", "route_primary_invalid", lambda config, value: route(config).__setitem__("primary", value)),
            (
                "supporting",
                "route_supporting_invalid",
                lambda config, value: route(config).__setitem__("supporting", [value]),
            ),
            (
                "verification",
                "route_verification_invalid",
                lambda config, value: route(config).__setitem__("verification", value),
            ),
            ("reason", "route_reason_invalid", lambda config, value: route(config).__setitem__("reason", value)),
            (
                "pattern-id",
                "route_pattern_id_invalid",
                lambda config, value: route(config)["patterns"][0].__setitem__("id", value),  # type: ignore[index,union-attr]
            ),
            (
                "pattern-label",
                "route_pattern_label_invalid",
                lambda config, value: route(config)["patterns"][0].__setitem__("label", value),  # type: ignore[index,union-attr]
            ),
            (
                "facet",
                "route_pattern_facet_invalid",
                lambda config, value: (
                    route(config)["patterns"][0].__setitem__("facet", value),  # type: ignore[index,union-attr]
                    route(config)["activation"].__setitem__("requiredFacets", [value]),  # type: ignore[union-attr]
                ),
            ),
            (
                "proposal-revision",
                "route_proposal_revision_invalid",
                lambda config, value: route(config)["lifecycle"].__setitem__("proposalRevision", value),  # type: ignore[union-attr]
            ),
            (
                "allowed-skill",
                "allowed_skills_invalid",
                lambda config, value: (
                    config.__setitem__("allowedSkills", [value]),
                    route(config).__setitem__("primary", value),
                ),
            ),
            (
                "default-verification",
                "default_verification_invalid",
                lambda config, value: config.__setitem__("defaultVerification", value),
            ),
        )

        for field, expected_code, mutate in cases:
            with self.subTest(field=field, boundary="accepted"):
                config = base_config()
                mutate(config, "x" * 160)
                parsed = parse_policy_config(config)
                self.assertTrue(parsed.valid, parsed.findings)
            with self.subTest(field=field, boundary="rejected"):
                config = base_config()
                mutate(config, "x" * 161)
                parsed = parse_policy_config(config)
                self.assertFalse(parsed.valid)
                self.assertIn(expected_code, {finding.code for finding in parsed.findings})
                self.assertIsNone(route_prompt("needle", config))

        for size, expected_valid in ((160, True), (161, False)):
            with self.subTest(field="unicode-route", size=size):
                config = base_config()
                route(config)["name"] = "가" * size
                parsed = parse_policy_config(config)
                self.assertEqual(parsed.valid, expected_valid)

    def test_v2_policy_text_boundaries_cover_binding_forms_and_facets(self) -> None:
        def base_config() -> dict[str, object]:
            return {
                "schemaVersion": 2,
                "policyVersion": "policy",
                "selection": {
                    "mode": "ranked",
                    "maxRecommendations": 1,
                    "minMatchStrength": 0.5,
                    "minScoreMargin": 0.1,
                },
                "skillBindings": {"capability": "skill"},
                "routes": [
                    {
                        "id": "route",
                        "intent": "intent",
                        "capabilityRequirements": {"primary": ["capability"]},
                        "match": {
                            "any": [
                                {
                                    "id": "pattern",
                                    "regex": "needle",
                                    "label": "label",
                                    "facet": "facet",
                                }
                            ]
                        },
                        "activation": {"requiredFacets": ["facet"]},
                        "lifecycle": {"state": "active", "proposalRevision": "revision"},
                        "reason": "reason",
                    }
                ],
            }

        def route(config: dict[str, object]) -> dict[str, object]:
            return config["routes"][0]  # type: ignore[index,return-value]

        def set_capability(config: dict[str, object], value: str) -> None:
            config["skillBindings"] = {value: "skill"}
            route(config)["capabilityRequirements"] = {"primary": [value]}

        def set_fallback(config: dict[str, object], value: str) -> None:
            config["fallbackRouteId"] = value
            route(config)["id"] = value

        cases = (
            (
                "policy-version",
                "policy_version_invalid",
                lambda config, value: config.__setitem__("policyVersion", value),
            ),
            ("route", "route_id_invalid", lambda config, value: route(config).__setitem__("id", value)),
            ("fallback-route", "fallback_route_id_invalid", set_fallback),
            ("intent", "route_intent_invalid", lambda config, value: route(config).__setitem__("intent", value)),
            ("capability", "skill_binding_capability_invalid", set_capability),
            (
                "binding-string",
                "skill_binding_name_invalid",
                lambda config, value: config.__setitem__("skillBindings", {"capability": value}),
            ),
            (
                "binding-object",
                "skill_binding_name_invalid",
                lambda config, value: config.__setitem__("skillBindings", {"capability": {"skill": value}}),
            ),
            (
                "pattern-id",
                "route_pattern_id_invalid",
                lambda config, value: route(config)["match"]["any"][0].__setitem__("id", value),  # type: ignore[index,union-attr]
            ),
            (
                "pattern-label",
                "route_pattern_label_invalid",
                lambda config, value: route(config)["match"]["any"][0].__setitem__("label", value),  # type: ignore[index,union-attr]
            ),
            (
                "facet",
                "route_pattern_facet_invalid",
                lambda config, value: (
                    route(config)["match"]["any"][0].__setitem__("facet", value),  # type: ignore[index,union-attr]
                    route(config)["activation"].__setitem__("requiredFacets", [value]),  # type: ignore[union-attr]
                ),
            ),
            (
                "proposal-revision",
                "route_proposal_revision_invalid",
                lambda config, value: route(config)["lifecycle"].__setitem__("proposalRevision", value),  # type: ignore[union-attr]
            ),
            ("reason", "route_reason_invalid", lambda config, value: route(config).__setitem__("reason", value)),
        )

        for field, expected_code, mutate in cases:
            with self.subTest(field=field, boundary="accepted"):
                config = copy.deepcopy(base_config())
                mutate(config, "x" * 160)
                parsed = parse_policy_config(config)
                self.assertTrue(parsed.valid, parsed.findings)
            with self.subTest(field=field, boundary="rejected"):
                config = copy.deepcopy(base_config())
                mutate(config, "x" * 161)
                parsed = parse_policy_config(config)
                self.assertFalse(parsed.valid)
                self.assertIn(expected_code, {finding.code for finding in parsed.findings})

    def test_implicit_pattern_id_stays_within_policy_identifier_limit(self) -> None:
        route_name = "x" * 160
        parsed = parse_policy_config({"routes": [{"name": route_name, "primary": "skill", "patterns": ["needle"]}]})

        self.assertTrue(parsed.valid, parsed.findings)
        self.assertLessEqual(len(parsed.policy.routes[0].patterns[0].pattern_id), 160)

    def test_v1_empty_or_null_intent_keeps_the_legacy_route_id_fallback(self) -> None:
        for intent in ("", None):
            with self.subTest(intent=intent):
                parsed = parse_policy_config(
                    {
                        "routes": [
                            {
                                "name": "legacy-route",
                                "intent": intent,
                                "primary": "skill",
                                "patterns": ["needle"],
                            }
                        ]
                    }
                )

                self.assertTrue(parsed.valid, parsed.findings)
                self.assertEqual(parsed.policy.routes[0].intent_id, "legacy-route")

    def test_capability_retrieval_algorithm_is_validated_by_shared_policy_parser(self) -> None:
        valid = parse_policy_config(
            {
                "capabilityRetrieval": {
                    "mode": "shadow",
                    "maxCandidates": 3,
                    "algorithm": "lexical-bm25-char3-anchored/v2",
                },
                "routes": [],
            }
        )
        invalid = parse_policy_config(
            {
                "capabilityRetrieval": {
                    "mode": "shadow",
                    "maxCandidates": 3,
                    "algorithm": "unknown/v9",
                },
                "routes": [],
            }
        )
        invalid_type = parse_policy_config(
            {
                "capabilityRetrieval": {
                    "mode": "shadow",
                    "maxCandidates": 3,
                    "algorithm": [],
                },
                "routes": [],
            }
        )
        replay_only = parse_policy_config(
            {
                "capabilityRetrieval": {
                    "mode": "shadow",
                    "maxCandidates": 3,
                    "algorithm": "lexical-bm25-char3/v1",
                },
                "routes": [],
            }
        )

        self.assertNotIn("capability_retrieval_fields_unknown", {finding.code for finding in valid.findings})
        self.assertNotIn("capability_retrieval_algorithm_invalid", {finding.code for finding in valid.findings})
        self.assertIn("capability_retrieval_algorithm_invalid", {finding.code for finding in invalid.findings})
        self.assertIn("capability_retrieval_algorithm_invalid", {finding.code for finding in invalid_type.findings})
        self.assertIn("capability_retrieval_algorithm_replay_only", {finding.code for finding in replay_only.findings})

    def test_activation_pattern_structure_is_rejected_before_regex_execution(self) -> None:
        compiled = mock.Mock()

        risk = activation_pattern_risk(r"(a+)+$", compiled)

        self.assertEqual(risk, "nested repetition is unsupported")
        compiled.search.assert_not_called()

    def test_activation_patterns_reject_catastrophic_custom_regex(self) -> None:
        fields_and_codes = (
            ("metaPatterns", "activation_meta_pattern_regex_unsafe"),
            ("actionPatterns", "activation_action_pattern_regex_unsafe"),
            ("noActionPatterns", "activation_no_action_pattern_regex_unsafe"),
        )

        for field, expected_code in fields_and_codes:
            with self.subTest(field=field):
                parsed = parse_policy_config(
                    {
                        "activation": {"mode": "inject", field: [r"(a+)+$"]},
                        "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
                    }
                )

                self.assertFalse(parsed.valid)
                self.assertIn(expected_code, {finding.code for finding in parsed.findings})

    def test_shipped_activation_patterns_pass_the_custom_regex_boundary(self) -> None:
        parsed = parse_policy_config(
            {
                "activation": {
                    "mode": "inject",
                    "metaPatterns": list(DEFAULT_META_PATTERNS),
                    "actionPatterns": list(DEFAULT_ACTION_PATTERNS),
                    "noActionPatterns": list(DEFAULT_NO_ACTION_PATTERNS),
                },
                "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
            }
        )

        self.assertTrue(parsed.valid, parsed.findings)

    def test_activation_patterns_reject_ambiguous_custom_repeats(self) -> None:
        for pattern in (r"a*a*a*a*a*a*b", r"\s+$"):
            with self.subTest(pattern=pattern):
                parsed = parse_policy_config(
                    {
                        "activation": {"mode": "inject", "actionPatterns": [pattern]},
                        "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
                    }
                )

                self.assertFalse(parsed.valid)
                self.assertIn("activation_action_pattern_regex_unsafe", {finding.code for finding in parsed.findings})

    def test_activation_patterns_reject_before_compile_resource_errors(self) -> None:
        for pattern in (
            ("(" * 2_000) + "a" + (")" * 2_000),
            r"a{999999999999999999999999999999999999}",
        ):
            with self.subTest(pattern=pattern[:40]):
                parsed = parse_policy_config(
                    {
                        "activation": {"mode": "inject", "actionPatterns": [pattern]},
                        "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
                    }
                )

                self.assertFalse(parsed.valid)
                self.assertIn("activation_action_pattern_regex_unsafe", {item.code for item in parsed.findings})

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
                    "lifecycle": {"state": "active"},
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
                    "lifecycle": {"state": "active"},
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
                    "lifecycle": {"state": "active"},
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
                    "lifecycle": {"state": "active"},
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
