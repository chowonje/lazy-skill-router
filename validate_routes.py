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

    patterns = strings(route.get("patterns"))
    if not patterns:
        findings.append(Finding("ERROR", f"route {name} must define non-empty string patterns"))
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


def validate_config(config: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    min_confidence = config.get("minConfidence", 0.55)
    if not isinstance(min_confidence, (int, float)) or not 0 <= float(min_confidence) <= 1:
        findings.append(Finding("ERROR", "minConfidence must be a number between 0 and 1"))

    allowed = set(strings(config.get("allowedSkills")))
    if "allowedSkills" in config and not allowed:
        findings.append(Finding("ERROR", "allowedSkills must be a non-empty list of strings when present"))

    findings.extend(check_patterns("answerOnlyPatterns", "answerOnlyPatterns", strings(config.get("answerOnlyPatterns"))))

    logging_config = config.get("logging", {})
    if logging_config and not isinstance(logging_config, dict):
        findings.append(Finding("ERROR", "logging must be an object when present"))

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
