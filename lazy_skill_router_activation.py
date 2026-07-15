from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from lazy_skill_router_common import MAX_ROUTABLE_PROMPT_CHARS
from lazy_skill_router_policy_ir import (
    ACTIVATION_REGEX_ERRORS,
    SHIPPED_ACTIVATION_PATTERN_BUNDLES,
    activation_pattern_risk,
    normalize_v1_leading_positive_lookahead,
)
from lazy_skill_router_scoring import RouteMatch, matched_patterns, tuple_of_strings

ACTIVATION_IR_SCHEMA = "lazy-skill-router.activation-ir/v1"
ACTIVATION_DISPOSITIONS = frozenset({"activate", "propose", "abstain"})
DEFAULT_AUTO_ACTIVATE_MIN_STRENGTH = 0.80
DEFAULT_MIN_SCORE_MARGIN = 0.05
DEFAULT_ACTIVATION_SCOPE = "turn"
DEFAULT_META_PATTERNS: tuple[str, ...] = (
    r"(skill|스킬|route|라우트|router|라우터|hook|훅).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치).*(why|problem|wrong|explain|왜|문제|잘못|설명)",
    r"(skill|스킬|route|라우트|router|라우터|hook|훅).*(why|problem|wrong|explain|왜|문제|잘못|설명).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치)",
    r"(why|problem|wrong|explain|왜|문제|잘못|설명).*(skill|스킬|route|라우트|router|라우터|hook|훅).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치)",
    r"(select|recommend|use|activat|match|선택|추천|사용|활성|매치).*(skill|스킬|route|라우트|router|라우터|hook|훅).*(why|problem|wrong|explain|왜|문제|잘못|설명)",
)
DEFAULT_ACTION_PATTERNS: tuple[str, ...] = (
    r"\b(fix|implement|update|change|add|create|install|remove|delete)\b",
    r"(수정|구현|추가|생성|변경|설치|삭제|업데이트)(해|하고|해서|하자|하라|해라)",
    r"(고치|만들)(고|거나|면)|고쳐|만들어",
)
DEFAULT_NO_ACTION_PATTERNS: tuple[str, ...] = (
    r"(?:don't|do\s+not)\s+(?:change|edit|modify|fix|install|remove|delete)",
    r"\bno\s+(?:edits?|changes?)\b",
    r"\b(?:explain|describe)\b[^.!?\n]{0,160}\bhow(?:\s+(?:i|we|you|one|someone)\s+(?:should|could|can|would))?\s+(?:to\s+)?(?:fix|implement|update|change|add|create|install|remove|delete)\b",
    r"(수정|구현|추가|생성|변경|설치|삭제|업데이트)하지\s*마",
    r"(고치|만들)지\s*마",
    r"(?:수정|구현|추가|생성|변경|설치|삭제|업데이트|고치|만들)(?:하는|할)?\s*방법(?:만)?\s*(?:을|를)?\s*설명",
)
DEFAULT_META_TOKEN_PATTERN = re.compile(
    r"(?P<subject>skill|스킬|route|라우트|router|라우터|hook|훅)"
    r"|(?P<action>select|recommend|use|activat|match|선택|추천|사용|활성|매치)"
    r"|(?P<reason>why|problem|wrong|explain|왜|문제|잘못|설명)",
    re.IGNORECASE,
)
DEFAULT_META_TOKEN_ORDERS = (
    ("subject", "action", "reason"),
    ("subject", "reason", "action"),
    ("reason", "subject", "action"),
    ("action", "subject", "reason"),
)


@dataclass(frozen=True)
class ActivationPolicyIR:
    auto_activate_min_strength: float
    min_score_margin: float
    meta_patterns: tuple[str, ...]
    action_patterns: tuple[str, ...]
    no_action_patterns: tuple[str, ...]


@dataclass(frozen=True)
class IntentFrame:
    request_mode: str
    matched_facets: tuple[str, ...]
    missing_required_facets: tuple[str, ...]
    ambiguous: bool
    fallback: bool


@dataclass(frozen=True)
class SkillActivationIR:
    role: str
    configured_name: str
    state: str


@dataclass(frozen=True)
class ActivationIR:
    disposition: str
    reason_code: str
    route_id: str | None
    intent_id: str | None
    confidence: float
    score: float
    evidence_ids: tuple[str, ...]
    scope: str
    intent_frame: IntentFrame
    activated_skills: tuple[SkillActivationIR, ...]
    deferred_skills: tuple[SkillActivationIR, ...]

    @property
    def should_activate(self) -> bool:
        return self.disposition == "activate"

    @property
    def should_propose(self) -> bool:
        return self.disposition == "propose"


def configured_number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def validated_activation_patterns(
    value: Any,
    defaults: tuple[str, ...],
    schema_version: int,
) -> tuple[str, ...]:
    def validated(patterns: tuple[str, ...], *, trusted_bundle: bool) -> tuple[str, ...]:
        normalized_patterns: list[str] = []
        for pattern in patterns:
            normalized, anchored = (
                normalize_v1_leading_positive_lookahead(pattern) if schema_version == 1 else (pattern, False)
            )
            if (
                not trusted_bundle
                and activation_pattern_risk(
                    normalized,
                    allow_leading_positive_lookahead=anchored,
                )
                is not None
            ):
                return ()
            try:
                compiled = re.compile(normalized)
            except ACTIVATION_REGEX_ERRORS:
                return ()
            if (
                not trusted_bundle
                and activation_pattern_risk(
                    normalized,
                    compiled,
                    allow_leading_positive_lookahead=anchored,
                )
                is not None
            ):
                return ()
            normalized_patterns.append(normalized)
        return tuple(normalized_patterns)

    trusted_defaults = defaults in SHIPPED_ACTIVATION_PATTERN_BUNDLES.values()
    safe_defaults = validated(defaults, trusted_bundle=trusted_defaults)
    configured = tuple_of_strings(value)
    if not configured or configured == defaults:
        return safe_defaults
    return validated(configured, trusted_bundle=False) or safe_defaults


def activation_policy(config: dict[str, Any]) -> ActivationPolicyIR:
    activation = config.get("activation")
    activation = activation if isinstance(activation, dict) else {}
    selection = config.get("selection")
    selection = selection if isinstance(selection, dict) else {}
    schema_version = 2 if config.get("schemaVersion") == 2 else 1
    return ActivationPolicyIR(
        configured_number(
            activation.get("autoActivateMinStrength"),
            DEFAULT_AUTO_ACTIVATE_MIN_STRENGTH,
        ),
        configured_number(selection.get("minScoreMargin"), DEFAULT_MIN_SCORE_MARGIN),
        validated_activation_patterns(activation.get("metaPatterns"), DEFAULT_META_PATTERNS, schema_version),
        validated_activation_patterns(activation.get("actionPatterns"), DEFAULT_ACTION_PATTERNS, schema_version),
        validated_activation_patterns(activation.get("noActionPatterns"), DEFAULT_NO_ACTION_PATTERNS, schema_version),
    )


def matched_facets(match: RouteMatch) -> tuple[str, ...]:
    matched_ids = set(match.matched_pattern_ids)
    return tuple(dict.fromkeys(pattern.facet for pattern in match.route.patterns if pattern.pattern_id in matched_ids))


def default_meta_prompt_matches(prompt: str) -> bool:
    for line in prompt.split("\n"):
        positions = [0] * len(DEFAULT_META_TOKEN_ORDERS)
        for token_match in DEFAULT_META_TOKEN_PATTERN.finditer(line):
            category = token_match.lastgroup
            for index, order in enumerate(DEFAULT_META_TOKEN_ORDERS):
                position = positions[index]
                if position < len(order) and category == order[position]:
                    position += 1
                    if position == len(order):
                        return True
                    positions[index] = position
    return False


def score_is_ambiguous(matches: tuple[RouteMatch, ...], min_score_margin: float) -> bool:
    if len(matches) < 2:
        return False
    return max(0.0, matches[0].score - matches[1].score) < min_score_margin


def request_mode(prompt: str, answer_only: bool, policy: ActivationPolicyIR) -> str:
    meta = (
        default_meta_prompt_matches(prompt)
        if policy.meta_patterns == DEFAULT_META_PATTERNS
        else bool(matched_patterns(prompt, policy.meta_patterns))
    )
    explicit_action = bool(matched_patterns(prompt, policy.action_patterns))
    hard_no_action = bool(matched_patterns(prompt, policy.no_action_patterns))
    if meta and (hard_no_action or not explicit_action):
        return "meta"
    if hard_no_action:
        return "answer-only"
    if explicit_action:
        return "action"
    if answer_only:
        return "answer-only"
    return "action"


def skill_states(
    match: RouteMatch,
    disposition: str,
) -> tuple[tuple[SkillActivationIR, ...], tuple[SkillActivationIR, ...]]:
    if disposition == "abstain":
        return (), ()
    primary = SkillActivationIR(
        "primary",
        match.route.primary,
        "activated" if disposition == "activate" else "deferred",
    )
    supporting = tuple(SkillActivationIR("supporting", skill, "deferred") for skill in match.route.supporting)
    verification = (
        (SkillActivationIR("verification", match.route.verification, "deferred"),) if match.route.verification else ()
    )
    if disposition == "activate":
        return (primary,), (*supporting, *verification)
    return (), (primary, *supporting, *verification)


def decide_activation(
    prompt: str,
    matches: tuple[RouteMatch, ...],
    config: dict[str, Any],
    *,
    answer_only: bool,
) -> ActivationIR:
    if len(prompt) > MAX_ROUTABLE_PROMPT_CHARS:
        return ActivationIR(
            "abstain",
            "prompt_too_long",
            None,
            None,
            0.0,
            0.0,
            (),
            DEFAULT_ACTIVATION_SCOPE,
            IntentFrame("input-rejected", (), (), False, False),
            (),
            (),
        )
    policy = activation_policy(config)
    if not matches:
        frame = IntentFrame(request_mode(prompt, answer_only, policy), (), (), False, False)
        return ActivationIR(
            "abstain",
            "no_candidate",
            None,
            None,
            0.0,
            0.0,
            (),
            DEFAULT_ACTIVATION_SCOPE,
            frame,
            (),
            (),
        )

    match = matches[0]
    facets = matched_facets(match)
    missing_facets = tuple(facet for facet in match.route.activation.required_facets if facet not in facets)
    ambiguous = score_is_ambiguous(matches, policy.min_score_margin)
    mode = request_mode(prompt, answer_only, policy)
    frame = IntentFrame(mode, facets, missing_facets, ambiguous, match.route.fallback)

    if mode == "meta":
        disposition, reason_code = "abstain", "meta_context"
    elif mode == "answer-only":
        disposition, reason_code = "propose", "answer_only"
    elif missing_facets:
        disposition, reason_code = "propose", "missing_required_facets"
    elif ambiguous:
        disposition, reason_code = "propose", "ambiguous_candidates"
    elif match.route.fallback:
        disposition, reason_code = "propose", "fallback_candidate"
    elif match.route.activation.mode == "propose-only":
        disposition, reason_code = "propose", "route_propose_only"
    elif match.confidence < policy.auto_activate_min_strength:
        disposition, reason_code = "propose", "weak_evidence"
    else:
        disposition, reason_code = "activate", "eligible"

    activated, deferred = skill_states(match, disposition)
    return ActivationIR(
        disposition,
        reason_code,
        match.route.name,
        match.route.intent,
        match.confidence,
        match.score,
        match.matched_pattern_ids,
        match.route.activation.scope,
        frame,
        activated,
        deferred,
    )


def skill_activation_dict(skill: SkillActivationIR) -> dict[str, str]:
    return {
        "role": skill.role,
        "configuredName": skill.configured_name,
        "state": skill.state,
    }


def activation_ir_dict(activation: ActivationIR) -> dict[str, Any]:
    return {
        "schema": ACTIVATION_IR_SCHEMA,
        "disposition": activation.disposition,
        "reasonCode": activation.reason_code,
        "routeId": activation.route_id,
        "intentId": activation.intent_id,
        "matchStrength": round(activation.confidence, 2),
        "score": round(activation.score, 2),
        "evidenceIds": list(activation.evidence_ids),
        "scope": activation.scope,
        "requiresAgentAcceptance": activation.should_propose,
        "intentFrame": {
            "requestMode": activation.intent_frame.request_mode,
            "matchedFacets": list(activation.intent_frame.matched_facets),
            "missingRequiredFacets": list(activation.intent_frame.missing_required_facets),
            "ambiguous": activation.intent_frame.ambiguous,
            "fallback": activation.intent_frame.fallback,
        },
        "activatedSkills": [skill_activation_dict(skill) for skill in activation.activated_skills],
        "deferredSkills": [skill_activation_dict(skill) for skill in activation.deferred_skills],
    }
