from __future__ import annotations

import json
import unittest

from lazy_skill_router_activation import (
    ACTIVATION_IR_SCHEMA,
    DEFAULT_ACTION_PATTERNS,
    DEFAULT_META_PATTERNS,
    DEFAULT_NO_ACTION_PATTERNS,
    activation_ir_dict,
    activation_policy,
    decide_activation,
    default_meta_prompt_matches,
)
from lazy_skill_router_policy_ir import parse_policy_config, runtime_routes
from lazy_skill_router_scoring import CapabilityRequirements, Route, RouteActivation, RouteMatch, RoutePattern


def route_pattern(pattern_id: str, regex: str, *, facet: str = "signal", weight: float = 1.0) -> RoutePattern:
    return RoutePattern(regex=regex, label=pattern_id, pattern_id=pattern_id, weight=weight, facet=facet)


def route(
    *,
    name: str = "route",
    primary: str = "primary-skill",
    supporting: tuple[str, ...] = (),
    verification: str = "",
    patterns: tuple[RoutePattern, ...] = (),
    fallback: bool = False,
    required_facets: tuple[str, ...] = (),
    scope: str = "turn",
    activation_mode: str = "auto",
) -> Route:
    verification_requirements = (verification,) if verification else ()
    return Route(
        name=name,
        primary=primary,
        supporting=supporting,
        verification=verification,
        reason="test route",
        patterns=patterns,
        exclude_patterns=(),
        priority=0.0,
        weight=0.0,
        fallback=fallback,
        intent="test.intent",
        capability_requirements=CapabilityRequirements(
            primary=(primary,),
            supporting=supporting,
            verification=verification_requirements,
        ),
        activation=RouteActivation(required_facets=required_facets, scope=scope, mode=activation_mode),
    )


def match(
    route_value: Route,
    *,
    confidence: float,
    score: float,
    matched_pattern_ids: tuple[str, ...],
) -> RouteMatch:
    by_id = {pattern.pattern_id: pattern for pattern in route_value.patterns}
    matched = tuple(by_id[pattern_id] for pattern_id in matched_pattern_ids)
    return RouteMatch(
        route=route_value,
        confidence=confidence,
        score=score,
        matched_signals=tuple(pattern.label for pattern in matched),
        matched_patterns=tuple(pattern.regex for pattern in matched),
        matched_pattern_ids=matched_pattern_ids,
    )


class ActivationIRTest(unittest.TestCase):
    def test_default_meta_detection_preserves_legacy_order_and_line_boundaries(self) -> None:
        for prompt in (
            "skill select why",
            "skill why select",
            "why skill select",
            "select skill why",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(default_meta_prompt_matches(prompt))

        for prompt in (
            "why select skill",
            "select why skill",
            "skill\nselect\nwhy",
            "skill select " + ("x" * 100_000),
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertFalse(default_meta_prompt_matches(prompt))

    def test_unsafe_custom_activation_patterns_never_reach_runtime_matching(self) -> None:
        for unsafe in (
            r"(a+)+$",
            ("(" * 2_000) + "a" + (")" * 2_000),
            r"a{999999999999999999999999999999999999}",
        ):
            with self.subTest(pattern=unsafe[:40]):
                policy = activation_policy(
                    {
                        "activation": {
                            "metaPatterns": [unsafe],
                            "actionPatterns": [unsafe],
                            "noActionPatterns": [unsafe],
                        }
                    }
                )

                self.assertEqual(policy.meta_patterns, DEFAULT_META_PATTERNS)
                self.assertEqual(policy.action_patterns, DEFAULT_ACTION_PATTERNS)
                self.assertEqual(policy.no_action_patterns, DEFAULT_NO_ACTION_PATTERNS)

    def test_no_match_abstains(self) -> None:
        activation = decide_activation("no routed skill here", (), {}, answer_only=False)

        self.assertEqual(activation.disposition, "abstain")
        self.assertEqual(activation.reason_code, "no_candidate")
        self.assertIsNone(activation.route_id)
        self.assertEqual(activation.scope, "turn")
        self.assertEqual(activation_ir_dict(activation)["activatedSkills"], [])

    def test_one_weak_route_proposes_weak_evidence(self) -> None:
        candidate = route(
            supporting=("support-skill",),
            verification="verification-skill",
            patterns=(route_pattern("weak.signal", r"draft"),),
        )

        activation = decide_activation(
            "please draft this",
            (match(candidate, confidence=0.65, score=0.65, matched_pattern_ids=("weak.signal",)),),
            {},
            answer_only=False,
        )

        self.assertEqual(activation.disposition, "propose")
        self.assertEqual(activation.reason_code, "weak_evidence")
        self.assertEqual([skill.role for skill in activation.activated_skills], [])
        self.assertEqual(
            [(skill.role, skill.configured_name, skill.state) for skill in activation.deferred_skills],
            [
                ("primary", "primary-skill", "deferred"),
                ("supporting", "support-skill", "deferred"),
                ("verification", "verification-skill", "deferred"),
            ],
        )

    def test_strong_two_signal_route_activates_primary_only(self) -> None:
        candidate = route(
            primary="primary-skill",
            supporting=("support-skill",),
            verification="verification-skill",
            patterns=(
                route_pattern("signal.one", r"pdf"),
                route_pattern("signal.two", r"extract"),
            ),
        )

        activation = decide_activation(
            "extract from pdf",
            (
                match(
                    candidate,
                    confidence=0.80,
                    score=0.92,
                    matched_pattern_ids=("signal.one", "signal.two"),
                ),
            ),
            {},
            answer_only=False,
        )

        self.assertTrue(activation.should_activate)
        self.assertEqual(activation.reason_code, "eligible")
        self.assertEqual(
            [(skill.role, skill.configured_name, skill.state) for skill in activation.activated_skills],
            [("primary", "primary-skill", "activated")],
        )
        self.assertEqual(
            [(skill.role, skill.configured_name, skill.state) for skill in activation.deferred_skills],
            [
                ("supporting", "support-skill", "deferred"),
                ("verification", "verification-skill", "deferred"),
            ],
        )

    def test_meta_skill_router_discussion_abstains_without_exposing_skills(self) -> None:
        candidate = route(patterns=(route_pattern("router.skill", r"router"),))

        activation = decide_activation(
            "왜 router 가 이 skill 을 사용했어?",
            (match(candidate, confidence=0.95, score=0.95, matched_pattern_ids=("router.skill",)),),
            {},
            answer_only=False,
        )

        self.assertEqual(activation.disposition, "abstain")
        self.assertEqual(activation.reason_code, "meta_context")
        self.assertEqual(activation.intent_frame.request_mode, "meta")
        self.assertEqual(activation.activated_skills, ())
        self.assertEqual(activation.deferred_skills, ())

    def test_answer_only_proposes_instead_of_activating(self) -> None:
        candidate = route(patterns=(route_pattern("answer.signal", r"summary"),))

        activation = decide_activation(
            "summarize this",
            (match(candidate, confidence=0.95, score=0.95, matched_pattern_ids=("answer.signal",)),),
            {},
            answer_only=True,
        )

        self.assertEqual(activation.disposition, "propose")
        self.assertEqual(activation.reason_code, "answer_only")
        self.assertEqual(activation.intent_frame.request_mode, "answer-only")

    def test_explicit_fix_action_overrides_meta_rationale_detection(self) -> None:
        candidate = route(
            patterns=(
                route_pattern("router.signal", r"skill"),
                route_pattern("fix.signal", r"fix"),
            )
        )

        activation = decide_activation(
            "fix the skill selection problem",
            (
                match(
                    candidate,
                    confidence=0.80,
                    score=0.80,
                    matched_pattern_ids=("router.signal", "fix.signal"),
                ),
            ),
            {},
            answer_only=False,
        )

        self.assertEqual(activation.intent_frame.request_mode, "action")
        self.assertEqual(activation.disposition, "activate")
        self.assertEqual(activation.reason_code, "eligible")

    def test_explicit_action_overrides_soft_explanation_pattern(self) -> None:
        candidate = route(
            patterns=(
                route_pattern("ci.signal", r"CI"),
                route_pattern("failure.signal", r"실패"),
            )
        )

        activation = decide_activation(
            "GitHub PR의 CI 실패를 고치고 원인도 설명해줘",
            (
                match(
                    candidate,
                    confidence=0.80,
                    score=0.80,
                    matched_pattern_ids=("ci.signal", "failure.signal"),
                ),
            ),
            {},
            answer_only=True,
        )

        self.assertEqual(activation.intent_frame.request_mode, "action")
        self.assertEqual(activation.disposition, "activate")

    def test_explanation_how_to_phrases_do_not_activate_action_skills(self) -> None:
        candidate = route(
            patterns=(
                route_pattern("ci.signal", r"CI"),
                route_pattern("failure.signal", r"failure|실패"),
            )
        )
        prompts = (
            "Explain how to fix a GitHub Actions failure",
            "GitHub Actions failure 수정 방법만 설명해줘",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                activation = decide_activation(
                    prompt,
                    (
                        match(
                            candidate,
                            confidence=0.95,
                            score=0.95,
                            matched_pattern_ids=("ci.signal", "failure.signal"),
                        ),
                    ),
                    {},
                    answer_only=True,
                )

                self.assertEqual(activation.intent_frame.request_mode, "answer-only")
                self.assertEqual(activation.disposition, "propose")
                self.assertEqual(activation.reason_code, "answer_only")

    def test_true_fix_imperatives_remain_actions(self) -> None:
        candidate = route(
            patterns=(
                route_pattern("ci.signal", r"CI"),
                route_pattern("failure.signal", r"failure|실패"),
            )
        )
        prompts = (
            "Fix the GitHub Actions failure",
            "GitHub Actions 실패 수정해줘",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                activation = decide_activation(
                    prompt,
                    (
                        match(
                            candidate,
                            confidence=0.95,
                            score=0.95,
                            matched_pattern_ids=("ci.signal", "failure.signal"),
                        ),
                    ),
                    {},
                    answer_only=False,
                )

                self.assertEqual(activation.intent_frame.request_mode, "action")
                self.assertEqual(activation.disposition, "activate")
                self.assertEqual(activation.reason_code, "eligible")

    def test_hard_no_action_pattern_wins_over_explicit_action(self) -> None:
        candidate = route(patterns=(route_pattern("router.signal", r"skill"),))

        activation = decide_activation(
            "Don't fix the skill router; explain why this skill was selected",
            (match(candidate, confidence=0.95, score=0.95, matched_pattern_ids=("router.signal",)),),
            {},
            answer_only=True,
        )

        self.assertEqual(activation.intent_frame.request_mode, "meta")
        self.assertEqual(activation.disposition, "abstain")
        self.assertEqual(activation.reason_code, "meta_context")

    def test_propose_only_route_never_auto_activates(self) -> None:
        candidate = route(
            activation_mode="propose-only",
            patterns=(
                route_pattern("router.signal", r"skill router"),
                route_pattern("fix.signal", r"fix"),
            ),
        )

        activation = decide_activation(
            "fix the skill router logic",
            (
                match(
                    candidate,
                    confidence=0.95,
                    score=0.95,
                    matched_pattern_ids=("router.signal", "fix.signal"),
                ),
            ),
            {},
            answer_only=False,
        )

        self.assertEqual(activation.intent_frame.request_mode, "action")
        self.assertEqual(activation.disposition, "propose")
        self.assertEqual(activation.reason_code, "route_propose_only")
        self.assertEqual(activation.activated_skills, ())

    def test_ambiguous_candidates_propose(self) -> None:
        first = route(name="first", patterns=(route_pattern("first.signal", r"pdf"),))
        second = route(name="second", primary="secondary-skill", patterns=(route_pattern("second.signal", r"pdf"),))

        activation = decide_activation(
            "pdf help",
            (
                match(first, confidence=0.95, score=0.90, matched_pattern_ids=("first.signal",)),
                match(second, confidence=0.95, score=0.87, matched_pattern_ids=("second.signal",)),
            ),
            {},
            answer_only=False,
        )

        self.assertEqual(activation.route_id, "first")
        self.assertEqual(activation.disposition, "propose")
        self.assertEqual(activation.reason_code, "ambiguous_candidates")
        self.assertTrue(activation.intent_frame.ambiguous)

    def test_required_facets_gate_activation_and_scope_propagates(self) -> None:
        config = {
            "schemaVersion": 2,
            "policyVersion": "activation-test",
            "selection": {
                "mode": "ranked",
                "maxRecommendations": 1,
                "minMatchStrength": 0.55,
                "minScoreMargin": 0.05,
            },
            "skillBindings": {"docs": "documents"},
            "routes": [
                {
                    "id": "document-workflow",
                    "intent": "document.workflow",
                    "capabilityRequirements": {"primary": ["docs"]},
                    "match": {
                        "any": [
                            {"id": "facet.read", "regex": "read", "facet": "read"},
                            {"id": "facet.write", "regex": "write", "facet": "write"},
                        ]
                    },
                    "activation": {"requiredFacets": ["read", "write"], "scope": "task"},
                    "lifecycle": {"state": "active"},
                }
            ],
        }
        parsed = parse_policy_config(config)
        self.assertTrue(parsed.valid, parsed.findings)
        candidate = runtime_routes(parsed.policy)[0]

        missing = decide_activation(
            "read only",
            (match(candidate, confidence=0.95, score=0.95, matched_pattern_ids=("facet.read",)),),
            config,
            answer_only=False,
        )
        ready = decide_activation(
            "read and write",
            (
                match(
                    candidate,
                    confidence=0.95,
                    score=0.95,
                    matched_pattern_ids=("facet.read", "facet.write"),
                ),
            ),
            config,
            answer_only=False,
        )

        self.assertEqual(missing.disposition, "propose")
        self.assertEqual(missing.reason_code, "missing_required_facets")
        self.assertEqual(missing.intent_frame.missing_required_facets, ("write",))
        self.assertEqual(missing.scope, "task")
        self.assertEqual(ready.disposition, "activate")
        self.assertEqual(ready.reason_code, "eligible")
        self.assertEqual(ready.intent_frame.matched_facets, ("read", "write"))
        self.assertEqual(ready.scope, "task")

    def test_activation_ir_redacts_prompt_and_regex_but_keeps_stable_ids(self) -> None:
        prompt = "customer-secret-123 needs a document workflow"
        candidate = route(
            patterns=(
                route_pattern("evidence.read", r"raw-regex-secret-456", facet="read"),
                route_pattern("evidence.write", r"raw-regex-secret-789", facet="write"),
            ),
            required_facets=("read", "write"),
        )
        activation = decide_activation(
            prompt,
            (
                match(
                    candidate,
                    confidence=0.95,
                    score=0.95,
                    matched_pattern_ids=("evidence.read", "evidence.write"),
                ),
            ),
            {},
            answer_only=False,
        )
        payload = activation_ir_dict(activation)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(payload["schema"], ACTIVATION_IR_SCHEMA)
        self.assertEqual(payload["reasonCode"], "eligible")
        self.assertEqual(payload["evidenceIds"], ["evidence.read", "evidence.write"])
        self.assertNotIn(prompt, encoded)
        self.assertNotIn("raw-regex-secret-456", encoded)
        self.assertNotIn("raw-regex-secret-789", encoded)

    def test_policy_ir_rejects_invalid_activation_contracts(self) -> None:
        config = {
            "activation": {
                "mode": [],
                "autoActivateMinStrength": 2,
                "metaPatterns": ["["],
                "actionPatterns": ["("],
                "noActionPatterns": [")"],
            },
            "routes": [
                {
                    "name": "invalid",
                    "primary": "skill",
                    "patterns": [{"id": "signal", "regex": "signal", "facet": "target"}],
                    "activation": {"requiredFacets": ["action"], "scope": "forever", "mode": "always"},
                }
            ],
        }

        parsed = parse_policy_config(config)
        codes = {finding.code for finding in parsed.findings if finding.severity == "ERROR"}

        self.assertFalse(parsed.valid)
        self.assertIn("activation_mode_invalid", codes)
        self.assertIn("activation_auto_strength_invalid", codes)
        self.assertIn("activation_meta_pattern_regex_invalid", codes)
        self.assertIn("activation_action_pattern_regex_invalid", codes)
        self.assertIn("activation_no_action_pattern_regex_invalid", codes)
        self.assertIn("route_activation_facets_unbound", codes)
        self.assertIn("route_activation_scope_invalid", codes)
        self.assertIn("route_activation_mode_invalid", codes)


if __name__ == "__main__":
    unittest.main()
