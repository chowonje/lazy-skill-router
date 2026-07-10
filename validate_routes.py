from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def pattern_regex(value: Any, route_name: str, field: str) -> tuple[str | None, list[Finding]]:
    if isinstance(value, str):
        return value, []
    if not isinstance(value, dict):
        return None, [Finding("ERROR", f"route {route_name} {field} entries must be strings or pattern objects")]

    regex = value.get("regex")
    if not isinstance(regex, str) or not regex:
        return None, [Finding("ERROR", f"route {route_name} {field} pattern object missing string regex")]
    label = value.get("label")
    if label is not None and (not isinstance(label, str) or not label):
        return None, [Finding("ERROR", f"route {route_name} {field} pattern object label must be a non-empty string")]
    return regex, []


def pattern_regexes(value: Any, route_name: str, field: str) -> tuple[list[str], list[Finding]]:
    if value is None:
        return [], []
    values = value if isinstance(value, list) else [value]
    patterns: list[str] = []
    findings: list[Finding] = []
    for item in values:
        regex, item_findings = pattern_regex(item, route_name, field)
        findings.extend(item_findings)
        if regex is not None:
            patterns.append(regex)
    return patterns, findings


def load_config(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return None, [Finding("ERROR", f"file not found: {path}")]
    except json.JSONDecodeError as exc:
        return None, [Finding("ERROR", f"invalid JSON: {exc}")]
    except OSError as exc:
        return None, [Finding("ERROR", f"failed to read file: {exc}")]
    if not isinstance(loaded, dict):
        return None, [Finding("ERROR", "config root must be an object")]
    return loaded, []


def check_patterns(route_name: str, field: str, patterns: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            findings.append(Finding("ERROR", f"route {route_name} has invalid {field} regex {pattern!r}: {exc}"))
    return findings


def is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def check_scoring_fields(route: dict[str, Any], name: str) -> list[Finding]:
    findings: list[Finding] = []
    priority = route.get("priority")
    if priority is not None and not is_number(priority):
        findings.append(Finding("ERROR", f"route {name} priority must be a number when set"))
    weight = route.get("weight")
    if weight is not None and not is_number(weight):
        findings.append(Finding("ERROR", f"route {name} weight must be a number when set"))
    fallback = route.get("fallback")
    if fallback is not None and not isinstance(fallback, bool):
        findings.append(Finding("ERROR", f"route {name} fallback must be a boolean when set"))
    return findings


def check_display_config(config: dict[str, Any]) -> list[Finding]:
    display = config.get("display", {})
    if not display:
        return []
    if not isinstance(display, dict):
        return [Finding("ERROR", "display must be an object when present")]
    show_notice = display.get("showRouterNotice")
    if show_notice is not None and not isinstance(show_notice, bool):
        return [Finding("ERROR", "display.showRouterNotice must be a boolean when set")]
    return []


def check_activation_config(config: dict[str, Any]) -> list[Finding]:
    activation = config.get("activation")
    if activation is None:
        return []
    if not isinstance(activation, dict):
        return [Finding("ERROR", "activation must be an object when present")]
    mode = activation.get("mode")
    if mode not in {"inject", "off", "shadow"}:
        return [Finding("ERROR", "activation.mode must be one of: inject, off, shadow")]
    return []


def check_logging_config(config: dict[str, Any]) -> list[Finding]:
    logging_config = config.get("logging", {})
    if not logging_config:
        return []
    if not isinstance(logging_config, dict):
        return [Finding("ERROR", "logging must be an object when present")]

    findings = []
    enabled = logging_config.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        findings.append(Finding("ERROR", "logging.enabled must be a boolean when set"))
    path = logging_config.get("path")
    if path is not None and not isinstance(path, str):
        findings.append(Finding("ERROR", "logging.path must be a string when set"))
    for field in ("maxEntries", "retentionDays"):
        value = logging_config.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            findings.append(Finding("ERROR", f"logging.{field} must be a positive integer when set"))
    return findings


def validate_route(route: Any, index: int, allowed: set[str]) -> tuple[str | None, list[Finding]]:
    findings: list[Finding] = []
    if not isinstance(route, dict):
        return None, [Finding("ERROR", f"route #{index} must be an object")]

    name = route.get("name")
    primary = route.get("primary")
    if not isinstance(name, str) or not name:
        findings.append(Finding("ERROR", f"route #{index} missing string name"))
        name = f"#{index}"
    if not isinstance(primary, str) or not primary:
        findings.append(Finding("ERROR", f"route {name} missing string primary"))
    elif allowed and primary not in allowed:
        findings.append(Finding("ERROR", f"route {name} primary is not in allowedSkills: {primary}"))

    patterns, pattern_findings = pattern_regexes(route.get("patterns"), str(name), "patterns")
    findings.extend(pattern_findings)
    if not patterns:
        findings.append(Finding("ERROR", f"route {name} must define non-empty patterns"))
    findings.extend(check_patterns(str(name), "patterns", patterns))
    findings.extend(check_patterns(str(name), "excludePatterns", strings(route.get("excludePatterns"))))
    findings.extend(check_scoring_fields(route, str(name)))

    for field in ("supporting",):
        for skill in strings(route.get(field)):
            if allowed and skill not in allowed:
                findings.append(Finding("WARN", f"route {name} {field} skill is not in allowedSkills: {skill}"))

    verification = route.get("verification", "")
    if isinstance(verification, str) and verification and allowed and verification not in allowed:
        findings.append(Finding("WARN", f"route {name} verification skill is not in allowedSkills: {verification}"))
    elif verification and not isinstance(verification, str):
        findings.append(Finding("ERROR", f"route {name} verification must be a string when set"))

    return str(name) if isinstance(name, str) else None, findings


def validate_v2_pattern(
    value: Any,
    route_id: str,
    field: str,
    seen_pattern_ids: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(value, str) and field == "none":
        try:
            re.compile(value)
        except re.error as exc:
            findings.append(Finding("ERROR", f"route {route_id} has invalid regex {value!r}: {exc}"))
        return findings
    if not isinstance(value, dict):
        return [Finding("ERROR", f"route {route_id} match.{field} entries must be pattern objects")]

    pattern_id = value.get("id")
    regex = value.get("regex")
    weight = value.get("weight", 1)
    if not isinstance(pattern_id, str) or not pattern_id:
        findings.append(Finding("ERROR", f"route {route_id} match.{field} pattern missing string id"))
    elif pattern_id in seen_pattern_ids:
        findings.append(Finding("ERROR", f"duplicate pattern id: {pattern_id}"))
    else:
        seen_pattern_ids.add(pattern_id)
    if not isinstance(regex, str) or not regex:
        findings.append(Finding("ERROR", f"route {route_id} match.{field} pattern missing string regex"))
    else:
        try:
            re.compile(regex)
        except re.error as exc:
            findings.append(Finding("ERROR", f"route {route_id} has invalid regex {regex!r}: {exc}"))
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
        findings.append(Finding("ERROR", f"route {route_id} pattern weight must be a positive number"))
    return findings


def validate_v2_selection(config: dict[str, Any]) -> list[Finding]:
    selection = config.get("selection")
    if not isinstance(selection, dict):
        return [Finding("ERROR", "schema v2 selection must be an object")]
    findings = []
    if selection.get("mode") != "ranked":
        findings.append(Finding("ERROR", "schema v2 selection.mode must be ranked"))
    max_recommendations = selection.get("maxRecommendations")
    if (
        isinstance(max_recommendations, bool)
        or not isinstance(max_recommendations, int)
        or not 1 <= max_recommendations <= 3
    ):
        findings.append(Finding("ERROR", "schema v2 selection.maxRecommendations must be an integer from 1 to 3"))
    for field in ("minMatchStrength", "minScoreMargin"):
        value = selection.get(field)
        if not is_number(value) or not 0 <= float(value) <= 1:
            findings.append(Finding("ERROR", f"schema v2 selection.{field} must be a number between 0 and 1"))
    return findings


def validate_config_v2(config: dict[str, Any]) -> list[Finding]:
    findings = validate_v2_selection(config)
    policy_version = config.get("policyVersion")
    if not isinstance(policy_version, str) or not policy_version:
        findings.append(Finding("ERROR", "schema v2 policyVersion must be a non-empty string"))

    bindings = config.get("skillBindings")
    if not isinstance(bindings, dict):
        findings.append(Finding("ERROR", "schema v2 skillBindings must be an object"))
        bindings = {}
    else:
        for capability, binding in bindings.items():
            skill = binding.get("skill") if isinstance(binding, dict) else binding
            if not isinstance(capability, str) or not capability or not isinstance(skill, str) or not skill:
                findings.append(Finding("ERROR", "schema v2 skillBindings entries must map names to skill strings"))

    routes = config.get("routes")
    if not isinstance(routes, list) or not routes:
        return [*findings, Finding("ERROR", "schema v2 routes must be a non-empty list")]

    fallback_route_id = config.get("fallbackRouteId")
    if fallback_route_id is not None and (not isinstance(fallback_route_id, str) or not fallback_route_id):
        findings.append(Finding("ERROR", "schema v2 fallbackRouteId must be a non-empty string or null"))

    seen_route_ids: set[str] = set()
    seen_pattern_ids: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            findings.append(Finding("ERROR", f"schema v2 route #{index} must be an object"))
            continue
        route_id = route.get("id")
        if not isinstance(route_id, str) or not route_id:
            findings.append(Finding("ERROR", f"schema v2 route #{index} missing string id"))
            route_id = f"#{index}"
        elif route_id in seen_route_ids:
            findings.append(Finding("ERROR", f"duplicate route id: {route_id}"))
        else:
            seen_route_ids.add(route_id)
        intent = route.get("intent")
        if not isinstance(intent, str) or not intent:
            findings.append(Finding("ERROR", f"route {route_id} missing string intent"))

        requirements = route.get("capabilityRequirements")
        if not isinstance(requirements, dict):
            findings.append(Finding("ERROR", f"route {route_id} capabilityRequirements must be an object"))
            requirements = {}
        primary = strings(requirements.get("primary"))
        if not primary:
            findings.append(Finding("ERROR", f"route {route_id} must require at least one primary capability"))
        for role in ("primary", "supporting", "verification"):
            raw = requirements.get(role, [])
            capabilities = strings(raw)
            if raw and not capabilities:
                findings.append(Finding("ERROR", f"route {route_id} capabilityRequirements.{role} must be strings"))
            for capability in capabilities:
                if capability not in bindings:
                    findings.append(Finding("ERROR", f"route {route_id} missing skill binding for {capability}"))

        match = route.get("match", {})
        if not isinstance(match, dict):
            findings.append(Finding("ERROR", f"route {route_id} match must be an object"))
            match = {}
        any_patterns = match.get("any", [])
        none_patterns = match.get("none", [])
        is_fallback = route_id == fallback_route_id or route.get("fallback") is True
        if not isinstance(any_patterns, list):
            findings.append(Finding("ERROR", f"route {route_id} match.any must be a list"))
            any_patterns = []
        if not any_patterns and not is_fallback:
            findings.append(Finding("ERROR", f"route {route_id} match.any must not be empty"))
        if not isinstance(none_patterns, list):
            findings.append(Finding("ERROR", f"route {route_id} match.none must be a list"))
            none_patterns = []
        for pattern in any_patterns:
            findings.extend(validate_v2_pattern(pattern, route_id, "any", seen_pattern_ids))
        for pattern in none_patterns:
            findings.extend(validate_v2_pattern(pattern, route_id, "none", seen_pattern_ids))

    if isinstance(fallback_route_id, str) and fallback_route_id not in seen_route_ids:
        findings.append(Finding("ERROR", f"schema v2 fallbackRouteId references missing route: {fallback_route_id}"))
    return findings


def validate_config(config: dict[str, Any]) -> list[Finding]:
    schema_version = config.get("schemaVersion", 1)
    if schema_version == 2:
        return validate_config_v2(config)
    if schema_version != 1 or isinstance(schema_version, bool):
        return [Finding("ERROR", f"unsupported schemaVersion: {schema_version}")]

    findings: list[Finding] = []
    min_confidence = config.get("minConfidence", 0.55)
    if not isinstance(min_confidence, (int, float)) or not 0 <= float(min_confidence) <= 1:
        findings.append(Finding("ERROR", "minConfidence must be a number between 0 and 1"))

    allowed = set(strings(config.get("allowedSkills")))
    if "allowedSkills" in config and not allowed:
        findings.append(Finding("ERROR", "allowedSkills must be a non-empty list of strings when present"))

    findings.extend(
        check_patterns("answerOnlyPatterns", "answerOnlyPatterns", strings(config.get("answerOnlyPatterns")))
    )

    findings.extend(check_activation_config(config))
    findings.extend(check_logging_config(config))
    findings.extend(check_display_config(config))

    routes = config.get("routes")
    if not isinstance(routes, list) or not routes:
        return [*findings, Finding("ERROR", "routes must be a non-empty list")]

    seen: set[str] = set()
    for index, route in enumerate(routes):
        name, route_findings = validate_route(route, index, allowed)
        findings.extend(route_findings)
        if name:
            if name in seen:
                findings.append(Finding("ERROR", f"duplicate route name: {name}"))
            seen.add(name)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a lazy-skill-router routes JSON file.")
    parser.add_argument("config", help="Path to routes JSON.")
    args = parser.parse_args()

    config, load_findings = load_config(Path(args.config).expanduser())
    findings = load_findings if config is None else validate_config(config)
    for finding in findings:
        print(f"{finding.severity}: {finding.message}")
    if any(finding.severity == "ERROR" for finding in findings):
        return 1
    print("OK: route config is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
