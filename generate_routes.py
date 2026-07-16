from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_common import codex_home, load_json_object, write_json_atomic
from lazy_skill_router_host_catalog import effective_skill_names, load_host_catalog, reconcile_inventory
from lazy_skill_router_inventory import build_inventory_manifest
from sync_skills import scan_installed_skills, string_list

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATE_SOURCE = PROJECT_ROOT / "routes.template.json"


@dataclass(frozen=True)
class GenerationResult:
    config: dict[str, Any]
    skipped_routes: tuple[str, ...]


@dataclass(frozen=True)
class TemplateError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def installed_skill_names(codex_root: Path, agents_root: Path) -> set[str]:
    records = scan_installed_skills(codex_root, agents_root)
    manifest = build_inventory_manifest(records, codex_root, agents_root)
    host_catalog = load_host_catalog(codex_root / "lazy-skill-router" / "host-catalog.json")
    if host_catalog.state == "invalid":
        reason = ", ".join(host_catalog.reason_codes) or "invalid"
        raise TemplateError(f"cannot use invalid host catalog: {reason}")
    if host_catalog.state == "available":
        manifest = reconcile_inventory(manifest, host_catalog)
    return effective_skill_names(manifest)


def first_installed(candidates: tuple[str, ...], installed: set[str]) -> str | None:
    for candidate in candidates:
        if candidate in installed:
            return candidate
    return None


def installed_candidates(candidates: tuple[str, ...], installed: set[str]) -> tuple[str, ...]:
    return tuple(candidate for candidate in candidates if candidate in installed)


def route_name(raw_route: dict[str, Any], index: int) -> str:
    name = raw_route.get("name")
    if isinstance(name, str) and name:
        return name
    return f"#{index}"


def route_patterns(raw_route: dict[str, Any], name: str) -> list[Any]:
    value = raw_route.get("patterns")
    if isinstance(value, list):
        patterns = value
    elif isinstance(value, dict):
        patterns = [value]
    else:
        patterns = string_list(value)
    if not patterns:
        raise TemplateError(f"template route {name} must define non-empty patterns")
    return patterns


def default_verification(template: dict[str, Any], installed: set[str]) -> str | None:
    candidates = string_list(template.get("defaultVerificationCandidates"))
    if not candidates:
        candidates = string_list(template.get("defaultVerification"))
    return first_installed(candidates, installed)


def copy_top_level_fields(template: dict[str, Any], config: dict[str, Any]) -> None:
    for key in (
        "version",
        "policyVersion",
        "selection",
        "minConfidence",
        "activation",
        "logging",
        "display",
        "answerOnlyPatterns",
    ):
        if key in template:
            config[key] = template[key]


def copy_route_metadata(raw_route: dict[str, Any], generated: dict[str, Any]) -> None:
    for key in ("reason", "excludePatterns", "priority", "weight", "fallback", "activation"):
        if key in raw_route:
            generated[key] = raw_route[key]


def generate_route(raw_route: dict[str, Any], installed: set[str], index: int) -> dict[str, Any] | None:
    name = route_name(raw_route, index)
    primary = first_installed(string_list(raw_route.get("primaryCandidates")), installed)
    if primary is None:
        return None

    generated: dict[str, Any] = {
        "name": name,
        "primary": primary,
    }
    if "supportingCandidates" in raw_route:
        supporting = installed_candidates(string_list(raw_route.get("supportingCandidates")), installed)
        generated["supporting"] = [skill for skill in supporting if skill != primary]
    if "verificationCandidates" in raw_route:
        verification = first_installed(string_list(raw_route.get("verificationCandidates")), installed)
        generated["verification"] = verification if verification is not None else ""

    copy_route_metadata(raw_route, generated)
    generated["patterns"] = route_patterns(raw_route, name)
    return generated


def generate_config(template: dict[str, Any], installed: set[str]) -> GenerationResult:
    routes_value = template.get("routes")
    if not isinstance(routes_value, list):
        raise TemplateError("template routes must be a non-empty list")

    config: dict[str, Any] = {}
    copy_top_level_fields(template, config)

    default_skill = default_verification(template, installed)
    allowed_skills: set[str] = set()
    if default_skill is not None:
        config["defaultVerification"] = default_skill
        allowed_skills.add(default_skill)

    routes: list[dict[str, Any]] = []
    skipped: list[str] = []
    for index, raw_route in enumerate(routes_value):
        if not isinstance(raw_route, dict):
            raise TemplateError(f"template route #{index} must be an object")
        generated = generate_route(raw_route, installed, index)
        if generated is None:
            skipped.append(route_name(raw_route, index))
            continue
        routes.append(generated)
        allowed_skills.add(generated["primary"])
        allowed_skills.update(string_list(generated.get("supporting")))
        verification = generated.get("verification")
        if isinstance(verification, str) and verification:
            allowed_skills.add(verification)

    config["allowedSkills"] = sorted(allowed_skills)
    config["routes"] = routes
    return GenerationResult(config, tuple(skipped))


def generated_route_count(result: GenerationResult) -> int:
    routes = result.config.get("routes")
    return len(routes) if isinstance(routes, list) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a user-specific lazy-skill-router routes JSON file.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument("--template", default=str(TEMPLATE_SOURCE), help="Candidate-based route template JSON.")
    parser.add_argument(
        "--output", help="Generated routes JSON. Defaults to $CODEX_HOME/lazy-skill-router/routes.json."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print generated JSON without writing it.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    template_path = Path(args.template).expanduser()
    output_path = Path(args.output).expanduser() if args.output else codex_root / "lazy-skill-router" / "routes.json"
    output_managed_root = output_path.parent if args.output else codex_root

    try:
        result = generate_config(
            load_json_object(template_path, "template root"), installed_skill_names(codex_root, agents_root)
        )
    except (OSError, ValueError, TemplateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    route_count = generated_route_count(result)
    if route_count == 0:
        print("ERROR: generated 0 routes; no installed primary candidates matched", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(result.config, ensure_ascii=False, indent=2))
        print(f"generated {route_count} routes; skipped {len(result.skipped_routes)} routes", file=sys.stderr)
        return 0

    try:
        write_json_atomic(output_path, result.config, managed_root=output_managed_root)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot write generated routes: {exc}", file=sys.stderr)
        return 1
    print(f"generated {route_count} routes at {output_path}; skipped {len(result.skipped_routes)} routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
