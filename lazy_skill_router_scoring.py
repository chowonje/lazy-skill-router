from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from lazy_skill_router_common import debug

MIN_CONFIDENCE = 0.55
NORMAL_CONFIDENCE = 0.75
MAX_MATCHED_SIGNALS = 5
PRIORITY_SCORE_STEP = 0.05


@dataclass(frozen=True)
class RoutePattern:
    regex: str
    label: str
    pattern_id: str
    weight: float
    facet: str = "signal"


@dataclass(frozen=True)
class CapabilityRequirements:
    primary: tuple[str, ...]
    supporting: tuple[str, ...]
    verification: tuple[str, ...]


@dataclass(frozen=True)
class RouteActivation:
    required_facets: tuple[str, ...] = ()
    scope: str = "turn"
    mode: str = "auto"


@dataclass(frozen=True)
class Route:
    name: str
    primary: str
    supporting: tuple[str, ...]
    verification: str
    reason: str
    patterns: tuple[RoutePattern, ...]
    exclude_patterns: tuple[str, ...]
    priority: float
    weight: float
    fallback: bool
    intent: str
    capability_requirements: CapabilityRequirements
    lifecycle_state: str = "active"
    proposal_revision: str | None = None
    activation: RouteActivation = RouteActivation()


@dataclass(frozen=True)
class RouteMatch:
    route: Route
    confidence: float
    score: float
    matched_signals: tuple[str, ...]
    matched_patterns: tuple[str, ...]
    matched_pattern_ids: tuple[str, ...]


def tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def stable_pattern_id(route_id: str, regex: str) -> str:
    route_segment = re.sub(r"[^A-Za-z0-9._-]+", "-", route_id).strip("-") or "route"
    digest = hashlib.sha256(f"{route_id}\0{regex}".encode()).hexdigest()[:12]
    return f"{route_segment}.{digest}"


def route_pattern(value: Any, route_id: str = "route") -> RoutePattern | None:
    if isinstance(value, str):
        return RoutePattern(value, value, stable_pattern_id(route_id, value), 1.0, "signal")
    if not isinstance(value, dict):
        return None

    regex = value.get("regex")
    label = value.get("label", regex)
    configured_id = value.get("id", value.get("pattern_id"))
    configured_weight = value.get("weight", 1.0)
    configured_facet = value.get("facet", "signal")
    if not isinstance(regex, str) or not regex:
        return None
    if not isinstance(label, str) or not label:
        label = regex
    pattern_id = (
        configured_id if isinstance(configured_id, str) and configured_id else stable_pattern_id(route_id, regex)
    )
    weight = (
        float(configured_weight)
        if not isinstance(configured_weight, bool) and isinstance(configured_weight, (int, float))
        else 1.0
    )
    facet = configured_facet if isinstance(configured_facet, str) and configured_facet else "signal"
    return RoutePattern(regex, label, pattern_id, max(0.0, weight), facet)


def tuple_of_patterns(value: Any, route_id: str = "route") -> tuple[RoutePattern, ...]:
    if value is None:
        return ()
    pattern = route_pattern(value, route_id)
    if pattern is not None:
        return (pattern,)
    if isinstance(value, list):
        return tuple(pattern for item in value if (pattern := route_pattern(item, route_id)) is not None)
    return ()


def route_number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def matched_patterns(text: str, patterns: Iterable[str]) -> tuple[str, ...]:
    matches: list[str] = []
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                matches.append(pattern)
        except re.error as exc:
            debug(f"invalid regex {pattern!r}: {exc}")
    return tuple(matches)


def matched_route_patterns(text: str, patterns: Iterable[RoutePattern]) -> tuple[RoutePattern, ...]:
    matches: list[RoutePattern] = []
    for pattern in patterns:
        try:
            if re.search(pattern.regex, text, re.IGNORECASE):
                matches.append(pattern)
        except re.error as exc:
            debug(f"invalid regex {pattern.regex!r}: {exc}")
    return tuple(matches)


def text_matches(text: str, patterns: Iterable[str]) -> bool:
    return bool(matched_patterns(text, patterns))


def configured_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def minimum_match_strength(config: dict[str, Any]) -> float:
    if config.get("schemaVersion") == 2:
        selection = config.get("selection")
        if isinstance(selection, dict):
            return configured_float(selection, "minMatchStrength", MIN_CONFIDENCE)
    return configured_float(config, "minConfidence", MIN_CONFIDENCE)


def confidence_for(matches: tuple[str, ...]) -> float:
    return min(0.95, 0.50 + (0.15 * len(matches)))


def match_strength_for(matches: tuple[RoutePattern, ...]) -> float:
    return min(0.95, 0.50 + (0.15 * sum(pattern.weight for pattern in matches)))


def confidence_label(confidence: float) -> str:
    return "normal" if confidence >= NORMAL_CONFIDENCE else "weak"


def score_for(route: Route, confidence: float) -> float:
    score = confidence + route.weight + (route.priority * PRIORITY_SCORE_STEP)
    return max(0.0, min(1.0, score))


def allowed_skills(config: dict[str, Any]) -> set[str]:
    return set(tuple_of_strings(config.get("allowedSkills")))


def filter_route(route: Route, config: dict[str, Any]) -> Route | None:
    skills = allowed_skills(config)
    if not skills:
        return route
    if route.primary not in skills:
        debug(f"route {route.name} primary is not allowlisted: {route.primary}")
        return None

    supporting = tuple(skill for skill in route.supporting if skill in skills)
    verification = route.verification
    if verification not in skills:
        verification = ""

    return Route(
        route.name,
        route.primary,
        supporting,
        verification,
        route.reason,
        route.patterns,
        route.exclude_patterns,
        route.priority,
        route.weight,
        route.fallback,
        route.intent,
        route.capability_requirements,
        route.lifecycle_state,
        route.proposal_revision,
        route.activation,
    )


def candidate_match(prompt: str, route: Route, config: dict[str, Any]) -> RouteMatch | None:
    if route.exclude_patterns and text_matches(prompt, route.exclude_patterns):
        return None
    matches = matched_route_patterns(prompt, route.patterns)
    matched_signals = tuple(pattern.label for pattern in matches)
    matched_regexes = tuple(pattern.regex for pattern in matches)
    matched_ids = tuple(pattern.pattern_id for pattern in matches)
    confidence = match_strength_for(matches)
    if not matches or confidence < minimum_match_strength(config):
        return None
    filtered = filter_route(route, config)
    if filtered is None:
        return None
    return RouteMatch(
        filtered,
        confidence,
        score_for(filtered, confidence),
        matched_signals[:MAX_MATCHED_SIGNALS],
        matched_regexes[:MAX_MATCHED_SIGNALS],
        matched_ids[:MAX_MATCHED_SIGNALS],
    )


def route_rank(match: RouteMatch, index: int) -> tuple[int, float, float, int]:
    return (0 if match.route.fallback else 1, match.score, match.confidence, -index)


def ranked_route_matches(prompt: str, routes: list[Route], config: dict[str, Any]) -> tuple[RouteMatch, ...]:
    ranked: list[tuple[tuple[int, float, float, int], RouteMatch]] = []
    lowered = prompt.lower()
    for index, route in enumerate(routes):
        match = candidate_match(lowered, route, config)
        if match is None:
            continue
        rank = route_rank(match, index)
        ranked.append((rank, match))
    return tuple(match for _, match in sorted(ranked, key=lambda item: item[0], reverse=True))


def choose_route(prompt: str, routes: list[Route], config: dict[str, Any]) -> RouteMatch | None:
    matches = ranked_route_matches(prompt, routes, config)
    return matches[0] if matches else None


def ranked_route_matches_v2(prompt: str, routes: list[Route], config: dict[str, Any]) -> tuple[RouteMatch, ...]:
    lowered = prompt.lower()
    normal = tuple(
        match
        for route in routes
        if not route.fallback and (match := candidate_match(lowered, route, config)) is not None
    )
    if normal:
        selected = normal
    else:
        fallback_matches = []
        for route in routes:
            if not route.fallback:
                continue
            match = candidate_match(lowered, route, config)
            if match is None and not route.patterns:
                filtered = filter_route(route, config)
                if filtered is not None:
                    match = RouteMatch(filtered, 0.0, score_for(filtered, 0.0), (), (), ())
            if match is not None:
                fallback_matches.append(match)
        selected = tuple(fallback_matches)
    return tuple(sorted(selected, key=lambda match: (-match.score, -match.confidence, match.route.name)))
