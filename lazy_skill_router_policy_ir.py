from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Protocol

try:  # Python 3.9-3.14 expose the same parser under this private compatibility module.
    from re import _parser as _regex_parser
except ImportError:  # pragma: no cover - retained for older supported Python builds.
    import sre_parse as _regex_parser  # type: ignore[no-redef]

from lazy_skill_router_common import MAX_ROUTABLE_PROMPT_CHARS
from lazy_skill_router_scoring import CapabilityRequirements, Route, RouteActivation, RoutePattern

SUPPORTED_POLICY_SCHEMAS = frozenset({1, 2})
ROUTE_LIFECYCLE_STATES = frozenset({"active", "disabled", "shadow"})
BASE_PATTERN_ID_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f<>]+$")
FACET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ACTIVATION_SCOPES = frozenset({"turn", "phase", "task"})
ROUTE_ACTIVATION_MODES = frozenset({"auto", "propose-only"})
CAPABILITY_RETRIEVAL_ALGORITHMS = frozenset({"lexical-bm25-char3/v1", "lexical-bm25-char3-anchored/v2"})
MAX_ACTIVATION_PATTERN_LENGTH = 300
MAX_ROUTE_PATTERN_LENGTH = 300
MAX_PATTERNS_PER_ROUTE = 32
MAX_PATTERNS_PER_POLICY = 512
MAX_BOUNDED_REPEAT = 256
MAX_REPEAT_NODES_PER_BRANCH = 8
MAX_VARIABLE_REPEAT_PATHS_PER_BRANCH = 2 * (MAX_ROUTABLE_PROMPT_CHARS + 1)
MAX_REGEX_SEARCH_WORK_PER_PATTERN = 2_048
MAX_UNTRUSTED_REGEX_SEARCH_WORK_PER_POLICY = 768
MAX_REGEX_SEARCH_WORK_PER_POLICY = 24_576
LEADING_UNBOUNDED_REPEAT_WORK = 256
MAX_UNANCHORED_VARIABLE_REPEAT_SPAN = 64
MAX_POLICY_IDENTIFIER_CHARS = 160
MAX_POLICY_TEXT_CHARS = 160
POLICY_IDENTIFIER_PATTERN = re.compile(r"^[^\x00-\x1f\x7f<>]+$")
ROUTE_PRIORITY_MIN = -20.0
ROUTE_PRIORITY_MAX = 20.0
ROUTE_WEIGHT_MIN = -1.0
ROUTE_WEIGHT_MAX = 1.0
PATTERN_WEIGHT_MAX = 3.0
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
BOUNDED_REPEAT_PATTERN = re.compile(r"(?<!\\)\{(?P<minimum>\d+)(?:,(?P<maximum>\d*))?\}")
REGEX_ERRORS = (re.error, RecursionError, OverflowError)
_REGEX_REPEAT_OPS = frozenset(
    operation
    for operation in (
        getattr(_regex_parser, "MAX_REPEAT", None),
        getattr(_regex_parser, "MIN_REPEAT", None),
        getattr(_regex_parser, "POSSESSIVE_REPEAT", None),
    )
    if operation is not None
)
_REGEX_LOOKAROUND_OPS = frozenset(
    operation
    for operation in (
        getattr(_regex_parser, "ASSERT", None),
        getattr(_regex_parser, "ASSERT_NOT", None),
    )
    if operation is not None
)
_REGEX_BACKREFERENCE_OPS = frozenset(
    operation
    for operation in (
        getattr(_regex_parser, "GROUPREF", None),
        getattr(_regex_parser, "GROUPREF_EXISTS", None),
    )
    if operation is not None
)

# These are exact compatibility exceptions for the checked-in v1 route table.
# Route id, field, and full regex are all part of the key so the exception
# cannot be widened by a merely similar expression.
SHIPPED_ROUTE_PATTERN_EXCEPTIONS = frozenset(
    {
        ("pdf", "patterns", r"(?<![A-Za-z0-9_])pdf(?![A-Za-z0-9_])"),
        ("github-ci", "patterns", r"\baction(s)?\b.*(?:fail|실패)"),
        (
            "docs",
            "excludePatterns",
            (
                r"^(?=.*(?:(?:(?<![A-Za-z0-9_])(?:fix|implement|patch|edit|add|change|update|create|apply)"
                r"(?![A-Za-z0-9_])|(?:고치|수정|구현|추가|변경|적용))\s*"
                r"(?:(?:the|a|an|this|that|new|small|helper|python|새|새로운)\s+){0,3}"
                r"(?:(?<![A-Za-z0-9_])(?:code|bug|function|class|patch)(?![A-Za-z0-9_])|"
                r"(?:코드|버그|함수|클래스|패치))(?:을|를|은|는|이|가)?|"
                r"(?:(?<![A-Za-z0-9_])(?:code|bug|function|class|patch)(?![A-Za-z0-9_])|"
                r"(?:코드|버그|함수|클래스|패치))(?:을|를|은|는|이|가)?\s*"
                r"(?:(?<![A-Za-z0-9_])(?:fix|implement|patch|edit|add|change|update|create|apply)"
                r"(?![A-Za-z0-9_])|(?:고치|수정|구현|추가|변경|적용))))"
                r"(?=.*(readme|docs?|documentation|changelog|release notes?|문서|릴리스|체인지로그))"
            ),
        ),
        ("codex-config", "patterns", r"현재\s*작업.*평가"),
        ("codex-config", "patterns", r"작업.*문제.*같"),
    }
)


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
    if not is_number(value):
        return default
    return float(value)


def is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def policy_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_POLICY_IDENTIFIER_CHARS
        and POLICY_IDENTIFIER_PATTERN.fullmatch(value) is not None
    )


def policy_text(value: Any, *, allow_empty: bool) -> bool:
    return isinstance(value, str) and (allow_empty or bool(value)) and len(value) <= MAX_POLICY_TEXT_CHARS


def route_pattern_is_shipped_exception(route_id: str, field: str, pattern: str) -> bool:
    return (route_id, field, pattern) in SHIPPED_ROUTE_PATTERN_EXCEPTIONS


def _sequence_contains_operation(sequence: Any, operations: frozenset[Any]) -> bool:
    for operation, argument in sequence:
        if operation in operations:
            return True
        if operation in _REGEX_REPEAT_OPS and _sequence_contains_operation(argument[2], operations):
            return True
        if operation is getattr(_regex_parser, "SUBPATTERN", None) and _sequence_contains_operation(
            argument[-1], operations
        ):
            return True
        if operation is getattr(_regex_parser, "BRANCH", None):
            if any(_sequence_contains_operation(branch, operations) for branch in argument[1]):
                return True
        if operation in _REGEX_LOOKAROUND_OPS and _sequence_contains_operation(argument[1], operations):
            return True
        if operation is getattr(_regex_parser, "ATOMIC_GROUP", None) and _sequence_contains_operation(
            argument, operations
        ):
            return True
    return False


def _unbounded_repeat_count(sequence: Any) -> int:
    count = 0
    for operation, argument in sequence:
        if operation in _REGEX_REPEAT_OPS:
            if argument[1] == _regex_parser.MAXREPEAT:
                count += 1
            continue
        if operation is getattr(_regex_parser, "SUBPATTERN", None):
            count += _unbounded_repeat_count(argument[-1])
        elif operation is getattr(_regex_parser, "BRANCH", None):
            count += max((_unbounded_repeat_count(branch) for branch in argument[1]), default=0)
        elif operation is getattr(_regex_parser, "ATOMIC_GROUP", None):
            count += _unbounded_repeat_count(argument)
    return count


def _repeat_node_count(sequence: Any) -> int:
    count = 0
    for operation, argument in sequence:
        if operation in _REGEX_REPEAT_OPS:
            count += 1 + _repeat_node_count(argument[2])
        elif operation is getattr(_regex_parser, "SUBPATTERN", None):
            count += _repeat_node_count(argument[-1])
        elif operation is getattr(_regex_parser, "BRANCH", None):
            count += max((_repeat_node_count(branch) for branch in argument[1]), default=0)
        elif operation in _REGEX_LOOKAROUND_OPS:
            count += _repeat_node_count(argument[1])
        elif operation is getattr(_regex_parser, "ATOMIC_GROUP", None):
            count += _repeat_node_count(argument)
    return count


def _capped_repeat_path_product(left: int, right: int) -> int:
    if left > MAX_VARIABLE_REPEAT_PATHS_PER_BRANCH // right:
        return MAX_VARIABLE_REPEAT_PATHS_PER_BRANCH + 1
    return left * right


def _variable_repeat_paths(sequence: Any) -> int:
    paths = 1
    for operation, argument in sequence:
        operation_paths = 1
        if operation in _REGEX_REPEAT_OPS:
            minimum, maximum, nested = argument
            if minimum == maximum:
                choices = 1
            elif maximum == _regex_parser.MAXREPEAT:
                choices = MAX_ROUTABLE_PROMPT_CHARS + 1
            else:
                choices = maximum - minimum + 1
            operation_paths = _capped_repeat_path_product(choices, _variable_repeat_paths(nested))
        elif operation is getattr(_regex_parser, "SUBPATTERN", None):
            operation_paths = _variable_repeat_paths(argument[-1])
        elif operation is getattr(_regex_parser, "BRANCH", None):
            operation_paths = max((_variable_repeat_paths(branch) for branch in argument[1]), default=1)
        paths = _capped_repeat_path_product(paths, operation_paths)
    return paths


def _repeat_choice_count(argument: Any) -> int | None:
    minimum, maximum, _ = argument
    if minimum == maximum:
        return None
    if maximum == _regex_parser.MAXREPEAT:
        return MAX_ROUTABLE_PROMPT_CHARS + 1
    return maximum - minimum + 1


def _repeat_choice_paths(sequence: Any) -> list[list[int]]:
    path_limit = MAX_REGEX_SEARCH_WORK_PER_PATTERN + 1
    paths: list[list[int]] = [[]]
    for operation, argument in sequence:
        if operation in _REGEX_REPEAT_OPS:
            choices = _repeat_choice_count(argument)
            operation_paths = [[choices]] if choices is not None else [[]]
        elif operation is getattr(_regex_parser, "SUBPATTERN", None):
            operation_paths = _repeat_choice_paths(argument[-1])
        elif operation is getattr(_regex_parser, "BRANCH", None):
            operation_paths = []
            for branch in argument[1]:
                operation_paths.extend(_repeat_choice_paths(branch))
                if len(operation_paths) >= path_limit:
                    operation_paths = operation_paths[:path_limit]
                    break
        else:
            operation_paths = [[]]

        combined: list[list[int]] = []
        for prefix in paths:
            for suffix in operation_paths:
                combined.append([*prefix, *suffix])
                if len(combined) >= path_limit:
                    break
            if len(combined) >= path_limit:
                break
        paths = combined
    return paths


def _repeat_path_work(choices: list[int]) -> int:
    if len(choices) <= 1:
        return 1
    work = 1
    for value in choices:
        if work > MAX_REGEX_SEARCH_WORK_PER_PATTERN // value:
            return MAX_REGEX_SEARCH_WORK_PER_PATTERN + 1
        work *= value
    return work


def _leading_unbounded_repeat_branches(sequence: Any) -> int:
    for operation, argument in sequence:
        if operation is getattr(_regex_parser, "AT", None) or operation in _REGEX_LOOKAROUND_OPS:
            continue
        if operation in _REGEX_REPEAT_OPS:
            return 1 if argument[1] == _regex_parser.MAXREPEAT else 0
        if operation is getattr(_regex_parser, "SUBPATTERN", None):
            return _leading_unbounded_repeat_branches(argument[-1])
        if operation is getattr(_regex_parser, "BRANCH", None):
            return sum(_leading_unbounded_repeat_branches(branch) for branch in argument[1])
        if operation is getattr(_regex_parser, "ATOMIC_GROUP", None):
            return _leading_unbounded_repeat_branches(argument)
        return 0
    return 0


def _variable_repeat_before_suffix_exceeds_limit(
    sequence: Any,
    *,
    suffix_pending: bool = False,
) -> bool:
    suffix_seen = suffix_pending
    for operation, argument in reversed(sequence):
        if operation in _REGEX_REPEAT_OPS:
            minimum, maximum, nested = argument
            if (
                suffix_seen
                and minimum != maximum
                and (maximum == _regex_parser.MAXREPEAT or maximum > MAX_UNANCHORED_VARIABLE_REPEAT_SPAN)
            ):
                return True
            if _variable_repeat_before_suffix_exceeds_limit(nested, suffix_pending=suffix_seen):
                return True
        elif operation is getattr(_regex_parser, "SUBPATTERN", None):
            if _variable_repeat_before_suffix_exceeds_limit(argument[-1], suffix_pending=suffix_seen):
                return True
        elif operation is getattr(_regex_parser, "BRANCH", None):
            if any(
                _variable_repeat_before_suffix_exceeds_limit(branch, suffix_pending=suffix_seen)
                for branch in argument[1]
            ):
                return True
        elif operation in _REGEX_LOOKAROUND_OPS:
            if _variable_repeat_before_suffix_exceeds_limit(argument[1]):
                return True
        elif operation is getattr(_regex_parser, "ATOMIC_GROUP", None):
            if _variable_repeat_before_suffix_exceeds_limit(argument, suffix_pending=suffix_seen):
                return True
        suffix_seen = True
    return False


def _regex_search_work(pattern: str, parsed: Any) -> int:
    if pattern.startswith("^") or pattern.startswith(r"\A"):
        return 1
    total = 0
    for choices in _repeat_choice_paths(parsed):
        total += _repeat_path_work(choices)
        if total > MAX_REGEX_SEARCH_WORK_PER_PATTERN:
            return MAX_REGEX_SEARCH_WORK_PER_PATTERN + 1
    leading_unbounded_work = min(
        MAX_REGEX_SEARCH_WORK_PER_PATTERN + 1,
        _leading_unbounded_repeat_branches(parsed) * LEADING_UNBOUNDED_REPEAT_WORK,
    )
    return max(1, total, leading_unbounded_work)


def _parsed_regex_risk(
    sequence: Any,
    *,
    allow_leading_positive_lookahead: bool,
    in_repeat: bool = False,
    root: bool = True,
) -> str | None:
    if _repeat_node_count(sequence) > MAX_REPEAT_NODES_PER_BRANCH:
        return f"branch contains more than {MAX_REPEAT_NODES_PER_BRANCH} repetition nodes"
    if _unbounded_repeat_count(sequence) > 1:
        return "branch contains more than one unbounded repetition"
    if _variable_repeat_paths(sequence) > MAX_VARIABLE_REPEAT_PATHS_PER_BRANCH:
        return f"branch variable-repeat paths exceed {MAX_VARIABLE_REPEAT_PATHS_PER_BRANCH}"

    prefix_allows_lookahead = root and allow_leading_positive_lookahead
    for operation, argument in sequence:
        if operation is getattr(_regex_parser, "AT", None) and prefix_allows_lookahead:
            continue
        if operation in _REGEX_LOOKAROUND_OPS:
            direction, nested = argument
            if not prefix_allows_lookahead or operation is not getattr(_regex_parser, "ASSERT", None) or direction < 0:
                return "lookaround is unsupported"
            nested_risk = _parsed_regex_risk(
                nested,
                allow_leading_positive_lookahead=False,
                in_repeat=False,
                root=False,
            )
            if nested_risk is not None:
                return nested_risk
            continue

        prefix_allows_lookahead = False
        if operation in _REGEX_BACKREFERENCE_OPS:
            return "backreferences and conditionals are unsupported"
        if operation in _REGEX_REPEAT_OPS:
            minimum, maximum, nested = argument
            if minimum > MAX_BOUNDED_REPEAT or (maximum != _regex_parser.MAXREPEAT and maximum > MAX_BOUNDED_REPEAT):
                return f"bounded repetition exceeds {MAX_BOUNDED_REPEAT}"
            if in_repeat:
                return "nested repetition is unsupported"
            if _sequence_contains_operation(
                nested,
                frozenset({getattr(_regex_parser, "BRANCH", None)}),
            ):
                return "repeated alternation is unsupported"
            nested_risk = _parsed_regex_risk(
                nested,
                allow_leading_positive_lookahead=False,
                in_repeat=True,
                root=False,
            )
            if nested_risk is not None:
                return nested_risk
        elif operation is getattr(_regex_parser, "SUBPATTERN", None):
            nested_risk = _parsed_regex_risk(
                argument[-1],
                allow_leading_positive_lookahead=False,
                in_repeat=in_repeat,
                root=False,
            )
            if nested_risk is not None:
                return nested_risk
        elif operation is getattr(_regex_parser, "BRANCH", None):
            for branch in argument[1]:
                nested_risk = _parsed_regex_risk(
                    branch,
                    allow_leading_positive_lookahead=False,
                    in_repeat=in_repeat,
                    root=False,
                )
                if nested_risk is not None:
                    return nested_risk
        elif operation is getattr(_regex_parser, "ATOMIC_GROUP", None):
            nested_risk = _parsed_regex_risk(
                argument,
                allow_leading_positive_lookahead=False,
                in_repeat=in_repeat,
                root=False,
            )
            if nested_risk is not None:
                return nested_risk
    return None


def route_pattern_risk(pattern: str, *, allow_leading_positive_lookahead: bool = False) -> str | None:
    if len(pattern) > MAX_ROUTE_PATTERN_LENGTH:
        return f"pattern exceeds {MAX_ROUTE_PATTERN_LENGTH} characters"
    if any(ord(character) < 32 or ord(character) == 127 for character in pattern):
        return "pattern contains a control character"
    if ACTIVATION_NESTED_QUANTIFIER_PATTERN.search(pattern):
        return "nested repetition is unsupported"
    if ACTIVATION_QUANTIFIED_ALTERNATION_PATTERN.search(pattern):
        return "repeated alternation is unsupported"
    if ACTIVATION_BACKREFERENCE_PATTERN.search(pattern) or "(?P=" in pattern or "(?P>" in pattern:
        return "backreferences are unsupported"
    if "(?" + "(" in pattern:
        return "conditionals are unsupported"
    for repeat in BOUNDED_REPEAT_PATTERN.finditer(pattern):
        minimum = int(repeat.group("minimum"))
        maximum_text = repeat.group("maximum")
        maximum = int(maximum_text) if maximum_text else None
        if minimum > MAX_BOUNDED_REPEAT or (maximum is not None and maximum > MAX_BOUNDED_REPEAT):
            return f"bounded repetition exceeds {MAX_BOUNDED_REPEAT}"
    parsed = _regex_parser.parse(pattern, re.IGNORECASE)
    parsed_risk = _parsed_regex_risk(
        parsed,
        allow_leading_positive_lookahead=allow_leading_positive_lookahead,
    )
    if parsed_risk is not None:
        return parsed_risk
    if not pattern.startswith(("^", r"\A")) and _variable_repeat_before_suffix_exceeds_limit(parsed):
        return f"unanchored variable repeat span exceeds {MAX_UNANCHORED_VARIABLE_REPEAT_SPAN} characters"
    if _regex_search_work(pattern, parsed) > MAX_REGEX_SEARCH_WORK_PER_PATTERN:
        return f"unanchored regex search work exceeds {MAX_REGEX_SEARCH_WORK_PER_PATTERN}"
    return None


def normalize_v1_leading_positive_lookahead(pattern: str) -> tuple[str, bool]:
    if pattern.startswith("(?="):
        return "^" + pattern, True
    return pattern, False


def activation_pattern_risk(
    pattern: str,
    compiled: re.Pattern[str] | None = None,
    *,
    allow_leading_positive_lookahead: bool = False,
) -> str | None:
    try:
        base_risk = route_pattern_risk(
            pattern,
            allow_leading_positive_lookahead=allow_leading_positive_lookahead,
        )
    except REGEX_ERRORS:
        return None
    if base_risk is not None:
        return base_risk
    if not allow_leading_positive_lookahead:
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
    schema_version: int,
) -> None:
    trusted_bundle = patterns == SHIPPED_ACTIVATION_PATTERN_BUNDLES.get(field)
    for pattern in patterns:
        normalized, anchored = (
            normalize_v1_leading_positive_lookahead(pattern) if schema_version == 1 else (pattern, False)
        )
        trusted_pattern = trusted_bundle
        if not trusted_pattern:
            risk = activation_pattern_risk(
                normalized,
                allow_leading_positive_lookahead=anchored,
            )
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
            compiled = re.compile(normalized)
        except ACTIVATION_REGEX_ERRORS as exc:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"{finding_prefix}_regex_invalid",
                    f"{field} has invalid regex {pattern!r}: {exc}",
                )
            )
            continue
        risk = (
            None
            if trusted_pattern
            else activation_pattern_risk(
                normalized,
                compiled,
                allow_leading_positive_lookahead=anchored,
            )
        )
        if risk is not None:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"{finding_prefix}_regex_unsafe",
                    f"{field} has unsafe regex {pattern!r}: {risk}",
                )
            )
        if anchored:
            findings.append(
                PolicyFinding(
                    "WARN",
                    f"{finding_prefix}_regex_anchored",
                    f"{field} legacy leading lookahead was anchored: {pattern!r}",
                )
            )


def validate_common_config(config: dict[str, Any], schema_version: int, findings: list[PolicyFinding]) -> None:
    pattern_budget = [0]

    def bounded_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
        remaining = max(0, MAX_PATTERNS_PER_POLICY - pattern_budget[0])
        bounded = patterns[:remaining]
        pattern_budget[0] += len(bounded)
        return bounded

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
            bounded_patterns(meta_patterns),
            "activation.metaPatterns",
            "activation_meta_pattern",
            findings,
            schema_version,
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
            bounded_patterns(action_patterns),
            "activation.actionPatterns",
            "activation_action_pattern",
            findings,
            schema_version,
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
            bounded_patterns(no_action_patterns),
            "activation.noActionPatterns",
            "activation_no_action_pattern",
            findings,
            schema_version,
        )

    capability_retrieval = config.get("capabilityRetrieval")
    if capability_retrieval is not None and not isinstance(capability_retrieval, dict):
        findings.append(
            PolicyFinding(
                "WARN",
                "capability_retrieval_invalid",
                "capabilityRetrieval must be an object; legacy routing will continue",
            )
        )
    elif isinstance(capability_retrieval, dict):
        unknown_fields = sorted(
            str(field) for field in set(capability_retrieval) - {"mode", "maxCandidates", "algorithm"}
        )
        if unknown_fields:
            findings.append(
                PolicyFinding(
                    "WARN",
                    "capability_retrieval_fields_unknown",
                    "capabilityRetrieval has unknown fields; legacy routing will continue: "
                    + ", ".join(unknown_fields),
                )
            )
        retrieval_mode = capability_retrieval.get("mode", "off")
        if not isinstance(retrieval_mode, str) or retrieval_mode not in {"off", "shadow"}:
            findings.append(
                PolicyFinding(
                    "WARN",
                    "capability_retrieval_mode_invalid",
                    "capabilityRetrieval.mode must be one of: off, shadow; legacy routing will continue",
                )
            )
        max_candidates = capability_retrieval.get("maxCandidates", 3)
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 3:
            findings.append(
                PolicyFinding(
                    "WARN",
                    "capability_retrieval_max_candidates_invalid",
                    "capabilityRetrieval.maxCandidates must be an integer between 1 and 3; "
                    "legacy routing will continue",
                )
            )
        retrieval_algorithm = capability_retrieval.get("algorithm", "lexical-bm25-char3-anchored/v2")
        if retrieval_algorithm == "lexical-bm25-char3/v1":
            findings.append(
                PolicyFinding(
                    "WARN",
                    "capability_retrieval_algorithm_replay_only",
                    "capabilityRetrieval.algorithm v1 is reserved for frozen replay; legacy routing will continue",
                )
            )
        elif not isinstance(retrieval_algorithm, str) or retrieval_algorithm not in CAPABILITY_RETRIEVAL_ALGORITHMS:
            findings.append(
                PolicyFinding(
                    "WARN",
                    "capability_retrieval_algorithm_invalid",
                    "capabilityRetrieval.algorithm is unsupported; legacy routing will continue",
                )
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
        configured_policy_version = config.get("policyVersion")
        if configured_policy_version is not None and not policy_identifier(configured_policy_version):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_version_invalid",
                    "schema v1 policyVersion must be a bounded identifier when set",
                )
            )
        legacy_version = config.get("version")
        if isinstance(legacy_version, str) and not policy_identifier(f"route-v1:{legacy_version}"):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_version_invalid",
                    "schema v1 version is too long for the bounded route-result identifier",
                )
            )
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
        for pattern in bounded_patterns(answer_patterns):
            normalized, anchored = normalize_v1_leading_positive_lookahead(pattern)
            try:
                risk = route_pattern_risk(
                    normalized,
                    allow_leading_positive_lookahead=anchored,
                )
            except REGEX_ERRORS as exc:
                risk = None
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "answer_only_pattern_regex_invalid",
                        f"route answerOnlyPatterns has invalid answerOnlyPatterns regex {pattern!r}: {exc}",
                    )
                )
                continue
            if risk is not None:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "answer_only_pattern_regex_unsafe",
                        f"answerOnlyPatterns has unsafe regex {pattern!r}: {risk}",
                    )
                )
                continue
            try:
                re.compile(normalized)
            except REGEX_ERRORS as exc:
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "answer_only_pattern_regex_invalid",
                        f"route answerOnlyPatterns has invalid answerOnlyPatterns regex {pattern!r}: {exc}",
                    )
                )
            if anchored:
                findings.append(
                    PolicyFinding(
                        "WARN",
                        "answer_only_pattern_regex_anchored",
                        f"answerOnlyPatterns legacy leading lookahead was anchored: {pattern!r}",
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
        if not policy_identifier(policy_version):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_version_invalid",
                    "schema v2 policyVersion must be a bounded identifier of at most "
                    f"{MAX_POLICY_IDENTIFIER_CHARS} characters",
                )
            )


def validate_route_scalars(raw_route: dict[str, Any], route_id: str, findings: list[PolicyFinding]) -> None:
    bounds = {
        "priority": (ROUTE_PRIORITY_MIN, ROUTE_PRIORITY_MAX),
        "weight": (ROUTE_WEIGHT_MIN, ROUTE_WEIGHT_MAX),
    }
    for field, (minimum, maximum) in bounds.items():
        value = raw_route.get(field)
        if value is not None and (not is_number(value) or not minimum <= float(value) <= maximum):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    f"route_{field}_invalid",
                    f"route {route_id} {field} must be a number from {minimum:g} to {maximum:g} and must be finite",
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
    digest_chars = 12
    max_route_segment = MAX_POLICY_IDENTIFIER_CHARS - digest_chars - 1
    route_segment = (re.sub(r"[^A-Za-z0-9._-]+", "-", route_id).strip("-") or "route")[:max_route_segment]
    digest = hashlib.sha256(f"{route_id}\0{regex}".encode()).hexdigest()[:12]
    return f"{route_segment}.{digest}"


def lifecycle(
    raw_route: dict[str, Any],
    route_id: str,
    findings: list[PolicyFinding],
    schema_version: int,
) -> tuple[str, str | None]:
    if "lifecycle" not in raw_route:
        if schema_version == 1:
            return "active", None
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_lifecycle_missing",
                f"schema v2 route {route_id} must define lifecycle.state explicitly",
                route_id,
                "lifecycle",
            )
        )
        return "disabled", None
    value = raw_route.get("lifecycle")
    if not isinstance(value, dict):
        findings.append(
            PolicyFinding("ERROR", "route_lifecycle_invalid", f"route {route_id} lifecycle must be an object", route_id)
        )
        return "disabled", None
    allowed_fields = {
        "state",
        "proposalRevision",
        "previousState",
        "retiredByProposal",
        "retirementReason",
        "promotionEvidence",
    }
    unknown_fields = sorted(str(field) for field in set(value) - allowed_fields)
    if unknown_fields:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_lifecycle_fields_invalid",
                f"route {route_id} lifecycle contains unsupported fields: {', '.join(unknown_fields)}",
                route_id,
                "lifecycle",
            )
        )
        invalid_fields = True
    else:
        invalid_fields = False
    if "state" not in value:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_lifecycle_state_missing",
                f"route {route_id} lifecycle.state is required when lifecycle is present",
                route_id,
                "lifecycle.state",
            )
        )
        state: Any = "disabled"
    else:
        state = value.get("state")
    if not isinstance(state, str) or state not in ROUTE_LIFECYCLE_STATES:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_lifecycle_state_invalid",
                f"route {route_id} lifecycle.state must be active, disabled, or shadow",
                route_id,
            )
        )
        state = "disabled"
    elif invalid_fields:
        state = "disabled"
    revision = value.get("proposalRevision")
    if revision is not None and not policy_identifier(revision):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_proposal_revision_invalid",
                f"route {route_id} lifecycle.proposalRevision must be a bounded identifier",
                route_id,
            )
        )
        revision = None
        state = "disabled"
    previous_state = value.get("previousState")
    if previous_state is not None and (
        not isinstance(previous_state, str) or previous_state not in {"active", "shadow"}
    ):
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_previous_state_invalid",
                f"route {route_id} lifecycle.previousState must be active or shadow",
                route_id,
                "lifecycle.previousState",
            )
        )
        state = "disabled"
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
        retired_by = None
        state = "disabled"
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
        retirement_reason = None
        state = "disabled"
    promotion_evidence = value.get("promotionEvidence")
    if promotion_evidence is not None:
        valid_evidence = isinstance(promotion_evidence, dict) and set(promotion_evidence) == {
            "samples",
            "helpfulRate",
            "harmful",
        }
        if valid_evidence:
            samples = promotion_evidence.get("samples")
            helpful_rate = promotion_evidence.get("helpfulRate")
            harmful = promotion_evidence.get("harmful")
            valid_evidence = (
                not isinstance(samples, bool)
                and isinstance(samples, int)
                and samples >= 0
                and is_number(helpful_rate)
                and 0 <= float(helpful_rate) <= 1
                and not isinstance(harmful, bool)
                and isinstance(harmful, int)
                and harmful >= 0
            )
        if not valid_evidence:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_promotion_evidence_invalid",
                    f"route {route_id} lifecycle.promotionEvidence must contain valid samples, "
                    "helpfulRate, and harmful",
                    route_id,
                    "lifecycle.promotionEvidence",
                )
            )
            state = "disabled"
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
    if isinstance(value, str):
        if policy_identifier(value):
            return SkillRef(value, capability=capability)
        findings.append(
            PolicyFinding(
                "ERROR",
                "skill_binding_name_invalid",
                f"skill binding {capability} skill must be a bounded identifier",
                field=f"skillBindings.{capability}",
            )
        )
        return None
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
    if not policy_identifier(configured_name):
        if configured_name is not None:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "skill_binding_name_invalid",
                    f"skill binding {capability} skill must be a bounded identifier",
                    field=f"skillBindings.{capability}",
                )
            )
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
    schema_version: int,
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
        if not policy_identifier(pattern_id) or not BASE_PATTERN_ID_PATTERN.fullmatch(pattern_id):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_id_invalid",
                    f"route {route_id} pattern id contains unsupported characters or exceeds the length limit",
                    route_id,
                    field,
                )
            )
            pattern_id = stable_pattern_id(route_id, regex)
        label = label_value if isinstance(label_value, str) and label_value else regex
        if label_value is not None and not policy_text(label_value, allow_empty=False):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_label_invalid",
                    f"route {route_id} {field} pattern object label must be a non-empty string of at most "
                    f"{MAX_POLICY_TEXT_CHARS} characters",
                    route_id,
                    field,
                )
            )
            label = regex
        if not is_number(weight_value) or not 0 < float(weight_value) <= PATTERN_WEIGHT_MAX:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_weight_invalid",
                    f"route {route_id} pattern weight must be a positive number at most {PATTERN_WEIGHT_MAX:g} "
                    "and must be finite",
                    route_id,
                    field,
                )
            )
            weight = 1.0
        else:
            weight = float(weight_value)
        if not policy_identifier(facet_value) or not FACET_ID_PATTERN.fullmatch(facet_value):
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
    original_shipped_exception = route_pattern_is_shipped_exception(route_id, field, regex)
    normalized_regex, anchored = (
        normalize_v1_leading_positive_lookahead(regex)
        if schema_version == 1 and not original_shipped_exception
        else (regex, False)
    )
    shipped_exception = original_shipped_exception or route_pattern_is_shipped_exception(
        route_id,
        field,
        normalized_regex,
    )
    if not shipped_exception:
        try:
            risk = route_pattern_risk(
                normalized_regex,
                allow_leading_positive_lookahead=anchored,
            )
        except REGEX_ERRORS as exc:
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
        if risk is not None:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_pattern_regex_unsafe",
                    f"route {route_id} has unsafe regex {regex!r} for {field}: {risk}",
                    route_id,
                    field,
                )
            )
            return None
    try:
        re.compile(normalized_regex, re.IGNORECASE)
    except REGEX_ERRORS as exc:
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
    if anchored:
        findings.append(
            PolicyFinding(
                "WARN",
                "route_pattern_regex_anchored",
                f"route {route_id} legacy leading lookahead was anchored for {field}: {regex!r}",
                route_id,
                field,
            )
        )
    return PatternIR(pattern_id, normalized_regex, label, weight, facet)


def pattern_list(
    value: Any,
    route_id: str,
    findings: list[PolicyFinding],
    field: str,
    schema_version: int,
    *,
    allow_string: bool = True,
    require_id: bool = False,
    budget: list[int] | None = None,
) -> tuple[PatternIR, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    if budget is not None:
        remaining = max(0, MAX_PATTERNS_PER_POLICY - budget[0])
        values = values[:remaining]
        budget[0] += len(values)
    return tuple(
        pattern
        for item in values
        if (
            pattern := pattern_ir(
                item,
                route_id,
                findings,
                field,
                schema_version,
                allow_string=allow_string,
                require_id=require_id,
            )
        )
        is not None
    )


def raw_pattern_values(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def bounded_route_patterns(
    include_value: Any,
    exclude_value: Any,
    route_id: str,
    findings: list[PolicyFinding],
) -> tuple[list[Any], list[Any]]:
    includes = raw_pattern_values(include_value)
    excludes = raw_pattern_values(exclude_value)
    if len(includes) + len(excludes) > MAX_PATTERNS_PER_ROUTE:
        findings.append(
            PolicyFinding(
                "ERROR",
                "route_pattern_limit_exceeded",
                f"route {route_id} must contain at most {MAX_PATTERNS_PER_ROUTE} include and exclude patterns total",
                route_id,
            )
        )
    bounded_includes = includes[:MAX_PATTERNS_PER_ROUTE]
    remaining = MAX_PATTERNS_PER_ROUTE - len(bounded_includes)
    return bounded_includes, excludes[:remaining]


def raw_policy_pattern_count(config: dict[str, Any], schema_version: int) -> int:
    routes = config.get("routes")
    total = 0
    total += len(raw_pattern_values(config.get("answerOnlyPatterns")))
    activation = config.get("activation")
    if isinstance(activation, dict):
        for field in ("metaPatterns", "actionPatterns", "noActionPatterns"):
            total += len(raw_pattern_values(activation.get(field)))
    for route in routes if isinstance(routes, list) else []:
        if not isinstance(route, dict):
            continue
        if schema_version == 2:
            match = route.get("match")
            if not isinstance(match, dict):
                continue
            total += len(raw_pattern_values(match.get("any")))
            total += len(raw_pattern_values(match.get("none")))
        else:
            total += len(raw_pattern_values(route.get("patterns")))
            total += len(raw_pattern_values(route.get("excludePatterns")))
    return total


def raw_regex(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        regex = value.get("regex")
        return regex if isinstance(regex, str) else None
    return None


def add_policy_regex_work(
    untrusted_total: int,
    total: int,
    pattern: str,
    *,
    trusted: bool,
) -> tuple[int, int]:
    if len(pattern) > MAX_ROUTE_PATTERN_LENGTH:
        return untrusted_total, total
    try:
        parsed = _regex_parser.parse(pattern, re.IGNORECASE)
    except REGEX_ERRORS:
        return untrusted_total, total
    work = _regex_search_work(pattern, parsed)
    if work > MAX_REGEX_SEARCH_WORK_PER_PATTERN and not trusted:
        return untrusted_total, total
    total = min(MAX_REGEX_SEARCH_WORK_PER_POLICY + 1, total + work)
    if not trusted:
        untrusted_total = min(
            MAX_UNTRUSTED_REGEX_SEARCH_WORK_PER_POLICY + 1,
            untrusted_total + work,
        )
    return untrusted_total, total


def raw_policy_regex_work(config: dict[str, Any], schema_version: int) -> tuple[int, int]:
    untrusted_total = 0
    total = 0

    answer_only = raw_pattern_values(config.get("answerOnlyPatterns"))
    for value in answer_only[: MAX_PATTERNS_PER_POLICY + 1]:
        pattern = raw_regex(value)
        if pattern is None:
            continue
        normalized, _ = normalize_v1_leading_positive_lookahead(pattern) if schema_version == 1 else (pattern, False)
        untrusted_total, total = add_policy_regex_work(
            untrusted_total,
            total,
            normalized,
            trusted=False,
        )

    activation = config.get("activation")
    if isinstance(activation, dict):
        for field in ("metaPatterns", "actionPatterns", "noActionPatterns"):
            values = raw_pattern_values(activation.get(field))
            patterns = strings(activation.get(field))
            trusted_bundle = patterns == SHIPPED_ACTIVATION_PATTERN_BUNDLES.get(f"activation.{field}")
            for value in values[: MAX_PATTERNS_PER_POLICY + 1]:
                pattern = raw_regex(value)
                if pattern is None:
                    continue
                normalized, _ = (
                    normalize_v1_leading_positive_lookahead(pattern) if schema_version == 1 else (pattern, False)
                )
                untrusted_total, total = add_policy_regex_work(
                    untrusted_total,
                    total,
                    normalized,
                    trusted=trusted_bundle,
                )

    routes = config.get("routes")
    for raw_route in routes if isinstance(routes, list) else []:
        if not isinstance(raw_route, dict):
            continue
        if schema_version == 2:
            route_id = raw_route.get("id")
            match = raw_route.get("match")
            fields = (
                (
                    ("match.any", match.get("any")),
                    ("match.none", match.get("none")),
                )
                if isinstance(match, dict)
                else ()
            )
        else:
            route_id = raw_route.get("name")
            fields = (
                ("patterns", raw_route.get("patterns")),
                ("excludePatterns", raw_route.get("excludePatterns")),
            )
        route_id = route_id if isinstance(route_id, str) else ""
        for field, raw_values in fields:
            for value in raw_pattern_values(raw_values)[: MAX_PATTERNS_PER_ROUTE + 1]:
                pattern = raw_regex(value)
                if pattern is None:
                    continue
                original_exception = route_pattern_is_shipped_exception(route_id, field, pattern)
                normalized, _ = (
                    normalize_v1_leading_positive_lookahead(pattern)
                    if schema_version == 1 and not original_exception
                    else (pattern, False)
                )
                trusted = original_exception or route_pattern_is_shipped_exception(route_id, field, normalized)
                untrusted_total, total = add_policy_regex_work(
                    untrusted_total,
                    total,
                    normalized,
                    trusted=trusted,
                )
                if (
                    untrusted_total > MAX_UNTRUSTED_REGEX_SEARCH_WORK_PER_POLICY
                    or total > MAX_REGEX_SEARCH_WORK_PER_POLICY
                ):
                    return untrusted_total, total
    return untrusted_total, total


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
    if not isinstance(required_value, list) or any(
        not policy_identifier(facet) or not FACET_ID_PATTERN.fullmatch(facet) for facet in required
    ):
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


def parse_v1(
    config: dict[str, Any],
    findings: list[PolicyFinding],
    pattern_budget: list[int] | None = None,
) -> tuple[RouteIR, ...]:
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
        if not policy_identifier(route_id):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_id_invalid",
                    f"route #{index} name must be a bounded identifier",
                )
            )
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
        if not policy_identifier(primary_name):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_primary_invalid",
                    f"route {route_id} primary must be a bounded identifier",
                    route_id,
                    "primary",
                )
            )
            continue
        pattern_values, exclude_values = bounded_route_patterns(
            raw_route.get("patterns"),
            raw_route.get("excludePatterns"),
            route_id,
            findings,
        )
        patterns = pattern_list(
            pattern_values,
            route_id,
            findings,
            "patterns",
            1,
            budget=pattern_budget,
        )
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
        if any(not policy_identifier(name) for name in supporting_names):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_supporting_invalid",
                    f"route {route_id} supporting must contain bounded identifiers",
                    route_id,
                    "supporting",
                )
            )
            supporting_names = tuple(name for name in supporting_names if policy_identifier(name))
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
        if isinstance(verification_name, str) and verification_name and not policy_identifier(verification_name):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_verification_invalid",
                    f"route {route_id} verification must be a bounded identifier",
                    route_id,
                    "verification",
                )
            )
            verification_name = ""
        state, proposal_revision = lifecycle(raw_route, route_id, findings, 1)
        intent = raw_route.get("intent", route_id)
        if not isinstance(intent, str) or not intent:
            intent_id = route_id
        elif policy_identifier(intent):
            intent_id = intent
        else:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_intent_invalid",
                    f"route {route_id} intent must be a bounded identifier",
                    route_id,
                    "intent",
                )
            )
            intent_id = route_id
        reason_value = raw_route.get("reason", "")
        if not policy_text(reason_value, allow_empty=True):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_reason_invalid",
                    f"route {route_id} reason must be a string of at most {MAX_POLICY_TEXT_CHARS} characters",
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
                pattern_list(
                    exclude_values,
                    route_id,
                    findings,
                    "excludePatterns",
                    1,
                    budget=pattern_budget,
                ),
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


def parse_v2(
    config: dict[str, Any],
    findings: list[PolicyFinding],
    pattern_budget: list[int] | None = None,
) -> tuple[RouteIR, ...]:
    bindings_value = config.get("skillBindings")
    if not isinstance(bindings_value, dict):
        findings.append(PolicyFinding("ERROR", "skill_bindings_invalid", "schema v2 skillBindings must be an object"))
        bindings_value = {}
    bindings: dict[str, SkillRef] = {}
    for capability, value in bindings_value.items():
        if not policy_identifier(capability):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "skill_binding_capability_invalid",
                    "skill binding capability must be a bounded identifier",
                )
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
    if fallback_route_id is not None and not policy_identifier(fallback_route_id):
        findings.append(
            PolicyFinding(
                "ERROR",
                "fallback_route_id_invalid",
                "schema v2 fallbackRouteId must be a bounded identifier or null",
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
        if not policy_identifier(route_id):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_id_invalid",
                    f"schema v2 route #{index} id must be a bounded identifier",
                )
            )
            continue
        validate_route_scalars(raw_route, route_id, findings)
        if route_id in seen:
            findings.append(PolicyFinding("ERROR", "route_id_duplicate", f"duplicate route id: {route_id}", route_id))
            continue
        seen.add(route_id)
        if not policy_identifier(intent):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_intent_invalid",
                    f"route {route_id} intent must be a bounded identifier",
                    route_id,
                )
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
            if any(not policy_identifier(capability) for capability in capabilities):
                findings.append(
                    PolicyFinding(
                        "ERROR",
                        "route_capabilities_invalid",
                        f"route {route_id} capabilityRequirements.{role} must contain bounded identifiers",
                        route_id,
                        f"capabilityRequirements.{role}",
                    )
                )
                capabilities = tuple(capability for capability in capabilities if policy_identifier(capability))
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
        any_patterns, none_patterns = bounded_route_patterns(any_patterns, none_patterns, route_id, findings)
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
            2,
            allow_string=False,
            require_id=True,
            budget=pattern_budget,
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
        state, proposal_revision = lifecycle(raw_route, route_id, findings, 2)
        reason_value = raw_route.get("reason", "")
        if not policy_text(reason_value, allow_empty=True):
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "route_reason_invalid",
                    f"route {route_id} reason must be a string of at most {MAX_POLICY_TEXT_CHARS} characters",
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
                pattern_list(
                    none_patterns,
                    route_id,
                    findings,
                    "match.none",
                    2,
                    require_id=True,
                    budget=pattern_budget,
                ),
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
        untrusted_regex_work, policy_regex_work = raw_policy_regex_work(config, schema_version)
        regex_work_exceeded = (
            untrusted_regex_work > MAX_UNTRUSTED_REGEX_SEARCH_WORK_PER_POLICY
            or policy_regex_work > MAX_REGEX_SEARCH_WORK_PER_POLICY
        )
        if regex_work_exceeded:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_regex_work_limit_exceeded",
                    "policy regex search work exceeds its untrusted or total compatibility budget",
                )
            )
        validate_common_config(config, schema_version, findings)
        pattern_count = raw_policy_pattern_count(config, schema_version)
        if pattern_count > MAX_PATTERNS_PER_POLICY:
            findings.append(
                PolicyFinding(
                    "ERROR",
                    "policy_pattern_limit_exceeded",
                    f"policy must contain at most {MAX_PATTERNS_PER_POLICY} regex patterns; found {pattern_count}",
                )
            )
        configured_non_route_patterns = len(raw_pattern_values(config.get("answerOnlyPatterns")))
        activation = config.get("activation")
        if isinstance(activation, dict):
            configured_non_route_patterns += sum(
                len(raw_pattern_values(activation.get(field)))
                for field in ("metaPatterns", "actionPatterns", "noActionPatterns")
            )
        pattern_budget = [min(configured_non_route_patterns, MAX_PATTERNS_PER_POLICY)]
        if regex_work_exceeded:
            routes = ()
        else:
            routes = (
                parse_v2(config, findings, pattern_budget)
                if schema_version == 2
                else parse_v1(config, findings, pattern_budget)
            )
    default_verification_value = config.get("defaultVerification")
    if default_verification_value is not None and not policy_identifier(default_verification_value):
        findings.append(
            PolicyFinding(
                "ERROR",
                "default_verification_invalid",
                "defaultVerification must be a bounded identifier when set",
            )
        )
    default_verification = (
        SkillRef(default_verification_value, capability=f"skill:{default_verification_value}")
        if policy_identifier(default_verification_value)
        else None
    )
    allowed_value = config.get("allowedSkills")
    if allowed_value is not None and (
        not isinstance(allowed_value, list)
        or not allowed_value
        or not all(policy_identifier(item) for item in allowed_value)
    ):
        findings.append(
            PolicyFinding(
                "ERROR",
                "allowed_skills_invalid",
                "allowedSkills must be a non-empty list of bounded identifiers when present",
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
            policy_version_value if policy_identifier(policy_version_value) else None,
            allowed,
            default_verification,
            fallback_value if policy_identifier(fallback_value) else None,
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
