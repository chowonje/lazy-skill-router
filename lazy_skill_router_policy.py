from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_common import (
    backup_file,
    codex_home,
    ensure_safe_write_target,
    load_json_object,
    write_json_atomic,
)
from lazy_skill_router_core import route_matches
from lazy_skill_router_host_catalog import load_host_catalog
from lazy_skill_router_inventory import InventorySnapshot, load_inventory_manifest
from lazy_skill_router_logging import (
    MEASUREMENT_EVENT_SCHEMA,
    append_measurement_event,
    config_revision,
    measurement_log_path,
    read_measurement_events,
)
from lazy_skill_router_policy_ir import parse_policy_config
from validate_routes import validate_config

POLICY_CONTEXT_SCHEMA = "lazy-skill-router.policy-context/v1"
POLICY_PROPOSAL_SCHEMA_V1 = "lazy-skill-router.policy-proposal/v1"
POLICY_PROPOSAL_SCHEMA_V2 = "lazy-skill-router.policy-proposal/v2"
POLICY_PROPOSAL_SCHEMA = POLICY_PROPOSAL_SCHEMA_V2
POLICY_COMPILE_SCHEMA = "lazy-skill-router.policy-compile/v1"
SUPPORTED_POLICY_PROPOSAL_SCHEMAS = frozenset({POLICY_PROPOSAL_SCHEMA_V1, POLICY_PROPOSAL_SCHEMA_V2})
V1_DEPRECATION_WARNING = f"proposal schema {POLICY_PROPOSAL_SCHEMA_V1} is deprecated; use {POLICY_PROPOSAL_SCHEMA_V2}"
V2_COMPILED_REASON = "Matched a validated app-LLM policy route."
MAX_ROUTES = 100
MAX_ROUTE_ID_LENGTH = 80
MAX_INTENT_LENGTH = 200
MAX_REASON_LENGTH = 500
MAX_PATTERN_LENGTH = 300
MAX_EXAMPLE_LENGTH = 500
MAX_PATTERNS_PER_ROUTE = 12
MAX_EXAMPLES_PER_ROUTE = 20
MAX_WHITESPACE_QUANTIFIERS = 1
MAX_SUPPORTING_SKILLS = 2
MIN_PROMOTION_SAMPLES = 5
MIN_HELPFUL_RATE = 0.8
POLICY_FEEDBACK_VERDICTS = frozenset({"helpful", "harmful", "irrelevant"})
POLICY_FEEDBACK_SOURCES = frozenset({"human", "objective"})
ROUTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
NESTED_QUANTIFIER_PATTERN = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
QUANTIFIED_ALTERNATION_PATTERN = re.compile(r"\([^)]*\|[^)]*\)[+*{]")
BACKREFERENCE_PATTERN = re.compile(r"\\[1-9]")
LOOKAROUND_TOKENS = ("(?=", "(?!", "(?<=", "(?<!")
ROUTER_MARKER_PATTERN = re.compile(r"</?lazy-skill-router\b", re.IGNORECASE)
CHARACTER_CLASS_PATTERN = re.compile(r"\[(?:\\.|[^\]])*\]")
ESCAPED_TOKEN_PATTERN = re.compile(r"\\.")
WHITESPACE_QUANTIFIER_PATTERN = re.compile(r"\\s[*+?]")
UNBOUNDED_QUANTIFIER_PATTERN = re.compile(r"[*+?{}]")


@dataclass(frozen=True)
class PolicyValidation:
    proposal: dict[str, Any] | None
    revision: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.proposal is not None and not self.errors and self.revision is not None


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def policy_proposal_revision(proposal: dict[str, Any]) -> str:
    canonical = {key: value for key, value in proposal.items() if key not in {"generatedAt", "revision"}}
    return "sha256:" + hashlib.sha256(canonical_json(canonical)).hexdigest()


def candidate_config_revision(config: dict[str, Any]) -> str:
    canonical = copy.deepcopy(config)
    compiler = canonical.get("policyCompiler")
    if isinstance(compiler, dict):
        compiler.pop("candidateConfigRevision", None)
    return "sha256:" + hashlib.sha256(canonical_json(canonical)).hexdigest()


def policy_proposal_warnings(schema: Any) -> tuple[str, ...]:
    return (V1_DEPRECATION_WARNING,) if schema == POLICY_PROPOSAL_SCHEMA_V1 else ()


def inventory_data(path: Path) -> tuple[dict[str, Any] | None, InventorySnapshot]:
    snapshot = load_inventory_manifest(path)
    if snapshot.state != "available":
        return None, snapshot
    try:
        data = load_json_object(path, "inventory root")
    except (OSError, ValueError):
        return None, snapshot
    return data, snapshot


def policy_context(data: dict[str, Any], snapshot: InventorySnapshot) -> dict[str, Any]:
    sources = data.get("sources")
    host_revision = sources.get("hostCatalogRevision") if isinstance(sources, dict) else None
    skills = []
    for skill in snapshot.skills:
        availability = skill.get("availability")
        status = availability.get("status") if isinstance(availability, dict) else "unknown"
        if status in {"disabled", "inactive", "unavailable"}:
            continue
        configured_name = skill.get("configured_name")
        canonical_id = skill.get("canonical_id")
        if not isinstance(configured_name, str) or not isinstance(canonical_id, str):
            continue
        if not SKILL_NAME_PATTERN.fullmatch(configured_name):
            continue
        provider = skill.get("provider")
        provider_type = provider.get("type", "unknown") if isinstance(provider, dict) else "unknown"
        skills.append(
            {
                "name": configured_name,
                "configuredName": configured_name,
                "canonicalId": canonical_id,
                "description": skill.get("description", ""),
                "source": skill.get("host_source", provider_type),
                "availability": status,
            }
        )
    skills.sort(key=lambda skill: (skill["name"], skill["canonicalId"]))
    return {
        "schema": POLICY_CONTEXT_SCHEMA,
        "inventoryRevision": snapshot.revision,
        "hostCatalogRevision": host_revision,
        "skills": skills,
        "proposalRules": {
            "schema": POLICY_PROPOSAL_SCHEMA,
            "preferredSchema": POLICY_PROPOSAL_SCHEMA,
            "acceptedSchemas": [POLICY_PROPOSAL_SCHEMA_V2, POLICY_PROPOSAL_SCHEMA_V1],
            "newRoutesStartInShadow": True,
            "maxSupportingSkills": MAX_SUPPORTING_SKILLS,
            "requirePositiveAndNegativeExamples": True,
            "supportsExplicitRouteRetirement": True,
            "runtimeLlmCalls": False,
        },
    }


def string_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return value
    return None


def validate_string(value: Any, field: str, maximum: int, errors: list[str], *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
        errors.append(f"{field} must be a string up to {maximum} characters")
        return ""
    normalized = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        errors.append(f"{field} must not contain control characters")
    if ROUTER_MARKER_PATTERN.search(normalized):
        errors.append(f"{field} must not contain a lazy-skill-router marker")
    return normalized


def validate_safe_identifier(value: Any, field: str, maximum: int, errors: list[str]) -> str:
    normalized = validate_string(value, field, maximum, errors)
    if normalized and not ROUTE_ID_PATTERN.fullmatch(normalized):
        errors.append(f"{field} contains unsupported characters: {normalized}")
    return normalized


def validate_pattern(
    value: Any,
    route_id: str,
    field: str,
    errors: list[str],
    *,
    allow_label: bool = True,
    require_safe_id: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"route {route_id} {field} entries must be objects")
        return None
    allowed_fields = {"id", "regex", "weight"}
    if allow_label:
        allowed_fields.add("label")
    unknown = set(value) - allowed_fields
    if unknown:
        errors.append(f"route {route_id} {field} contains unsupported fields: {', '.join(sorted(unknown))}")
    pattern_id = validate_string(value.get("id"), f"route {route_id} pattern id", MAX_ROUTE_ID_LENGTH, errors)
    if require_safe_id and pattern_id and not ROUTE_ID_PATTERN.fullmatch(pattern_id):
        errors.append(f"route {route_id} pattern id contains unsupported characters: {pattern_id}")
    regex = validate_string(value.get("regex"), f"route {route_id} regex", MAX_PATTERN_LENGTH, errors)
    label = (
        validate_string(value.get("label", regex), f"route {route_id} pattern label", MAX_REASON_LENGTH, errors)
        if allow_label
        else pattern_id
    )
    weight = value.get("weight", 1)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < float(weight) <= 3:
        errors.append(f"route {route_id} pattern weight must be greater than 0 and at most 3")
        weight = 1
    if regex:
        try:
            compiled = re.compile(regex, re.IGNORECASE)
        except re.error as exc:
            errors.append(f"route {route_id} regex is invalid: {exc}")
        else:
            if compiled.search("") is not None:
                errors.append(f"route {route_id} regex must not match an empty string")
            if NESTED_QUANTIFIER_PATTERN.search(regex):
                errors.append(f"route {route_id} regex contains a nested quantifier")
            if QUANTIFIED_ALTERNATION_PATTERN.search(regex):
                errors.append(f"route {route_id} regex contains a quantified alternation")
            if BACKREFERENCE_PATTERN.search(regex):
                errors.append(f"route {route_id} regex contains a backreference")
            if any(token in regex for token in LOOKAROUND_TOKENS):
                errors.append(f"route {route_id} regex contains lookaround")
            whitespace_quantifiers = WHITESPACE_QUANTIFIER_PATTERN.findall(regex)
            if len(whitespace_quantifiers) > MAX_WHITESPACE_QUANTIFIERS:
                errors.append(f"route {route_id} regex contains too many whitespace quantifiers")
            quantifier_probe = WHITESPACE_QUANTIFIER_PATTERN.sub("", regex)
            quantifier_probe = quantifier_probe.replace("(?:", "(")
            quantifier_probe = CHARACTER_CLASS_PATTERN.sub("", quantifier_probe)
            quantifier_probe = ESCAPED_TOKEN_PATTERN.sub("", quantifier_probe)
            if UNBOUNDED_QUANTIFIER_PATTERN.search(quantifier_probe):
                errors.append(f"route {route_id} regex contains an unsupported quantifier")
    return {"id": pattern_id, "regex": regex, "label": label, "weight": float(weight)}


def skill_is_resolvable(snapshot: InventorySnapshot, name: str) -> bool:
    skill = snapshot.resolve(name)
    if skill is None:
        return False
    availability = skill.get("availability")
    status = availability.get("status") if isinstance(availability, dict) else "unknown"
    return status not in {"disabled", "inactive", "unavailable"}


def resolve_skill_binding(
    value: Any,
    snapshot: InventorySnapshot,
    route_id: str,
    field: str,
    errors: list[str],
) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict):
        errors.append(f"route {route_id} {field} must be a canonical binding object")
        return "", {"canonicalId": "", "configuredName": ""}
    unknown = set(value) - {"canonicalId", "configuredName"}
    if unknown:
        errors.append(f"route {route_id} {field} contains unsupported fields: {', '.join(sorted(unknown))}")
    canonical_id = validate_string(
        value.get("canonicalId"),
        f"route {route_id} {field}.canonicalId",
        MAX_REASON_LENGTH,
        errors,
    )
    configured_name = validate_string(
        value.get("configuredName"),
        f"route {route_id} {field}.configuredName",
        MAX_ROUTE_ID_LENGTH,
        errors,
    )
    if configured_name and not SKILL_NAME_PATTERN.fullmatch(configured_name):
        errors.append(f"route {route_id} {field}.configuredName contains unsupported characters: {configured_name}")
    normalized_binding = {"canonicalId": canonical_id, "configuredName": configured_name}
    if not canonical_id or not configured_name:
        return configured_name, normalized_binding

    canonical_matches = tuple(skill for skill in snapshot.skills if skill.get("canonical_id") == canonical_id)
    name_matches = tuple(skill for skill in snapshot.skills if skill.get("configured_name") == configured_name)
    exact_matches = tuple(skill for skill in canonical_matches if skill.get("configured_name") == configured_name)
    binding = f"{canonical_id} / {configured_name}"
    if len(exact_matches) != 1:
        if len(exact_matches) > 1 or len(canonical_matches) > 1 or len(name_matches) > 1:
            errors.append(f"route {route_id} {field} binding is ambiguous in the current inventory: {binding}")
        elif canonical_matches or name_matches:
            errors.append(f"route {route_id} {field} canonicalId/configuredName mismatch: {binding}")
        else:
            errors.append(f"route {route_id} {field} binding is unavailable in the current inventory: {binding}")
        return configured_name, normalized_binding
    if len(canonical_matches) != 1 or len(name_matches) != 1:
        errors.append(f"route {route_id} {field} binding is ambiguous in the current inventory: {binding}")
        return configured_name, normalized_binding

    availability = exact_matches[0].get("availability")
    status = availability.get("status") if isinstance(availability, dict) else "unknown"
    if status in {"disabled", "inactive", "unavailable"}:
        errors.append(f"route {route_id} {field} binding is unavailable in the current inventory: {binding}")
    return configured_name, normalized_binding


def example_matches(route: dict[str, Any], example: str) -> bool:
    excluded = route.get("excludePatterns", [])
    if any(re.search(pattern["regex"], example, re.IGNORECASE) for pattern in excluded):
        return False
    return any(re.search(pattern["regex"], example, re.IGNORECASE) for pattern in route.get("patterns", []))


def normalize_route(
    value: Any,
    snapshot: InventorySnapshot,
    errors: list[str],
    seen_route_ids: set[str],
    seen_pattern_ids: set[str],
    proposal_schema: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append("proposal routes must be objects")
        return None
    is_v2 = proposal_schema == POLICY_PROPOSAL_SCHEMA_V2
    allowed_fields = (
        {
            "id",
            "intentId",
            "primary",
            "supporting",
            "verification",
            "patterns",
            "excludePatterns",
            "positiveExamples",
            "negativeExamples",
        }
        if is_v2
        else {
            "id",
            "intent",
            "primary",
            "supporting",
            "verification",
            "reason",
            "patterns",
            "excludePatterns",
            "positiveExamples",
            "negativeExamples",
        }
    )
    unknown = set(value) - allowed_fields
    if unknown:
        errors.append(f"proposal route contains unsupported fields: {', '.join(sorted(unknown))}")
    route_id = validate_string(value.get("id"), "route id", MAX_ROUTE_ID_LENGTH, errors)
    if route_id and not ROUTE_ID_PATTERN.fullmatch(route_id):
        errors.append(f"route id contains unsupported characters: {route_id}")
    if route_id in seen_route_ids:
        errors.append(f"duplicate route id: {route_id}")
    seen_route_ids.add(route_id)
    if is_v2:
        intent = validate_safe_identifier(
            value.get("intentId"),
            f"route {route_id} intentId",
            MAX_INTENT_LENGTH,
            errors,
        )
        primary, resolved_primary = resolve_skill_binding(
            value.get("primary"),
            snapshot,
            route_id,
            "primary",
            errors,
        )
        supporting_value = value.get("supporting", [])
        if not isinstance(supporting_value, list) or len(supporting_value) > MAX_SUPPORTING_SKILLS:
            errors.append(
                f"route {route_id} supporting must contain at most {MAX_SUPPORTING_SKILLS} canonical bindings"
            )
            supporting_value = []
        supporting_bindings = [
            resolve_skill_binding(item, snapshot, route_id, f"supporting[{index}]", errors)
            for index, item in enumerate(supporting_value)
        ]
        supporting = [name for name, _ in supporting_bindings]
        verification, resolved_verification = resolve_skill_binding(
            value.get("verification"),
            snapshot,
            route_id,
            "verification",
            errors,
        )
        reason = V2_COMPILED_REASON
        resolved_supporting_by_name = {
            name: binding for name, binding in supporting_bindings if name and name != primary
        }
    else:
        intent = validate_safe_identifier(value.get("intent"), f"route {route_id} intent", MAX_INTENT_LENGTH, errors)
        primary = validate_string(value.get("primary"), f"route {route_id} primary", MAX_ROUTE_ID_LENGTH, errors)
        supporting = string_list(value.get("supporting", []))
        if supporting is None or len(supporting) > MAX_SUPPORTING_SKILLS:
            errors.append(f"route {route_id} supporting must contain at most {MAX_SUPPORTING_SKILLS} skill names")
            supporting = []
        verification = validate_string(
            value.get("verification", ""),
            f"route {route_id} verification",
            MAX_ROUTE_ID_LENGTH,
            errors,
            required=False,
        )
        reason = validate_string(value.get("reason", ""), f"route {route_id} reason", MAX_REASON_LENGTH, errors)
        selected_skills = [primary, *supporting, *([verification] if verification else [])]
        resolved_by_name: dict[str, dict[str, str]] = {}
        for skill in selected_skills:
            if skill and not SKILL_NAME_PATTERN.fullmatch(skill):
                errors.append(f"route {route_id} skill name contains unsupported characters: {skill}")
            if skill and not skill_is_resolvable(snapshot, skill):
                errors.append(f"route {route_id} references unavailable or ambiguous skill: {skill}")
                continue
            resolved = snapshot.resolve(skill)
            canonical_id = resolved.get("canonical_id") if isinstance(resolved, dict) else None
            if skill and isinstance(canonical_id, str) and canonical_id:
                resolved_by_name[skill] = {"canonicalId": canonical_id, "configuredName": skill}

    patterns_value = value.get("patterns")
    if not isinstance(patterns_value, list) or not patterns_value or len(patterns_value) > MAX_PATTERNS_PER_ROUTE:
        errors.append(f"route {route_id} patterns must contain 1 to {MAX_PATTERNS_PER_ROUTE} entries")
        patterns_value = []
    patterns = [
        pattern
        for item in patterns_value
        if (
            pattern := validate_pattern(
                item,
                route_id,
                "patterns",
                errors,
                allow_label=not is_v2,
                require_safe_id=True,
            )
        )
        is not None
    ]
    excludes_value = value.get("excludePatterns", [])
    if not isinstance(excludes_value, list) or len(excludes_value) > MAX_PATTERNS_PER_ROUTE:
        errors.append(f"route {route_id} excludePatterns must contain at most {MAX_PATTERNS_PER_ROUTE} entries")
        excludes_value = []
    excludes = [
        pattern
        for item in excludes_value
        if (
            pattern := validate_pattern(
                item,
                route_id,
                "excludePatterns",
                errors,
                allow_label=not is_v2,
                require_safe_id=True,
            )
        )
        is not None
    ]
    for pattern in [*patterns, *excludes]:
        pattern_id = pattern["id"]
        if pattern_id in seen_pattern_ids:
            errors.append(f"duplicate pattern id: {pattern_id}")
        seen_pattern_ids.add(pattern_id)

    positive = string_list(value.get("positiveExamples"))
    negative = string_list(value.get("negativeExamples"))
    if not positive:
        errors.append(f"route {route_id} positiveExamples must be a non-empty list")
        positive = []
    if not negative:
        errors.append(f"route {route_id} negativeExamples must be a non-empty list")
        negative = []
    for field, examples in (("positiveExamples", positive), ("negativeExamples", negative)):
        if len(examples) > MAX_EXAMPLES_PER_ROUTE:
            errors.append(f"route {route_id} {field} must contain at most {MAX_EXAMPLES_PER_ROUTE} entries")
        if any(len(example) > MAX_EXAMPLE_LENGTH for example in examples):
            errors.append(f"route {route_id} {field} entries must be at most {MAX_EXAMPLE_LENGTH} characters")

    normalized_supporting = list(dict.fromkeys(skill for skill in supporting if skill and skill != primary))
    normalized = {
        "id": route_id,
        "intent": intent,
        "primary": primary,
        "supporting": normalized_supporting,
        "verification": verification,
        "reason": reason,
        "patterns": patterns,
        "excludePatterns": excludes,
        "positiveExamples": positive,
        "negativeExamples": negative,
    }
    if is_v2:
        normalized["resolvedBindings"] = {
            "primary": resolved_primary,
            "supporting": [resolved_supporting_by_name[name] for name in normalized_supporting],
            "verification": resolved_verification,
        }
    else:
        normalized["resolvedBindings"] = {
            "primary": resolved_by_name.get(primary),
            "supporting": [resolved_by_name[name] for name in normalized_supporting if name in resolved_by_name],
            "verification": resolved_by_name.get(verification) if verification else None,
        }
    for example in positive:
        if patterns and not example_matches(normalized, example):
            errors.append(f"route {route_id} positive example does not match its patterns")
    for example in negative:
        if patterns and example_matches(normalized, example):
            errors.append(f"route {route_id} negative example matches its patterns")
    return normalized


def normalize_retirement(value: Any, errors: list[str], seen_route_ids: set[str]) -> dict[str, str] | None:
    if not isinstance(value, dict):
        errors.append("proposal retireRoutes entries must be objects")
        return None
    unknown = set(value) - {"id", "reason"}
    if unknown:
        errors.append(f"retireRoutes entry contains unsupported fields: {', '.join(sorted(unknown))}")
    route_id = validate_string(value.get("id"), "retired route id", MAX_ROUTE_ID_LENGTH, errors)
    if route_id and not ROUTE_ID_PATTERN.fullmatch(route_id):
        errors.append(f"retired route id contains unsupported characters: {route_id}")
    if route_id in seen_route_ids:
        errors.append(f"duplicate retired route id: {route_id}")
    seen_route_ids.add(route_id)
    reason = validate_string(value.get("reason"), f"retired route {route_id} reason", MAX_REASON_LENGTH, errors)
    return {"id": route_id, "reason": reason}


def validate_policy_proposal(
    raw: dict[str, Any],
    inventory: dict[str, Any],
    snapshot: InventorySnapshot,
) -> PolicyValidation:
    errors: list[str] = []
    proposal_schema = raw.get("schema")
    warnings = policy_proposal_warnings(proposal_schema)
    allowed_top = {
        "schema",
        "revision",
        "generatedAt",
        "inventoryRevision",
        "hostCatalogRevision",
        "generatedBy",
        "routes",
        "retireRoutes",
    }
    unknown = set(raw) - allowed_top
    if unknown:
        errors.append(f"proposal contains unsupported fields: {', '.join(sorted(unknown))}")
    if proposal_schema not in SUPPORTED_POLICY_PROPOSAL_SCHEMAS:
        errors.append(f"proposal schema must be {POLICY_PROPOSAL_SCHEMA_V2} or {POLICY_PROPOSAL_SCHEMA_V1}")
    if raw.get("inventoryRevision") != snapshot.revision:
        errors.append("proposal inventoryRevision does not match the current inventory")
    sources = inventory.get("sources")
    expected_host_revision = sources.get("hostCatalogRevision") if isinstance(sources, dict) else None
    if raw.get("hostCatalogRevision") != expected_host_revision:
        errors.append("proposal hostCatalogRevision does not match the current inventory")

    generated_by = raw.get("generatedBy")
    if not isinstance(generated_by, dict):
        errors.append("proposal generatedBy must be an object")
        generated_by = {}
    elif set(generated_by) - {"host", "model", "promptVersion"}:
        errors.append("proposal generatedBy contains unsupported fields")
    for field in ("host", "model", "promptVersion"):
        validate_string(generated_by.get(field), f"generatedBy.{field}", 200, errors)

    routes_value = raw.get("routes", [])
    if not isinstance(routes_value, list) or len(routes_value) > MAX_ROUTES:
        errors.append(f"proposal routes must contain at most {MAX_ROUTES} routes")
        routes_value = []
    seen_route_ids: set[str] = set()
    seen_pattern_ids: set[str] = set()
    routes = [
        route
        for value in routes_value
        if (
            route := normalize_route(
                value,
                snapshot,
                errors,
                seen_route_ids,
                seen_pattern_ids,
                proposal_schema if isinstance(proposal_schema, str) else "",
            )
        )
        is not None
    ]
    retirements_value = raw.get("retireRoutes", [])
    if not isinstance(retirements_value, list) or len(retirements_value) > MAX_ROUTES:
        errors.append(f"proposal retireRoutes must contain at most {MAX_ROUTES} entries")
        retirements_value = []
    seen_retired_ids: set[str] = set()
    retirements = [
        retirement
        for value in retirements_value
        if (retirement := normalize_retirement(value, errors, seen_retired_ids)) is not None
    ]
    overlap = sorted(seen_route_ids & seen_retired_ids)
    if overlap:
        errors.append("routes cannot be added and retired in the same proposal: " + ", ".join(overlap))
    if not routes and not retirements:
        errors.append("proposal must add or retire at least one route")
    proposal = {
        "schema": proposal_schema if isinstance(proposal_schema, str) else "",
        "inventoryRevision": snapshot.revision,
        "hostCatalogRevision": expected_host_revision,
        "generatedBy": {
            "host": generated_by.get("host", ""),
            "model": generated_by.get("model", ""),
            "promptVersion": generated_by.get("promptVersion", ""),
        },
        "routes": routes,
        "retireRoutes": retirements,
    }
    if routes:
        route_config = {
            "allowedSkills": sorted(
                {
                    skill
                    for route in routes
                    for skill in [route["primary"], *route["supporting"], route["verification"]]
                    if skill
                }
            ),
            "routes": [compiled_route(route, "validation") for route in routes],
        }
        for route in route_config["routes"]:
            route["lifecycle"] = {"state": "active", "proposalRevision": "validation"}
        for route in routes:
            for example in route["positiveExamples"]:
                matches = route_matches(example, route_config)
                if not matches or matches[0].route.name != route["id"]:
                    selected = matches[0].route.name if matches else "none"
                    errors.append(
                        f"route {route['id']} positive example selects {selected}; refine overlapping patterns"
                    )
    revision = policy_proposal_revision(proposal)
    configured_revision = raw.get("revision")
    if configured_revision is not None and configured_revision != revision:
        errors.append("proposal revision does not match its canonical content")
    proposal["revision"] = revision
    return PolicyValidation(
        proposal if routes or retirements else None,
        revision,
        tuple(dict.fromkeys(errors)),
        warnings,
    )


def compiled_route(route: dict[str, Any], proposal_revision: str) -> dict[str, Any]:
    return {
        "name": route["id"],
        "intent": route["intent"],
        "primary": route["primary"],
        "supporting": route["supporting"],
        "verification": route["verification"],
        "reason": route["reason"],
        "patterns": route["patterns"],
        "excludePatterns": [pattern["regex"] for pattern in route["excludePatterns"]],
        "lifecycle": {"state": "shadow", "proposalRevision": proposal_revision},
    }


def route_identifier(route: dict[str, Any], schema_version: int) -> str | None:
    key = "id" if schema_version == 2 else "name"
    value = route.get(key)
    return value if isinstance(value, str) and value else None


def generated_capability(route_id: str, role: str, index: int) -> str:
    return f"generated.{route_id}.{role}.{index}"


def compiled_v2_binding(binding: Any, configured_name: str) -> str | dict[str, str]:
    if not isinstance(binding, dict):
        return configured_name
    canonical_id = binding.get("canonicalId")
    bound_name = binding.get("configuredName")
    if isinstance(canonical_id, str) and canonical_id and bound_name == configured_name:
        return {"skill": configured_name, "canonicalId": canonical_id}
    return configured_name


def compiled_route_v2(
    route: dict[str, Any],
    proposal_revision: str,
) -> tuple[dict[str, Any], dict[str, str | dict[str, str]]]:
    route_id = route["id"]
    resolved = route.get("resolvedBindings")
    resolved = resolved if isinstance(resolved, dict) else {}
    primary_binding = resolved.get("primary")
    supporting_bindings = resolved.get("supporting")
    supporting_bindings = supporting_bindings if isinstance(supporting_bindings, list) else []
    verification_binding = resolved.get("verification")

    bindings: dict[str, str | dict[str, str]] = {}
    primary_capability = generated_capability(route_id, "primary", 0)
    bindings[primary_capability] = compiled_v2_binding(primary_binding, route["primary"])
    supporting_capabilities = []
    for index, skill in enumerate(route["supporting"]):
        capability = generated_capability(route_id, "supporting", index)
        supporting_capabilities.append(capability)
        binding = supporting_bindings[index] if index < len(supporting_bindings) else None
        bindings[capability] = compiled_v2_binding(binding, skill)
    verification_capabilities = []
    if route["verification"]:
        capability = generated_capability(route_id, "verification", 0)
        verification_capabilities.append(capability)
        bindings[capability] = compiled_v2_binding(verification_binding, route["verification"])

    compiled = {
        "id": route_id,
        "intent": route["intent"],
        "capabilityRequirements": {
            "primary": [primary_capability],
            "supporting": supporting_capabilities,
            "verification": verification_capabilities,
        },
        "match": {
            "any": [
                {
                    "id": pattern["id"],
                    "regex": pattern["regex"],
                    "weight": pattern["weight"],
                }
                for pattern in route["patterns"]
            ],
            "none": [
                {
                    "id": pattern["id"],
                    "regex": pattern["regex"],
                    "weight": pattern["weight"],
                }
                for pattern in route["excludePatterns"]
            ],
        },
        "lifecycle": {"state": "shadow", "proposalRevision": proposal_revision},
    }
    return compiled, bindings


def compile_policy(
    base: dict[str, Any],
    proposal: dict[str, Any],
    proposal_revision: str,
) -> dict[str, Any]:
    parsed_base = parse_policy_config(base)
    schema_version = parsed_base.policy.schema_version
    if schema_version not in {1, 2}:
        raise ValueError("policy proposal compilation requires a schema v1 or v2 base route config")
    routes = base.get("routes")
    if not isinstance(routes, list):
        raise ValueError("base route config routes are invalid")
    existing_names = {
        route_id
        for route in routes
        if isinstance(route, dict) and (route_id := route_identifier(route, schema_version)) is not None
    }
    proposed_names = {route["id"] for route in proposal["routes"]}
    retirements = {retirement["id"]: retirement["reason"] for retirement in proposal.get("retireRoutes", [])}
    collisions = sorted(existing_names & proposed_names)
    if collisions:
        raise ValueError("proposal route ids collide with existing routes: " + ", ".join(collisions))
    missing_retirements = sorted(set(retirements) - existing_names)
    if missing_retirements:
        raise ValueError("proposal retires missing routes: " + ", ".join(missing_retirements))
    compiled = copy.deepcopy(base)
    compiled_base_routes = copy.deepcopy(routes)
    for route in compiled_base_routes:
        if not isinstance(route, dict):
            continue
        existing_route_id = route_identifier(route, schema_version)
        if existing_route_id not in retirements:
            continue
        lifecycle = route.get("lifecycle")
        previous_state = lifecycle.get("state", "active") if isinstance(lifecycle, dict) else "active"
        if previous_state == "disabled":
            raise ValueError(f"proposal retires an already disabled route: {existing_route_id}")
        route["lifecycle"] = {
            **(lifecycle if isinstance(lifecycle, dict) else {}),
            "state": "disabled",
            "previousState": previous_state,
            "retiredByProposal": proposal_revision,
            "retirementReason": retirements[str(existing_route_id)],
        }
    if schema_version == 1:
        compiled_routes = [compiled_route(route, proposal_revision) for route in proposal["routes"]]
    else:
        compiled_routes = []
        skill_bindings = compiled.get("skillBindings")
        if not isinstance(skill_bindings, dict):
            raise ValueError("schema v2 base route config skillBindings are invalid")
        for route in proposal["routes"]:
            compiled_route_value, new_bindings = compiled_route_v2(route, proposal_revision)
            collisions = sorted(set(skill_bindings) & set(new_bindings))
            if collisions:
                raise ValueError("generated capability ids collide with existing bindings: " + ", ".join(collisions))
            skill_bindings.update(new_bindings)
            compiled_routes.append(compiled_route_value)
    compiled["routes"] = [*compiled_base_routes, *compiled_routes]
    allowed = compiled.get("allowedSkills")
    if schema_version == 1 or isinstance(allowed, list):
        allowed_skills = set(allowed) if isinstance(allowed, list) else set()
        for route in proposal["routes"]:
            allowed_skills.add(route["primary"])
            allowed_skills.update(route["supporting"])
            if route["verification"]:
                allowed_skills.add(route["verification"])
        compiled["allowedSkills"] = sorted(allowed_skills)
    compiled["policyCompiler"] = {
        "schema": POLICY_COMPILE_SCHEMA,
        "proposalSchema": proposal.get("schema"),
        "proposalRevision": proposal_revision,
        "baseConfigRevision": config_revision(base),
        "inventoryRevision": proposal["inventoryRevision"],
        "hostCatalogRevision": proposal["hostCatalogRevision"],
        "generatedBy": proposal["generatedBy"],
        "retiredRoutes": sorted(retirements),
        "warnings": list(policy_proposal_warnings(proposal.get("schema"))),
    }
    resolved_bindings = {
        route["id"]: route["resolvedBindings"]
        for route in proposal["routes"]
        if isinstance(route.get("resolvedBindings"), dict)
    }
    if resolved_bindings:
        compiled["policyCompiler"]["resolvedBindings"] = resolved_bindings
    errors = [finding.message for finding in validate_config(compiled) if finding.severity == "ERROR"]
    if errors:
        raise ValueError("compiled policy failed route validation: " + "; ".join(errors))
    compiled["policyCompiler"]["candidateConfigRevision"] = candidate_config_revision(compiled)
    return compiled


def policy_compiler(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("policyCompiler")
    return value if isinstance(value, dict) else {}


def current_inventory_revisions(path: Path, host_catalog_path: Path | None = None) -> tuple[str, str | None]:
    data, snapshot = inventory_data(path)
    if data is None or snapshot.revision is None:
        reason = ", ".join(snapshot.reason_codes) or snapshot.state
        raise ValueError(f"inventory unavailable: {reason}")
    sources = data.get("sources")
    host_revision = sources.get("hostCatalogRevision") if isinstance(sources, dict) else None
    normalized_host_revision = host_revision if isinstance(host_revision, str) else None
    host_catalog = load_host_catalog(host_catalog_path or path.with_name("host-catalog.json"))
    if host_catalog.state == "invalid":
        reason = ", ".join(host_catalog.reason_codes) or "invalid"
        raise ValueError(f"host catalog unavailable: {reason}")
    current_host_revision = host_catalog.revision if host_catalog.state == "available" else None
    if current_host_revision != normalized_host_revision:
        raise ValueError("inventory is stale relative to the host catalog; run sync --apply")
    return snapshot.revision, normalized_host_revision


def validate_candidate_inventory(
    candidate: dict[str, Any],
    inventory_revision: str,
    host_catalog_revision: str | None,
) -> None:
    compiler = policy_compiler(candidate)
    if compiler.get("inventoryRevision") != inventory_revision:
        raise ValueError("candidate inventory revision is stale; run sync and compile again")
    if compiler.get("hostCatalogRevision") != host_catalog_revision:
        raise ValueError("candidate host catalog revision is stale; run sync and compile again")


def shadow_route(config: dict[str, Any], route_id: str) -> dict[str, Any] | None:
    routes = config.get("routes")
    if not isinstance(routes, list):
        return None
    schema_version = parse_policy_config(config).policy.schema_version
    for route in routes:
        if not isinstance(route, dict) or route_identifier(route, schema_version) != route_id:
            continue
        lifecycle = route.get("lifecycle")
        if isinstance(lifecycle, dict) and lifecycle.get("state") == "shadow":
            return route
    return None


def stage_policy(
    base: dict[str, Any],
    candidate: dict[str, Any],
    inventory_revision: str,
    host_catalog_revision: str | None,
) -> tuple[int, int, str | None]:
    validate_candidate_inventory(candidate, inventory_revision, host_catalog_revision)
    compiler = policy_compiler(candidate)
    expected_candidate_revision = compiler.get("candidateConfigRevision")
    if expected_candidate_revision != candidate_config_revision(candidate):
        raise ValueError("candidate config revision does not match its compiled content")
    expected_base_revision = compiler.get("baseConfigRevision")
    actual_base_revision = config_revision(base)
    if expected_base_revision != actual_base_revision:
        raise ValueError("candidate was compiled from a different base route revision")
    base_routes = base.get("routes")
    candidate_routes = candidate.get("routes")
    if not isinstance(base_routes, list) or not isinstance(candidate_routes, list):
        raise ValueError("base or candidate routes are invalid")
    if len(candidate_routes) < len(base_routes):
        raise ValueError("candidate removes existing routes")
    proposal_revision = compiler.get("proposalRevision")
    retired_count = 0
    for base_route, candidate_route in zip(base_routes, candidate_routes[: len(base_routes)]):
        if candidate_route == base_route:
            continue
        if not isinstance(base_route, dict) or not isinstance(candidate_route, dict):
            raise ValueError("candidate modifies or reorders existing routes")
        base_without_lifecycle = {key: value for key, value in base_route.items() if key != "lifecycle"}
        candidate_without_lifecycle = {key: value for key, value in candidate_route.items() if key != "lifecycle"}
        lifecycle = candidate_route.get("lifecycle")
        base_lifecycle = base_route.get("lifecycle")
        previous_state = base_lifecycle.get("state", "active") if isinstance(base_lifecycle, dict) else "active"
        if (
            candidate_without_lifecycle != base_without_lifecycle
            or not isinstance(lifecycle, dict)
            or lifecycle.get("state") != "disabled"
            or lifecycle.get("previousState") != previous_state
            or lifecycle.get("retiredByProposal") != proposal_revision
        ):
            raise ValueError("candidate modifies existing routes beyond an explicit retirement")
        retired_count += 1
    added_routes = candidate_routes[len(base_routes) :]
    if not added_routes and not retired_count:
        raise ValueError("candidate does not add or retire any routes")
    for route in added_routes:
        lifecycle = route.get("lifecycle") if isinstance(route, dict) else None
        if not isinstance(lifecycle, dict) or lifecycle.get("state") != "shadow":
            raise ValueError("candidate additions must all start in shadow state")
    schema_version = parse_policy_config(base).policy.schema_version
    mutable_fields = {"routes", "policyCompiler"}
    mutable_fields.add("skillBindings" if schema_version == 2 else "allowedSkills")
    if "allowedSkills" in base or "allowedSkills" in candidate:
        mutable_fields.add("allowedSkills")
    for key, value in base.items():
        if key not in mutable_fields and candidate.get(key) != value:
            raise ValueError(f"candidate modifies protected base field: {key}")
    base_allowed = base.get("allowedSkills")
    candidate_allowed = candidate.get("allowedSkills")
    if isinstance(base_allowed, list) and (
        not isinstance(candidate_allowed, list) or not set(base_allowed).issubset(set(candidate_allowed))
    ):
        raise ValueError("candidate removes existing allowed skills")
    if schema_version == 2:
        base_bindings = base.get("skillBindings")
        candidate_bindings = candidate.get("skillBindings")
        if not isinstance(base_bindings, dict) or not isinstance(candidate_bindings, dict):
            raise ValueError("schema v2 base or candidate skillBindings are invalid")
        if any(candidate_bindings.get(key) != value for key, value in base_bindings.items()):
            raise ValueError("candidate modifies or removes existing skill bindings")
    errors = [finding.message for finding in validate_config(candidate) if finding.severity == "ERROR"]
    if errors:
        raise ValueError("candidate route validation failed: " + "; ".join(errors))
    return len(added_routes), retired_count, proposal_revision if isinstance(proposal_revision, str) else None


def event_key(event: dict[str, Any]) -> tuple[str, str] | None:
    session_hash = event.get("sessionHash")
    turn_hash = event.get("turnHash")
    if not isinstance(session_hash, str) or not isinstance(turn_hash, str):
        return None
    return session_hash, turn_hash


def decision_has_shadow_route(event: dict[str, Any], route_id: str, proposal_revision: str) -> bool:
    revisions = event.get("shadowCandidateProposalRevisions")
    if isinstance(revisions, dict) and revisions.get(route_id) == proposal_revision:
        return True
    return (
        event.get("proposalRevision") == proposal_revision
        and isinstance(event.get("shadowCandidateRouteIds"), list)
        and route_id in event["shadowCandidateRouteIds"]
    )


def decision_supports_promotion(event: dict[str, Any], route_id: str, proposal_revision: str) -> bool:
    return (
        decision_has_shadow_route(event, route_id, proposal_revision)
        and isinstance(event.get("shadowWouldWinRouteIds"), list)
        and route_id in event["shadowWouldWinRouteIds"]
    )


def shadow_decision_contexts(
    events: list[dict[str, Any]],
    route_id: str,
    proposal_revision: str,
    current_config_revision: str,
) -> dict[tuple[str, str], str]:
    return {
        key: current_config_revision
        for event in events
        if event.get("schema") == MEASUREMENT_EVENT_SCHEMA
        and event.get("eventType") == "decision"
        and event.get("configRevision") == current_config_revision
        and decision_supports_promotion(event, route_id, proposal_revision)
        and (key := event_key(event)) is not None
    }


def latest_unlabeled_shadow_decision(
    events: list[dict[str, Any]],
    route_id: str,
    proposal_revision: str,
    current_config_revision: str,
) -> dict[str, Any] | None:
    labeled = {
        key
        for event in events
        if event.get("schema") == MEASUREMENT_EVENT_SCHEMA
        and event.get("eventType") == "policy-feedback"
        and event.get("proposalRevision") == proposal_revision
        and event.get("route") == route_id
        and event.get("decisionConfigRevision") == current_config_revision
        and (key := event_key(event)) is not None
    }
    for event in reversed(events):
        key = event_key(event)
        if (
            key is not None
            and key not in labeled
            and event.get("schema") == MEASUREMENT_EVENT_SCHEMA
            and event.get("eventType") == "decision"
            and event.get("configRevision") == current_config_revision
            and decision_supports_promotion(event, route_id, proposal_revision)
        ):
            return event
    return None


def promotion_gate(config: dict[str, Any], route_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    route = shadow_route(config, route_id)
    if route is None:
        raise ValueError(f"route is not a shadow candidate: {route_id}")
    lifecycle = route["lifecycle"]
    proposal_revision = lifecycle.get("proposalRevision")
    if not isinstance(proposal_revision, str) or not proposal_revision:
        raise ValueError(f"shadow route is missing proposal revision: {route_id}")
    current_config_revision = config_revision(config)
    observed = shadow_decision_contexts(events, route_id, proposal_revision, current_config_revision)
    grouped: dict[tuple[str, str], set[str]] = {}
    invalid = 0
    ignored_context = 0
    for event in events:
        if (
            event.get("schema") != MEASUREMENT_EVENT_SCHEMA
            or event.get("eventType") != "policy-feedback"
            or event.get("proposalRevision") != proposal_revision
            or event.get("route") != route_id
        ):
            continue
        key = event_key(event)
        verdict = event.get("verdict")
        source = event.get("feedbackSource")
        if event.get("decisionConfigRevision") != current_config_revision:
            ignored_context += 1
            continue
        if key not in observed or verdict not in POLICY_FEEDBACK_VERDICTS or source not in POLICY_FEEDBACK_SOURCES:
            invalid += 1
            continue
        grouped.setdefault(key, set()).add(str(verdict))
    conflicts = sum(len(verdicts) > 1 for verdicts in grouped.values())
    verdicts = [next(iter(values)) for values in grouped.values() if len(values) == 1]
    helpful = verdicts.count("helpful")
    harmful = verdicts.count("harmful")
    irrelevant = verdicts.count("irrelevant")
    total = len(verdicts)
    helpful_rate = round(helpful / total, 4) if total else None
    eligible = (
        total >= MIN_PROMOTION_SAMPLES
        and helpful_rate is not None
        and helpful_rate >= MIN_HELPFUL_RATE
        and harmful == 0
        and conflicts == 0
        and invalid == 0
    )
    return {
        "schema": "lazy-skill-router.policy-promotion-gate/v1",
        "routeId": route_id,
        "proposalRevision": proposal_revision,
        "eligible": eligible,
        "samples": total,
        "helpful": helpful,
        "irrelevant": irrelevant,
        "harmful": harmful,
        "helpfulRate": helpful_rate,
        "minimumSamples": MIN_PROMOTION_SAMPLES,
        "minimumHelpfulRate": MIN_HELPFUL_RATE,
        "conflicts": conflicts,
        "invalidFeedback": invalid,
        "ignoredContextFeedback": ignored_context,
        "observedShadowDecisions": len(observed),
        "configRevision": current_config_revision,
        "requiresApproval": True,
    }


def promoted_config(config: dict[str, Any], route_id: str, gate: dict[str, Any]) -> dict[str, Any]:
    promoted = copy.deepcopy(config)
    route = shadow_route(promoted, route_id)
    if route is None:
        raise ValueError(f"route is not a shadow candidate: {route_id}")
    route["lifecycle"] = {
        **route["lifecycle"],
        "state": "active",
        "promotionEvidence": {
            "samples": gate["samples"],
            "helpfulRate": gate["helpfulRate"],
            "harmful": gate["harmful"],
        },
    }
    compiler = policy_compiler(promoted)
    promotions = compiler.get("promotions")
    if not isinstance(promotions, list):
        promotions = []
    compiler["promotions"] = [*promotions, {"routeId": route_id, "gate": gate["schema"]}]
    promoted["policyCompiler"] = compiler
    errors = [finding.message for finding in validate_config(promoted) if finding.severity == "ERROR"]
    if errors:
        raise ValueError("promoted policy failed route validation: " + "; ".join(errors))
    return promoted


def load_and_validate(proposal_path: Path, inventory_path: Path) -> tuple[PolicyValidation, dict[str, Any] | None]:
    inventory, snapshot = inventory_data(inventory_path)
    if inventory is None:
        reason = ", ".join(snapshot.reason_codes) or snapshot.state
        return PolicyValidation(None, None, (f"inventory unavailable: {reason}",)), None
    try:
        proposal = load_json_object(proposal_path, "proposal root")
    except (OSError, ValueError) as exc:
        return PolicyValidation(None, None, (f"proposal unavailable: {exc}",)), inventory
    return validate_policy_proposal(proposal, inventory, snapshot), inventory


def default_path(root: Path, name: str) -> Path:
    return root / "lazy-skill-router" / name


def print_validation(validation: PolicyValidation, *, as_json: bool) -> None:
    payload = {
        "valid": validation.valid,
        "revision": validation.revision,
        "errors": list(validation.errors),
        "warnings": list(validation.warnings),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("lazy-skill-router policy validation")
    print(f"Valid: {str(validation.valid).lower()}")
    print(f"Revision: {validation.revision or 'unavailable'}")
    for warning in validation.warnings:
        print(f"Warning: {warning}")
    for error in validation.errors:
        print(f"- {error}")


def policy_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router policy", description="Prepare and compile app-LLM route policies."
    )
    parser.add_argument("command", choices=("context", "validate", "compile", "stage", "feedback", "promote"))
    parser.add_argument("--codex-home", default=str(codex_home()))
    parser.add_argument("--inventory")
    parser.add_argument(
        "--host-catalog",
        help="Host catalog used to verify a custom inventory path. Defaults to host-catalog.json beside the inventory.",
    )
    parser.add_argument("--proposal")
    parser.add_argument("--base-routes")
    parser.add_argument("--candidate")
    parser.add_argument("--output")
    parser.add_argument("--route-id")
    parser.add_argument("--verdict", choices=tuple(sorted(POLICY_FEEDBACK_VERDICTS)))
    parser.add_argument("--source", choices=tuple(sorted(POLICY_FEEDBACK_SOURCES)))
    parser.add_argument("--log")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.codex_home).expanduser()
    inventory_path = Path(args.inventory).expanduser() if args.inventory else default_path(root, "skills.manifest.json")
    host_catalog_path = Path(args.host_catalog).expanduser() if args.host_catalog else None

    if args.command == "context":
        data, snapshot = inventory_data(inventory_path)
        if data is None:
            reason = ", ".join(snapshot.reason_codes) or snapshot.state
            print(f"ERROR: inventory unavailable: {reason}", file=sys.stderr)
            return 1
        print(json.dumps(policy_context(data, snapshot), ensure_ascii=False, indent=2))
        return 0

    base_path = Path(args.base_routes).expanduser() if args.base_routes else default_path(root, "routes.json")
    candidate_path = (
        Path(args.candidate).expanduser() if args.candidate else default_path(root, "routes.candidate.json")
    )
    if args.command == "stage":
        try:
            base = load_json_object(base_path, "routes root")
            candidate = load_json_object(candidate_path, "candidate routes root")
            inventory_revision, host_catalog_revision = current_inventory_revisions(inventory_path, host_catalog_path)
            added_count, retired_count, proposal_revision = stage_policy(
                base,
                candidate,
                inventory_revision,
                host_catalog_revision,
            )
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print("lazy-skill-router policy stage plan")
        print(f"Shadow routes to add: {added_count}")
        print(f"Existing routes to retire: {retired_count}")
        schema_version = parse_policy_config(candidate).policy.schema_version
        added_ids = [
            route_identifier(route, schema_version)
            for route in candidate.get("routes", [])[len(base.get("routes", [])) :]
            if isinstance(route, dict)
        ]
        retired_ids = [
            route_identifier(route, schema_version)
            for route in candidate.get("routes", [])[: len(base.get("routes", []))]
            if isinstance(route, dict)
            and isinstance(route.get("lifecycle"), dict)
            and route["lifecycle"].get("retiredByProposal") == proposal_revision
        ]
        print(f"Shadow route ids: {', '.join(str(route_id) for route_id in added_ids) or 'none'}")
        print(f"Retired route ids: {', '.join(str(route_id) for route_id in retired_ids) or 'none'}")
        print(f"Proposal revision: {proposal_revision or 'unavailable'}")
        print(f"Active routes preserved: {len(base.get('routes', []))}")
        if not args.apply or args.dry_run:
            print("Result: dry-run; no files changed" if args.dry_run else "Result: read-only; no files changed")
            return 0
        try:
            ensure_safe_write_target(base_path, root)
        except ValueError as exc:
            print(f"ERROR: refusing unsafe policy write: {base_path}: {exc}", file=sys.stderr)
            return 1
        backup = backup_file(base_path, label="policy-stage")
        write_json_atomic(base_path, candidate)
        print(f"Staged shadow routes at {base_path}")
        if backup is not None:
            print(f"Backup: {backup}")
        return 0

    if args.command in {"feedback", "promote"}:
        config_path = Path(args.candidate).expanduser() if args.candidate else default_path(root, "routes.json")
        if not args.route_id:
            parser.error(f"--route-id is required for policy {args.command}")
        try:
            config = load_json_object(config_path, "candidate routes root")
            inventory_revision, host_catalog_revision = current_inventory_revisions(inventory_path, host_catalog_path)
            validate_candidate_inventory(config, inventory_revision, host_catalog_revision)
            route = shadow_route(config, args.route_id)
            if route is None:
                raise ValueError(f"route is not a shadow candidate: {args.route_id}")
            proposal_revision = route["lifecycle"].get("proposalRevision")
            if not isinstance(proposal_revision, str) or not proposal_revision:
                raise ValueError(f"shadow route is missing proposal revision: {args.route_id}")
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        config_for_log = {**config, "_loaded_from": str(config_path)}
        log_path = Path(args.log).expanduser() if args.log else measurement_log_path(config_for_log)
        events = read_measurement_events(log_path)

        if args.command == "feedback":
            if args.verdict is None or args.source is None:
                parser.error("--verdict and --source are required for policy feedback")
            current_config_revision = config_revision(config)
            decision = latest_unlabeled_shadow_decision(
                events,
                args.route_id,
                proposal_revision,
                current_config_revision,
            )
            if decision is None:
                print("ERROR: no unlabeled shadow decision found for this route", file=sys.stderr)
                return 1
            event = {
                "eventType": "policy-feedback",
                "sessionHash": decision.get("sessionHash"),
                "turnHash": decision.get("turnHash"),
                "route": args.route_id,
                "proposalRevision": proposal_revision,
                "decisionConfigRevision": decision.get("configRevision"),
                "verdict": args.verdict,
                "feedbackSource": args.source,
            }
            if args.dry_run:
                print(f"Would record {args.source} feedback for {args.route_id}: {args.verdict}")
                print("Result: dry-run; no events written")
                return 0
            if not append_measurement_event(event, config_for_log, explicit_path=log_path, force=True):
                print(f"ERROR: failed to append policy feedback to {log_path}", file=sys.stderr)
                return 1
            print(f"Recorded {args.source} feedback for {args.route_id}: {args.verdict}")
            return 0

        try:
            gate = promotion_gate(config, args.route_id, events)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(gate, ensure_ascii=False, indent=2))
        else:
            print("lazy-skill-router policy promotion gate")
            print(f"Route: {gate['routeId']}")
            print(f"Eligible: {str(gate['eligible']).lower()}")
            print(
                f"Evidence: {gate['samples']} samples, helpful {gate['helpful']}, "
                f"irrelevant {gate['irrelevant']}, harmful {gate['harmful']}"
            )
            print(f"Helpful rate: {gate['helpfulRate']}")
            print(
                f"Conflicts: {gate['conflicts']}; invalid feedback: {gate['invalidFeedback']}; "
                f"ignored other config: {gate['ignoredContextFeedback']}"
            )
        if not args.apply:
            return 0 if gate["eligible"] else 1
        if not args.approve:
            print("ERROR: --approve is required to activate a shadow route", file=sys.stderr)
            return 1
        if not gate["eligible"]:
            print("ERROR: promotion gate is not eligible", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"Would promote route to active: {args.route_id}")
            print("Result: dry-run; no files changed")
            return 0
        try:
            ensure_safe_write_target(config_path, root)
        except ValueError as exc:
            print(f"ERROR: refusing unsafe policy write: {config_path}: {exc}", file=sys.stderr)
            return 1
        try:
            promoted = promoted_config(config, args.route_id, gate)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        backup = backup_file(config_path, label="policy-promote")
        write_json_atomic(config_path, promoted)
        print(f"Promoted route to active: {args.route_id}")
        if backup is not None:
            print(f"Backup: {backup}")
        return 0

    proposal_path = Path(args.proposal).expanduser() if args.proposal else default_path(root, "policy.proposal.json")
    validation, _ = load_and_validate(proposal_path, inventory_path)
    if args.command == "validate":
        print_validation(validation, as_json=args.json)
        return 0 if validation.valid else 1
    if not validation.valid or validation.proposal is None or validation.revision is None:
        print_validation(validation, as_json=args.json)
        return 1

    output_path = Path(args.output).expanduser() if args.output else default_path(root, "routes.candidate.json")
    if output_path.resolve(strict=False) == base_path.resolve(strict=False):
        print("ERROR: compile output must not overwrite the active base routes", file=sys.stderr)
        return 1
    try:
        compiled = compile_policy(load_json_object(base_path, "routes root"), validation.proposal, validation.revision)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps(compiled, ensure_ascii=False, indent=2))
        return 0
    try:
        ensure_safe_write_target(output_path, root)
    except ValueError as exc:
        print(f"ERROR: refusing unsafe policy write: {output_path}: {exc}", file=sys.stderr)
        return 1
    write_json_atomic(output_path, compiled)
    for warning in validation.warnings:
        print(f"Warning: {warning}")
    print(
        f"Compiled {len(validation.proposal['routes'])} shadow routes and "
        f"{len(validation.proposal['retireRoutes'])} route retirements at {output_path}"
    )
    print(f"Proposal revision: {validation.revision}")
    print(f"Active routes preserved: {base_path}")
    return 0
