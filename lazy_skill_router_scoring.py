from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable


MIN_CONFIDENCE = 0.55
NORMAL_CONFIDENCE = 0.75
MAX_MATCHED_SIGNALS = 5
DEFAULT_VERIFICATION = "verification-gate"
PRIORITY_SCORE_STEP = 0.05


@dataclass(frozen=True)
class Route:
    name: str
    primary: str
    supporting: tuple[str, ...]
    verification: str
    reason: str
    patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    priority: float
    weight: float
    fallback: bool


@dataclass(frozen=True)
class RouteMatch:
    route: Route
    confidence: float
    score: float
    matched_signals: tuple[str, ...]


def debug(message: str) -> None:
    if os.environ.get("LAZY_SKILL_ROUTER_DEBUG"):
        print(f"lazy-skill-router: {message}", file=sys.stderr)


def tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
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


def text_matches(text: str, patterns: Iterable[str]) -> bool:
    return bool(matched_patterns(text, patterns))


def configured_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def confidence_for(matches: tuple[str, ...]) -> float:
    return min(0.95, 0.50 + (0.15 * len(matches)))


def confidence_label(confidence: float) -> str:
    return "normal" if confidence >= NORMAL_CONFIDENCE else "weak"


def score_for(route: Route, confidence: float) -> float:
    score = confidence + route.weight + (route.priority * PRIORITY_SCORE_STEP)
    return max(0.0, min(1.0, score))


def allowed_skills(config: dict[str, Any]) -> set[str]:
    return set(tuple_of_strings(config.get("allowedSkills")))


def default_verification(config: dict[str, Any]) -> str:
    value = config.get("defaultVerification", DEFAULT_VERIFICATION)
    return value if isinstance(value, str) else DEFAULT_VERIFICATION


def filter_route(route: Route, config: dict[str, Any]) -> Route | None:
    skills = allowed_skills(config)
    if not skills:
        return route
    if route.primary not in skills:
        debug(f"route {route.name} primary is not allowlisted: {route.primary}")
        return None

    supporting = tuple(skill for skill in route.supporting if skill in skills)
    verification = route.verification or default_verification(config)
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
    )


def candidate_match(prompt: str, route: Route, config: dict[str, Any]) -> RouteMatch | None:
    if route.exclude_patterns and text_matches(prompt, route.exclude_patterns):
        return None
    matches = matched_patterns(prompt, route.patterns)
    confidence = confidence_for(matches)
    if not matches or confidence < configured_float(config, "minConfidence", MIN_CONFIDENCE):
        return None
    filtered = filter_route(route, config)
    if filtered is None:
        return None
    return RouteMatch(filtered, confidence, score_for(filtered, confidence), matches[:MAX_MATCHED_SIGNALS])


def route_rank(match: RouteMatch, index: int) -> tuple[int, float, float, int]:
    return (0 if match.route.fallback else 1, match.score, match.confidence, -index)


def choose_route(prompt: str, routes: list[Route], config: dict[str, Any]) -> RouteMatch | None:
    best_match: RouteMatch | None = None
    best_rank: tuple[int, float, float, int] | None = None
    lowered = prompt.lower()
    for index, route in enumerate(routes):
        match = candidate_match(lowered, route, config)
        if match is None:
            continue
        rank = route_rank(match, index)
        if best_rank is None or rank > best_rank:
            best_match = match
            best_rank = rank
    return best_match
