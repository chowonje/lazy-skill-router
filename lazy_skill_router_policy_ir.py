from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Protocol

from lazy_skill_router_scoring import CapabilityRequirements, Route, RouteActivation, RoutePattern

SUPPORTED_POLICY_SCHEMAS = frozenset({1, 2})
ROUTE_LIFECYCLE_STATES = frozenset({"active", "disabled", "shadow"})
BASE_PATTERN_ID_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f<>]+$")
FACET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ACTIVATION_SCOPES = frozenset({"turn", "phase", "task"})
ROUTE_ACTIVATION_MODES = frozenset({"auto", "propose-only"})
MAX_ACTIVATION_PATTERN_LENGTH = 300
# Deliberately duplicated instead of importing the runtime defaults: changing a
# trusted exception must also cross this validation boundary in review.
SHIPPED_ACTIVATION_PATTERN_BUNDLES = {
    "activation.metaPatterns": (
        r"(skill|스킬|route|라우트|router|라우터|hook|훅).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치).*(why|problem|wrong|explain|왜|문제|잘못|설명)",
        r"(skill|스킬|route|라우트|router|라우터|hook|훅).*(why|problem|wrong|explain|왜|문제|잘못|설명).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치)",
        r"(why|problem|wrong|explain|왜|문제|잘못|설명).*(skill|스킬|route|라우트|router|라우터|hook|훅).*(select|recommend|use|activat|match|선택|추천|사용|활성|매치)",
        r"(select|recommend|use|activat|match|선택|추천|사용|활성|매치).*(skill|스킬|route|라우트|router|라우터|hook|훅).*(why|problem|wrong|explain|왜|문제|잘못|설명)",
    ),
    "activation.actionPatterns": (
        r"\b(fix|implement|update|change|add|create|install|remove|delete)\b",
        r"(수정|구현|추가|생성|변경|설치|삭제|업데이트)(해|하고|해서|하자|하라|해라)",
        r"(고치|만들)(고|거나|면)|고쳐|만들어",
    ),
    "activation.noActionPatterns": (
        r"(?:don't|do\s+not)\s+(?:change|edit|modify|fix|install|remove|delete)",
        r"\bno\s+(?:edits?|changes?)\b",
        r"\b(?:explain|describe)\b[^.!?\n]{0,160}\bhow(?:\s+(?:i|we|you|one|someone)\s+(?:should|could|can|would))?\s+(?:to\s+)?(?:fix|implement|update|change|add|create|install|remove|delete)\b",
        r"(수정|구현|추가|생성|변경|설치|삭제|업데이트)하지\s*마",
        r"(고치|만들)지\s*마",
        r"(?:수정|구현|추가|생성|변경|설치|삭제|업데이트|고치|만들)(?:하는|할)?\s*방법(?:만)?\s*(?:을|를)?\s*설명",
    ),
}
ACTIVATION_REGEX_ERRORS = (re.error, RecursionError, OverflowError)
ACTIVATION_NESTED_QUANTIFIER_PATTERN = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
ACTIVATION_QUANTIFIED_ALTERNATION_PATTERN = re.compile(r"\([^)]*\|[^)]*\)[+*{]")
ACTIVATION_BACKREFERENCE_PATTERN = re.compile(r"\\[1-9]")
ACTIVATION_LOOKAROUND_TOKENS = ("(?=", "(?!", "(?<=", "(?<!")
ACTIVATION_CHARACTER_CLASS_PATTERN = re.compile(r"\[(?:\\.|[^\]])*\]")
ACTIVATION_ESCAPED_TOKEN_PATTERN = re.compile(r"\\.")
ACTIVATION_UNSUPPORTED_QUANTIFIER_PATTERN = re.compile(r"[*+?{}]")


class InventoryResolver(Protocol):
    skills: tuple[dict[str, Any], ...]

    def resolve(self, configured_name: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class PolicyFinding:
    severity: str
    code: str
    message: str
    route_id: str | None = None
    field: str | None = None


@dataclass(frozen=True)
class SkillRef:
    configured_name: str
    canonical_id: str | None = None
    capability: str | None = None


@dataclass(frozen=True)
class ActivationRuleIR:
    required_facets: tuple[str, ...] = ()
    scope: str = "turn"
    mode: str = "auto"


@dataclass(frozen=True)
class PatternIR:
    pattern_id: str
    regex: str
    diagnostic_label: str
    weight: float = 1.0
    facet: str = "signal"


@dataclass(frozen=True)
class RouteIR:
    route_id: str
    intent_id: str
    primary: tuple[SkillRef, ...]
    supporting: tuple[SkillRef, ...]
    verification: tuple[SkillRef, ...]
    patterns: tuple[PatternIR, ...]
    exclude_patterns: tuple[PatternIR, ...]
    priority: float
    weight: float
    fallback: bool
    lifecycle_state: str
    proposal_revision: str | None
    reason: str
    capability_requirements: CapabilityRequirements
    activation: ActivationRuleIR = ActivationRuleIR()


@dataclass(frozen=True)
class PolicyIR:
    schema_version: int
    policy_version: str | None
    allowed_skills: tuple[str, ...]
    default_verification: SkillRef | None
    fallback_route_id: str | None
    routes: tuple[RouteIR, ...]


@dataclass(frozen=True)
class PolicyParseResult:
    policy: PolicyIR
    findings: tuple[PolicyFinding, ...]

    @property
    def valid(self) -> bool:
        return not any(finding.severity == "ERROR" for finding in self.findings)


@dataclass(frozen=True)
class PolicyReference:
    route_id: str
    field: str
    skill: SkillRef
    lifecycle_state: str


@dataclass(frozen=True)
class PolicyReferenceResolution:
    route_id: str
    field: str
    lifecycle_state: str
    configured_name: str
    requested_canonical_id: str | None
    resolved_canonical_id: str | None
    status: str


@dataclass(frozen=True)
class ResolvedPolicy:
    policy: PolicyIR
    findings: tuple[PolicyFinding, ...]
    references: tuple[PolicyReferenceResolution, ...]

    @property
    def valid(self) -> bool:
        return not any(finding.severity == "ERROR" for finding in self.findings)


def strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    return ()


def number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def activation_pattern_risk(pattern: str, compiled: re.Pattern[str] | None = None) -> str | None:
    if len(pattern) > MAX_ACTIVATION_PATTERN_LENGTH:
        return f"pattern exceeds {MAX_ACTIVATION_PATTERN_LENGTH} characters"
    if any(ord(character) < 32 or ord(character) == 127 for character in pattern):
        return "pattern contains a control character"
    if ACTIVATION_NESTED_QUANTIFIER_PATTERN.search(pattern):
        return "nested repetition is unsupported"
    if ACTIVATION_QUANTIFIED_ALTERNATION_PATTERN.search(pattern):
        return "repeated alternation is unsupported"
    if ACTIVATION_BACKREFERENCE_PATTERN.search(pattern):
        return "backreferences are unsupported"
    if any(token in pattern for token in ACTIVATION_LOOKAROUND_TOKENS):
        return "lookaround is unsupported"
    quantifier_probe = pattern.replace("(?:", "(")
    quantifier_probe = ACTIVATION_CHARACTER_CLASS_PATTERN.sub("", quantifier_probe)
    quantifier_probe = ACTIVATION_ESCAPED_TOKEN_PATTERN.sub("", quantifier_probe)
    if ACTIVATION_UNSUPPORTED_QUANTIFIER_PATTERN.search(quantifier_probe):
        return "pattern contains an unsupported quantifier"
    if compiled is not None:
        try:
            matches_empty = compiled.search("") is not None
        except ACTIVATION_REGEX_ERRORS:
            return "pattern evaluation failed"
        if matches_empty:
            return "pattern must not match an empty string"
    return None


def validate_activation_patterns(
    patterns: tuple[str, ...],
    field: str,
    finding_prefix: str,
    findings: list[PolicyFinding],
) -> None:
    trusted_bundle = patterns == SHIPPED_ACTIVATION_PATTERN_BUNDLES.get(field)
    for pattern in patterns:
        if not trusted_bundle:
            risk = activation_pattern_risk(pattern)
            if risk is not None:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        f"{finding_prefix}_regex_unsafe",
                        f"{field} has unsafe regex {pattern!r}: {risk}",
                    )
                )
                continue
        try:
            compiled = re.compile(pattern)
        except ACTIVATION_REGEX_ERRORS as exc:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"{finding_prefix}_regex_invalid",
                    f"{field} has invalid regex {pattern!r}: {exc}",
                )
            )
            continue
        risk = None if trusted_bundle else activation_pattern_risk(pattern, compiled)
        if risk is not None:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"{finding_prefix}_regex_unsafe",
                    f"{field} has unsafe regex {pattern!r}: {risk}",
                )
            )


def validate_common_config(config: dict[str, Any], schema_version: int, findings: list[PolicyFinding]) -> None:
    display = config.get("display", {})
    if display and not isinstance(display, dict):
        findings.append(PolicyFinding("ERROR", "display_invalid", "display must be an object when present"))
    elif isinstance(display, dict):
        show_notice = display.get("showRouterNotice")
        if show_notice is not None and not isinstance(show_notice, bool):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "display_notice_invalid",
                    "display.showRouterNotice must be a boolean when set",
                )
            )

    activation = config.get("activation")
    if activation is not None and not isinstance(activation, dict):
        findings.append(PolicyFinding("ERROR", "activation_invalid", "activation must be an object when present"))
    elif isinstance(activation, dict):
        activation_mode = activation.get("mode")
        if not isinstance(activation_mode, str) or activation_mode not in {"inject", "off", "shadow"}:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "activation_mode_invalid",
                    "activation.mode must be one of: inject, off, shadow",
                )
            )
        auto_strength = activation.get("autoActivateMinStrength")
        if auto_strength is not None and (not is_number(auto_strength) or not 0 <= float(auto_strength) <= 1):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "activation_auto_strength_invalid",
                    "activation.autoActivateMinStrength must be a number between 0 and 1",
                )
            )
        meta_value = activation.get("metaPatterns")
        meta_patterns = strings(meta_value)
        if meta_value is not None and not meta_patterns:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "activation_meta_patterns_invalid",
                    "activation.metaPatterns must contain strings when present",
                )
            )
        validate_activation_patterns(
            meta_patterns,
            "activation.metaPatterns",
            "activation_meta_pattern",
            findings,
        )
        action_value = activation.get("actionPatterns")
        action_patterns = strings(action_value)
        if action_value is not None and not action_patterns:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "activation_action_patterns_invalid",
                    "activation.actionPatterns must contain strings when present",
                )
            )
        validate_activation_patterns(
            action_patterns,
            "activation.actionPatterns",
            "activation_action_pattern",
            findings,
        )
        no_action_value = activation.get("noActionPatterns")
        no_action_patterns = strings(no_action_value)
        if no_action_value is not None and not no_action_patterns:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "activation_no_action_patterns_invalid",
                    "activation.noActionPatterns must contain strings when present",
                )
            )
        validate_activation_patterns(
            no_action_patterns,
            "activation.noActionPatterns",
            "activation_no_action_pattern",
            findings,
        )

    logging_config = config.get("logging", {})
    if logging_config and not isinstance(logging_config, dict):
        findings.append(PolicyFinding("ERROR", "logging_invalid", "logging must be an object when present"))
    elif isinstance(logging_config, dict):
        enabled = logging_config.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            findings.append(
                PolicyFinding("ERROR", "logging_enabled_invalid", "logging.enabled must be a boolean when set")
            )
        path = logging_config.get("path")
        if path is not None and not isinstance(path, str):
            findings.append(PolicyFinding("ERROR", "logging_path_invalid", "logging.path must be a string when set"))
        for field in ("maxEntries", "retentionDays"):
            value = logging_config.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        f"logging_{field}_invalid",
                        f"logging.{field} must be a positive integer when set",
                    )
                )

    if schema_version == 1:
        min_confidence = config.get("minConfidence", 0.55)
        if not is_number(min_confidence) or not 0 <= float(min_confidence) <= 1:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "min_confidence_invalid",
                    "minConfidence must be a number between 0 and 1",
                )
            )
        answer_only = config.get("answerOnlyPatterns")
        answer_patterns = strings(answer_only)
        if answer_only is not None and not answer_patterns:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "answer_only_patterns_invalid",
                    "answerOnlyPatterns must contain strings when present",
                )
            )
        for pattern in answer_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "answer_only_pattern_regex_invalid",
                        f"route answerOnlyPatterns has invalid answerOnlyPatterns regex {pattern!r}: {exc}",
                    )
                )
    elif schema_version == 2:
        selection = config.get("selection")
        if not isinstance(selection, dict):
            findings.append(PolicyFinding("ERROR", "selection_invalid", "schema v2 selection must be an object"))
        else:
            if selection.get("mode") != "ranked":
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "selection_mode_invalid",
                        "schema v2 selection.mode must be ranked",
                    )
                )
            max_recommendations = selection.get("maxRecommendations")
            if (
                isinstance(max_recommendations, bool)
                or not isinstance(max_recommendations, int)
                or not 1 <= max_recommendations <= 3
            ):
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "selection_max_recommendations_invalid",
                        "schema v2 selection.maxRecommendations must be an integer from 1 to 3",
                    )
                )
            for field in ("minMatchStrength", "minScoreMargin"):
                value = selection.get(field)
                if not is_number(value) or not 0 <= float(value) <= 1:
                    findings.append(
                        PolicyFinding(
                            "ERROR",
                            f"selection_{field}_invalid",
                            f"schema v2 selection.{field} must be a number between 0 and 1",
                        )
                    )
        policy_version = config.get("policyVersion")
        if not isinstance(policy_version, str) or not policy_version:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_version_invalid",
                    "schema v2 policyVersion must be a non-empty string",
                )
            )


def validate_route_scalars(raw_route: dict[str, Any], route_id: str, findings: list[PolicyFinding]) -> None:
    for field in ("priority", "weight"):
        value = raw_route.get(field)
        if value is not None and not is_number(value):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"route_{field}_invalid",
                    f"route {route_id} {field} must be a number when set",
                    route_id,
                    field,
                )
            )
    fallback = raw_route.get("fallback")
    if fallback is not None and not isinstance(fallback, bool):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_fallback_invalid",
                f"route {route_id} fallback must be a boolean when set",
                route_id,
                "fallback",
            )
        )


def stable_pattern_id(route_id: str, regex: str) -> str:
    route_segment = re.sub(r"[^A-Za-z0-9._-]+", "-", route_id).strip("-") or "route"
    digest = hashlib.sha256(f"{route_id}\0{regex}".encode()).hexdigest()[:12]
    return f"{route_segment}.{digest}"


def lifecycle(raw_route: dict[str, Any], route_id: str, findings: list[PolicyFinding]) -> tuple[str, str | None]:
    value = raw_route.get("lifecycle")
    if value is None:
        return "active", None
    if not isinstance(value, dict):
        findings.append(
            PolicyFinding("ERROR", "route_lifecycle_invalid", f"route {route_id} lifecycle must be an object", route_id)
        )
        return "disabled", None
    state = value.get("state", "active")
    if state not in ROUTE_LIFECYCLE_STATES:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_lifecycle_state_invalid",
                f"route {route_id} lifecycle.state must be active, disabled, or shadow",
                route_id,
            )
        )
        state = "disabled"
    revision = value.get("proposalRevision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_proposal_revision_invalid",
                f"route {route_id} lifecycle.proposalRevision must be a non-empty string",
                route_id,
            )
        )
        revision = None
    previous_state = value.get("previousState")
    if previous_state is not None and previous_state not in {"active", "shadow"}:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_previous_state_invalid",
                f"route {route_id} lifecycle.previousState must be active or shadow",
                route_id,
                "lifecycle.previousState",
            )
        )
    retired_by = value.get("retiredByProposal")
    if retired_by is not None and (not isinstance(retired_by, str) or not retired_by):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_retired_by_invalid",
                f"route {route_id} lifecycle.retiredByProposal must be a non-empty string",
                route_id,
                "lifecycle.retiredByProposal",
            )
        )
    retirement_reason = value.get("retirementReason")
    if retirement_reason is not None and (not isinstance(retirement_reason, str) or not retirement_reason):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_retirement_reason_invalid",
                f"route {route_id} lifecycle.retirementReason must be a non-empty string",
                route_id,
                "lifecycle.retirementReason",
            )
        )
    if state == "disabled" and retired_by is not None and retirement_reason is None:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_retirement_reason_missing",
                f"route {route_id} retired lifecycle requires retirementReason",
                route_id,
                "lifecycle.retirementReason",
            )
        )
    return str(state), revision if isinstance(revision, str) else None


def binding_ref(value: Any, capability: str, findings: list[PolicyFinding]) -> SkillRef | None:
    if isinstance(value, str) and value:
        return SkillRef(value, capability=capability)
    if not isinstance(value, dict):
        return None
    unknown = set(value) - {"skill", "canonicalId"}
    if unknown:
        findings.append(
            PolicyFinding(
                "ERROR",
                "skill_binding_fields_unsupported",
                "skill binding contains unsupported fields: " + ", ".join(sorted(unknown)),
                field=f"skillBindings.{capability}",
            )
        )
    configured_name = value.get("skill")
    canonical_id = value.get("canonicalId")
    if not isinstance(configured_name, str) or not configured_name:
        return None
    if canonical_id is not None and (not isinstance(canonical_id, str) or not canonical_id):
        findings.append(
            PolicyFinding(
                "ERROR",
                "skill_binding_canonical_id_invalid",
                f"skill binding {capability} canonicalId must be a non-empty string",
                field=f"skillBindings.{capability}",
            )
        )
        canonical_id = None
    return SkillRef(configured_name, canonical_id if isinstance(canonical_id, str) else None, capability)


def pattern_ir(
    value: Any,
    route_id: str,
    findings: list[PolicyFinding],
    field: str,
    *,
    allow_string: bool = True,
    require_id: bool = False,
) -> PatternIR | None:
    if isinstance(value, str):
        if not allow_string:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_invalid",
                    f"route {route_id} {field} entries must be pattern objects",
                    route_id,
                    field,
                )
            )
            return None
        regex = value
        pattern_id = stable_pattern_id(route_id, regex)
        label = regex
        weight = 1.0
        facet = "signal"
    elif isinstance(value, dict):
        regex = value.get("regex")
        configured_id = value.get("id", value.get("pattern_id"))
        label_value = value.get("label")
        weight_value = value.get("weight", 1.0)
        facet_value = value.get("facet", "signal")
        if not isinstance(regex, str) or not regex:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_regex_invalid",
                    f"route {route_id} {field} pattern object missing string regex",
                    route_id,
                    field,
                )
            )
            return None
        if require_id and (not isinstance(configured_id, str) or not configured_id):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_id_missing",
                    f"route {route_id} {field} pattern missing string id",
                    route_id,
                    field,
                )
            )
        pattern_id = (
            configured_id if isinstance(configured_id, str) and configured_id else stable_pattern_id(route_id, regex)
        )
        if not BASE_PATTERN_ID_PATTERN.fullmatch(pattern_id):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_id_invalid",
                    f"route {route_id} pattern id contains unsupported characters: {pattern_id}",
                    route_id,
                    field,
                )
            )
            pattern_id = stable_pattern_id(route_id, regex)
        label = label_value if isinstance(label_value, str) and label_value else regex
        if label_value is not None and (not isinstance(label_value, str) or not label_value):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_label_invalid",
                    f"route {route_id} {field} pattern object label must be a non-empty string",
                    route_id,
                    field,
                )
            )
        if isinstance(weight_value, bool) or not isinstance(weight_value, (int, float)) or weight_value <= 0:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_weight_invalid",
                    f"route {route_id} pattern weight must be a positive number",
                    route_id,
                    field,
                )
            )
            weight = 1.0
        else:
            weight = float(weight_value)
        if not isinstance(facet_value, str) or not FACET_ID_PATTERN.fullmatch(facet_value):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_facet_invalid",
                    f"route {route_id} {field} pattern facet contains unsupported characters: {facet_value}",
                    route_id,
                    field,
                )
            )
            facet = "signal"
        else:
            facet = facet_value
    else:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_pattern_invalid",
                f"route {route_id} {field} entries must be strings or pattern objects",
                route_id,
                field,
            )
        )
        return None
    try:
        re.compile(regex)
    except re.error as exc:
        message = (
            f"route {route_id} has invalid regex {regex!r}: {exc}"
            if field.startswith("match.")
            else (f"route {route_id} has invalid regex {regex!r} for {field} (invalid {field} regex): {exc}")
        )
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_pattern_regex_invalid",
                message,
                route_id,
                field,
            )
        )
        return None
    return PatternIR(pattern_id, regex, label, weight, facet)


def pattern_list(
    value: Any,
    route_id: str,
    findings: list[PolicyFinding],
    field: str,
    *,
    allow_string: bool = True,
    require_id: bool = False,
) -> tuple[PatternIR, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    return tuple(
        pattern
        for item in values
        if (
            pattern := pattern_ir(
                item,
                route_id,
                findings,
                field,
                allow_string=allow_string,
                require_id=require_id,
            )
        )
        is not None
    )


def route_activation_rule(
    raw_route: dict[str, Any],
    route_id: str,
    patterns: tuple[PatternIR, ...],
    findings: list[PolicyFinding],
) -> ActivationRuleIR:
    value = raw_route.get("activation")
    if value is None:
        return ActivationRuleIR()
    if not isinstance(value, dict):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_invalid",
                f"route {route_id} activation must be an object when present",
                route_id,
                "activation",
            )
        )
        return ActivationRuleIR()
    unknown = set(value) - {"requiredFacets", "scope", "mode"}
    if unknown:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_fields_invalid",
                f"route {route_id} activation contains unsupported fields: {', '.join(sorted(unknown))}",
                route_id,
                "activation",
            )
        )
    required_value = value.get("requiredFacets", [])
    required = strings(required_value)
    if not isinstance(required_value, list) or any(not FACET_ID_PATTERN.fullmatch(facet) for facet in required):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_facets_invalid",
                f"route {route_id} activation.requiredFacets must contain safe identifiers",
                route_id,
                "activation.requiredFacets",
            )
        )
        required = ()
    required = tuple(dict.fromkeys(required))
    available_facets = {pattern.facet for pattern in patterns}
    unavailable = tuple(facet for facet in required if facet not in available_facets)
    if unavailable:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_facets_unbound",
                f"route {route_id} activation.requiredFacets are not present in route patterns: "
                f"{', '.join(unavailable)}",
                route_id,
                "activation.requiredFacets",
            )
        )
    scope = value.get("scope", "turn")
    if not isinstance(scope, str) or scope not in ACTIVATION_SCOPES:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_scope_invalid",
                f"route {route_id} activation.scope must be one of: phase, task, turn",
                route_id,
                "activation.scope",
            )
        )
        scope = "turn"
    mode = value.get("mode", "auto")
    if not isinstance(mode, str) or mode not in ROUTE_ACTIVATION_MODES:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_activation_mode_invalid",
                f"route {route_id} activation.mode must be one of: auto, propose-only",
                route_id,
                "activation.mode",
            )
        )
        mode = "auto"
    return ActivationRuleIR(required, scope, mode)


def parse_v1(config: dict[str, Any], findings: list[PolicyFinding]) -> tuple[RouteIR, ...]:
    routes_value = config.get("routes")
    if not isinstance(routes_value, list) or not routes_value:
        findings.append(PolicyFinding("ERROR", "routes_invalid", "routes must be a non-empty list"))
        return ()
    routes: list[RouteIR] = []
    seen: set[str] = set()
    for index, raw_route in enumerate(routes_value):
        if not isinstance(raw_route, dict):
            findings.append(PolicyFinding("ERROR", "route_invalid", f"route #{index} must be an object"))
            continue
        route_id = raw_route.get("name")
        primary_name = raw_route.get("primary")
        if not isinstance(route_id, str) or not route_id:
            findings.append(PolicyFinding("ERROR", "route_id_invalid", f"route #{index} missing string name"))
            continue
        validate_route_scalars(raw_route, route_id, findings)
        if route_id in seen:
            findings.append(PolicyFinding("ERROR", "route_id_duplicate", f"duplicate route name: {route_id}", route_id))
            continue
        seen.add(route_id)
        if not isinstance(primary_name, str) or not primary_name:
            findings.append(
                PolicyFinding("ERROR", "route_primary_missing", f"route {route_id} missing string primary", route_id)
            )
            continue
        patterns = pattern_list(raw_route.get("patterns"), route_id, findings, "patterns")
        if not patterns:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_patterns_missing",
                    f"route {route_id} must define non-empty patterns",
                    route_id,
                )
            )
            continue
        supporting_value = raw_route.get("supporting")
        supporting_names = strings(supporting_value)
        if supporting_value and not supporting_names:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_supporting_invalid",
                    f"route {route_id} supporting must contain strings",
                    route_id,
                    "supporting",
                )
            )
        verification_name = raw_route.get("verification", "")
        if verification_name and not isinstance(verification_name, str):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_verification_invalid",
                    f"route {route_id} verification must be a string when set",
                    route_id,
                )
            )
            verification_name = ""
        state, proposal_revision = lifecycle(raw_route, route_id, findings)
        intent = raw_route.get("intent", route_id)
        intent_id = intent if isinstance(intent, str) and intent else route_id
        reason_value = raw_route.get("reason", "")
        if not isinstance(reason_value, str):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_reason_invalid",
                    f"route {route_id} reason must be a string when set",
                    route_id,
                    "reason",
                )
            )
            reason_value = ""
        primary = (SkillRef(primary_name, capability=f"skill:{primary_name}"),)
        supporting = tuple(SkillRef(name, capability=f"skill:{name}") for name in supporting_names)
        verification = (
            (SkillRef(verification_name, capability=f"skill:{verification_name}"),)
            if isinstance(verification_name, str) and verification_name
            else ()
        )
        routes.append(
            RouteIR(
                route_id,
                intent_id,
                primary,
                supporting,
                verification,
                patterns,
                pattern_list(raw_route.get("excludePatterns"), route_id, findings, "excludePatterns"),
                number(raw_route.get("priority")),
                number(raw_route.get("weight")),
                raw_route.get("fallback") is True,
                state,
                proposal_revision,
                reason_value,
                CapabilityRequirements(
                    tuple(ref.capability or "" for ref in primary),
                    tuple(ref.capability or "" for ref in supporting),
                    tuple(ref.capability or "" for ref in verification),
                ),
                route_activation_rule(raw_route, route_id, patterns, findings),
            )
        )
    return tuple(routes)


def parse_v2(config: dict[str, Any], findings: list[PolicyFinding]) -> tuple[RouteIR, ...]:
    bindings_value = config.get("skillBindings")
    if not isinstance(bindings_value, dict):
        findings.append(PolicyFinding("ERROR", "skill_bindings_invalid", "schema v2 skillBindings must be an object"))
        bindings_value = {}
    bindings: dict[str, SkillRef] = {}
    for capability, value in bindings_value.items():
        if not isinstance(capability, str) or not capability:
            findings.append(
                PolicyFinding("ERROR", "skill_binding_capability_invalid", "skill binding capability must be a string")
            )
            continue
        ref = binding_ref(value, capability, findings)
        if ref is None:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "skill_binding_invalid",
                    f"schema v2 skill binding {capability} must reference a skill",
                    field=f"skillBindings.{capability}",
                )
            )
            continue
        bindings[capability] = ref

    routes_value = config.get("routes")
    if not isinstance(routes_value, list) or not routes_value:
        findings.append(PolicyFinding("ERROR", "routes_invalid", "schema v2 routes must be a non-empty list"))
        return ()
    fallback_route_id = config.get("fallbackRouteId")
    if fallback_route_id is not None and (not isinstance(fallback_route_id, str) or not fallback_route_id):
        findings.append(
            PolicyFinding(
                "ERROR",
                "fallback_route_id_invalid",
                "schema v2 fallbackRouteId must be a non-empty string or null",
            )
        )
    routes: list[RouteIR] = []
    seen: set[str] = set()
    for index, raw_route in enumerate(routes_value):
        if not isinstance(raw_route, dict):
            findings.append(PolicyFinding("ERROR", "route_invalid", f"schema v2 route #{index} must be an object"))
            continue
        route_id = raw_route.get("id")
        intent = raw_route.get("intent")
        if not isinstance(route_id, str) or not route_id:
            findings.append(PolicyFinding("ERROR", "route_id_invalid", f"schema v2 route #{index} missing string id"))
            continue
        validate_route_scalars(raw_route, route_id, findings)
        if route_id in seen:
            findings.append(PolicyFinding("ERROR", "route_id_duplicate", f"duplicate route id: {route_id}", route_id))
            continue
        seen.add(route_id)
        if not isinstance(intent, str) or not intent:
            findings.append(
                PolicyFinding("ERROR", "route_intent_invalid", f"route {route_id} missing string intent", route_id)
            )
            continue
        requirements_value = raw_route.get("capabilityRequirements")
        if not isinstance(requirements_value, dict):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_capabilities_invalid",
                    f"route {route_id} capabilityRequirements must be an object",
                    route_id,
                )
            )
            requirements_value = {}
        role_capabilities: dict[str, tuple[str, ...]] = {}
        for role in ("primary", "supporting", "verification"):
            raw_capabilities = requirements_value.get(role)
            capabilities = strings(raw_capabilities)
            if raw_capabilities and not capabilities:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "route_capabilities_invalid",
                        f"route {route_id} capabilityRequirements.{role} must be strings",
                        route_id,
                        f"capabilityRequirements.{role}",
                    )
                )
            role_capabilities[role] = capabilities
        primary_capabilities = role_capabilities["primary"]
        supporting_capabilities = role_capabilities["supporting"]
        verification_capabilities = role_capabilities["verification"]
        if not primary_capabilities:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_primary_capability_missing",
                    f"route {route_id} must require at least one primary capability",
                    route_id,
                )
            )
        role_refs: dict[str, tuple[SkillRef, ...]] = {}
        for role, capabilities in (
            ("primary", primary_capabilities),
            ("supporting", supporting_capabilities),
            ("verification", verification_capabilities),
        ):
            refs: list[SkillRef] = []
            for capability in capabilities:
                ref = bindings.get(capability)
                if ref is None:
                    findings.append(
                        PolicyFinding(
                            "ERROR",
                            "route_skill_binding_missing",
                            f"route {route_id} missing skill binding for {capability}",
                            route_id,
                            f"capabilityRequirements.{role}",
                        )
                    )
                    continue
                refs.append(ref)
            role_refs[role] = tuple(refs)
        fallback = fallback_route_id == route_id or raw_route.get("fallback") is True
        match = raw_route.get("match", {})
        if not isinstance(match, dict):
            findings.append(
                PolicyFinding("ERROR", "route_match_invalid", f"route {route_id} match must be an object", route_id)
            )
            match = {}
        any_patterns = match.get("any", [])
        none_patterns = match.get("none", [])
        if not isinstance(any_patterns, list):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_match_any_invalid",
                    f"route {route_id} match.any must be a list",
                    route_id,
                    "match.any",
                )
            )
            any_patterns = []
        if not isinstance(none_patterns, list):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_match_none_invalid",
                    f"route {route_id} match.none must be a list",
                    route_id,
                    "match.none",
                )
            )
            none_patterns = []
        seen_pattern_ids: set[str] = set()
        for raw_pattern in (*any_patterns, *none_patterns):
            if not isinstance(raw_pattern, dict):
                continue
            pattern_id = raw_pattern.get("id", raw_pattern.get("pattern_id"))
            if not isinstance(pattern_id, str) or not pattern_id:
                continue
            if pattern_id in seen_pattern_ids:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "route_pattern_id_duplicate",
                        f"duplicate pattern id: {pattern_id}",
                        route_id,
                    )
                )
            else:
                seen_pattern_ids.add(pattern_id)
        patterns = pattern_list(
            any_patterns,
            route_id,
            findings,
            "match.any",
            allow_string=False,
            require_id=True,
        )
        if not patterns and not fallback:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_patterns_missing",
                    f"route {route_id} match.any must not be empty",
                    route_id,
                )
            )
            continue
        state, proposal_revision = lifecycle(raw_route, route_id, findings)
        reason_value = raw_route.get("reason", "")
        if not isinstance(reason_value, str):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_reason_invalid",
                    f"route {route_id} reason must be a string when set",
                    route_id,
                    "reason",
                )
            )
            reason_value = ""
        routes.append(
            RouteIR(
                route_id,
                intent,
                role_refs.get("primary", ()),
                role_refs.get("supporting", ()),
                role_refs.get("verification", ()),
                patterns,
                pattern_list(none_patterns, route_id, findings, "match.none", require_id=True),
                number(raw_route.get("priority")),
                number(raw_route.get("weight")),
                fallback,
                state,
                proposal_revision,
                reason_value,
                CapabilityRequirements(primary_capabilities, supporting_capabilities, verification_capabilities),
                route_activation_rule(raw_route, route_id, patterns, findings),
            )
        )
    if isinstance(fallback_route_id, str) and fallback_route_id not in seen:
        findings.append(
            PolicyFinding(
                "ERROR",
                "fallback_route_missing",
                f"schema v2 fallbackRouteId references missing route: {fallback_route_id}",
            )
        )
    return tuple(routes)


def parse_policy_config(config: dict[str, Any]) -> PolicyParseResult:
    findings: list[PolicyFinding] = []
    schema_value = config.get("schemaVersion", 1)
    if (
        isinstance(schema_value, bool)
        or not isinstance(schema_value, int)
        or schema_value not in SUPPORTED_POLICY_SCHEMAS
    ):
        findings.append(
            PolicyFinding("ERROR", "policy_schema_unsupported", f"unsupported schemaVersion: {schema_value}")
        )
        schema_version = 0
        routes: tuple[RouteIR, ...] = ()
    else:
        schema_version = schema_value
        validate_common_config(config, schema_version, findings)
        routes = parse_v2(config, findings) if schema_version == 2 else parse_v1(config, findings)
    default_verification_value = config.get("defaultVerification")
    if default_verification_value is not None and (
        not isinstance(default_verification_value, str) or not default_verification_value
    ):
        findings.append(
            PolicyFinding(
                "ERROR",
                "default_verification_invalid",
                "defaultVerification must be a non-empty string when set",
            )
        )
    default_verification = (
        SkillRef(default_verification_value, capability=f"skill:{default_verification_value}")
        if isinstance(default_verification_value, str) and default_verification_value
        else None
    )
    allowed_value = config.get("allowedSkills")
    if allowed_value is not None and (
        not isinstance(allowed_value, list)
        or not allowed_value
        or not all(isinstance(item, str) and item for item in allowed_value)
    ):
        findings.append(
            PolicyFinding(
                "ERROR",
                "allowed_skills_invalid",
                "allowedSkills must be a non-empty list of strings when present",
            )
        )
        allowed = ()
    else:
        allowed = tuple(dict.fromkeys(strings(allowed_value)))
    allowed_set = set(allowed)
    if allowed_set:
        for route in routes:
            for ref in route.primary:
                if ref.configured_name not in allowed_set:
                    findings.append(
                        PolicyFinding(
                            "ERROR",
                            "route_primary_not_allowed",
                            f"route {route.route_id} primary is not in allowedSkills: {ref.configured_name}",
                            route.route_id,
                            "primary",
                        )
                    )
            for role, refs in (("supporting", route.supporting), ("verification", route.verification)):
                for ref in refs:
                    if ref.configured_name not in allowed_set:
                        findings.append(
                            PolicyFinding(
                                "WARN",
                                f"route_{role}_not_allowed",
                                f"route {route.route_id} {role} skill is not in allowedSkills: {ref.configured_name}",
                                route.route_id,
                                role,
                            )
                        )
    fallback_value = config.get("fallbackRouteId")
    policy_version_value = config.get("policyVersion")
    return PolicyParseResult(
        PolicyIR(
            schema_version,
            policy_version_value if isinstance(policy_version_value, str) and policy_version_value else None,
            allowed,
            default_verification,
            fallback_value if isinstance(fallback_value, str) and fallback_value else None,
            routes,
        ),
        tuple(findings),
    )


def policy_references(
    policy: PolicyIR,
    *,
    include_disabled: bool = False,
    include_shadow: bool = False,
) -> tuple[PolicyReference, ...]:
    references: list[PolicyReference] = []
    if policy.default_verification is not None:
        references.append(PolicyReference("<default>", "defaultVerification", policy.default_verification, "active"))
    for route in policy.routes:
        if route.lifecycle_state == "disabled" and not include_disabled:
            continue
        if route.lifecycle_state == "shadow" and not include_shadow:
            continue
        for index, ref in enumerate(route.primary):
            field = "primary" if index == 0 else f"primary[{index}]"
            references.append(PolicyReference(route.route_id, field, ref, route.lifecycle_state))
        for index, ref in enumerate(route.supporting):
            field = f"supporting[{index}]" if policy.schema_version == 2 else "supporting"
            references.append(PolicyReference(route.route_id, field, ref, route.lifecycle_state))
        for index, ref in enumerate(route.verification):
            field = f"verification[{index}]" if policy.schema_version == 2 else "verification"
            references.append(PolicyReference(route.route_id, field, ref, route.lifecycle_state))
    return tuple(references)


def resolve_skill_ref(
    ref: SkillRef,
    inventory: InventoryResolver,
    route_id: str,
    field: str,
    findings: list[PolicyFinding],
) -> tuple[SkillRef, str, str | None]:
    skill = inventory.resolve(ref.configured_name)
    if skill is None:
        matches = tuple(item for item in inventory.skills if item.get("configured_name") == ref.configured_name)
        usable = tuple(
            item
            for item in matches
            if not isinstance(item.get("availability"), dict)
            or item["availability"].get("status") not in {"disabled", "inactive", "unavailable"}
        )
        if not matches:
            code = "skill_missing"
            detail = "missing"
        elif not usable:
            code = "skill_inactive"
            detail = "inactive"
        else:
            code = "skill_ambiguous"
            detail = "ambiguous"
        findings.append(
            PolicyFinding(
                "ERROR",
                code,
                f"route {route_id} {field} references {detail} skill: {ref.configured_name}",
                route_id,
                field,
            )
        )
        return ref, detail, None
    canonical_id = skill.get("canonical_id")
    if not isinstance(canonical_id, str) or not canonical_id:
        findings.append(
            PolicyFinding(
                "ERROR",
                "skill_canonical_id_missing",
                f"route {route_id} {field} skill has no canonical identity: {ref.configured_name}",
                route_id,
                field,
            )
        )
        return ref, "canonical_missing", None
    if ref.canonical_id is not None and ref.canonical_id != canonical_id:
        findings.append(
            PolicyFinding(
                "ERROR",
                "skill_canonical_id_mismatch",
                f"route {route_id} {field} canonicalId does not match {ref.configured_name}",
                route_id,
                field,
            )
        )
        return ref, "canonical_mismatch", canonical_id
    return replace(ref, canonical_id=canonical_id), "resolved", canonical_id


def resolve_policy(
    policy: PolicyIR,
    inventory: InventoryResolver,
    *,
    include_shadow: bool = False,
) -> ResolvedPolicy:
    findings: list[PolicyFinding] = []
    references: list[PolicyReferenceResolution] = []
    routes: list[RouteIR] = []

    def resolved_role(
        route_id: str,
        lifecycle_state: str,
        role: str,
        values: tuple[SkillRef, ...],
    ) -> tuple[SkillRef, ...]:
        resolved_values: list[SkillRef] = []
        for index, ref in enumerate(values):
            if role == "primary" and index == 0:
                field = role
            elif policy.schema_version == 1 and role in {"supporting", "verification"}:
                field = role
            else:
                field = f"{role}[{index}]"
            resolved_ref, status, canonical_id = resolve_skill_ref(
                ref,
                inventory,
                route_id,
                field,
                findings,
            )
            resolved_values.append(resolved_ref)
            references.append(
                PolicyReferenceResolution(
                    route_id,
                    field,
                    lifecycle_state,
                    ref.configured_name,
                    ref.canonical_id,
                    canonical_id,
                    status,
                )
            )
        return tuple(resolved_values)

    for route in policy.routes:
        if route.lifecycle_state == "disabled" or (route.lifecycle_state == "shadow" and not include_shadow):
            routes.append(route)
            continue

        primary = resolved_role(route.route_id, route.lifecycle_state, "primary", route.primary)
        supporting = resolved_role(route.route_id, route.lifecycle_state, "supporting", route.supporting)
        verification = resolved_role(route.route_id, route.lifecycle_state, "verification", route.verification)
        routes.append(replace(route, primary=primary, supporting=supporting, verification=verification))
    default_verification = policy.default_verification
    if default_verification is not None:
        requested = default_verification
        resolved_default, status, canonical_id = resolve_skill_ref(
            default_verification, inventory, "<default>", "defaultVerification", findings
        )
        default_verification = resolved_default if status == "resolved" else None
        references.append(
            PolicyReferenceResolution(
                "<default>",
                "defaultVerification",
                "active",
                requested.configured_name,
                requested.canonical_id,
                canonical_id,
                status,
            )
        )
    return ResolvedPolicy(
        replace(policy, routes=tuple(routes), default_verification=default_verification),
        tuple(findings),
        tuple(references),
    )


def select_smoke_primary(policy: PolicyIR) -> str | None:
    for route in policy.routes:
        if route.lifecycle_state == "active" and route.primary:
            return route.primary[0].configured_name
    return None


def runtime_routes(policy: PolicyIR) -> list[Route]:
    routes: list[Route] = []
    for route in policy.routes:
        primary = route.primary[0].configured_name if route.primary else ""
        supporting = tuple(
            dict.fromkeys(
                [
                    *(ref.configured_name for ref in route.primary[1:]),
                    *(ref.configured_name for ref in route.supporting),
                ]
            )
        )
        verification_ref = route.verification[0] if route.verification else policy.default_verification
        verification = verification_ref.configured_name if verification_ref is not None else ""
        routes.append(
            Route(
                route.route_id,
                primary,
                supporting,
                verification,
                route.reason,
                tuple(
                    RoutePattern(
                        pattern.regex,
                        pattern.diagnostic_label,
                        pattern.pattern_id,
                        pattern.weight,
                        pattern.facet,
                    )
                    for pattern in route.patterns
                ),
                tuple(pattern.regex for pattern in route.exclude_patterns),
                route.priority,
                route.weight,
                route.fallback,
                route.intent_id,
                route.capability_requirements,
                route.lifecycle_state,
                route.proposal_revision,
                RouteActivation(route.activation.required_facets, route.activation.scope, route.activation.mode),
            )
        )
    return routes
