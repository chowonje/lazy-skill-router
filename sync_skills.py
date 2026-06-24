from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_common import codex_home, load_json_object

PLUGIN_PROVIDER_SEGMENTS = {
    "cache",
    "openai-bundled",
    "openai-curated",
    "openai-curated-remote",
    "openai-primary-runtime",
    "plugins",
    "sisyphuslabs",
    "won-local",
}


@dataclass(frozen=True)
class SkillRecord:
    name: str
    path: Path


@dataclass(frozen=True)
class RouteReference:
    route: str
    field: str
    skill: str


@dataclass(frozen=True)
class SkillSyncReport:
    installed: tuple[SkillRecord, ...]
    allowed_skills: tuple[str, ...]
    route_references: tuple[RouteReference, ...]
    duplicate_installed: tuple[tuple[str, tuple[Path, ...]], ...]
    allowed_missing: tuple[str, ...]
    route_references_missing: tuple[RouteReference, ...]
    installed_not_allowlisted: tuple[str, ...]


def default_routes_path(home: Path, script_path: Path) -> Path:
    installed = home / "lazy-skill-router" / "routes.json"
    return installed if installed.is_file() else script_path.with_name("routes.default.json")


def string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def frontmatter_name(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:60]:
        stripped = line.strip()
        if stripped == "---":
            return None
        match = re.match(r"^name:\s*['\"]?([^'\"]+)['\"]?\s*$", stripped)
        if match:
            return match.group(1).strip()
    return None


def looks_like_version(segment: str) -> bool:
    if "+codex." in segment:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)+(?:[A-Za-z0-9.+_-]*)?", segment):
        return True
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", segment))


def plugin_prefix(path: Path) -> str | None:
    parts = path.parts
    if "plugins" not in parts or "cache" not in parts or "skills" not in parts:
        return None
    skills_index = parts.index("skills")
    for segment in reversed(parts[:skills_index]):
        if segment in PLUGIN_PROVIDER_SEGMENTS or looks_like_version(segment):
            continue
        return segment
    return None


def skill_name(path: Path) -> str:
    raw_name = frontmatter_name(path) or path.parent.name
    prefix = plugin_prefix(path)
    if prefix is None or raw_name.startswith(f"{prefix}:"):
        return raw_name
    return f"{prefix}:{raw_name}"


def skill_files(codex_root: Path, agents_root: Path) -> tuple[Path, ...]:
    roots = (
        codex_root / "skills",
        agents_root / "skills",
        codex_root / "plugins" / "cache",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(path for path in root.rglob("SKILL.md") if path.is_file())
    return tuple(sorted(files))


def scan_installed_skills(codex_root: Path, agents_root: Path) -> tuple[SkillRecord, ...]:
    records = [SkillRecord(skill_name(path), path) for path in skill_files(codex_root, agents_root)]
    return tuple(sorted(records, key=lambda item: (item.name, item.path.as_posix())))


def route_name(route: dict[str, Any], index: int) -> str:
    name = route.get("name")
    return name if isinstance(name, str) and name else f"#{index}"


def route_references(config: dict[str, Any]) -> tuple[RouteReference, ...]:
    references: list[RouteReference] = []
    default_verification = config.get("defaultVerification")
    if isinstance(default_verification, str) and default_verification:
        references.append(RouteReference("<default>", "defaultVerification", default_verification))

    routes = config.get("routes")
    if not isinstance(routes, list):
        return tuple(references)
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        name = route_name(route, index)
        primary = route.get("primary")
        if isinstance(primary, str) and primary:
            references.append(RouteReference(name, "primary", primary))
        for skill in string_list(route.get("supporting")):
            references.append(RouteReference(name, "supporting", skill))
        verification = route.get("verification")
        if isinstance(verification, str) and verification:
            references.append(RouteReference(name, "verification", verification))
    return tuple(references)


def duplicate_records(records: tuple[SkillRecord, ...]) -> tuple[tuple[str, tuple[Path, ...]], ...]:
    by_name: dict[str, list[Path]] = {}
    for record in records:
        by_name.setdefault(record.name, []).append(record.path)
    duplicates = [(name, tuple(paths)) for name, paths in by_name.items() if len(paths) > 1]
    return tuple(sorted(duplicates, key=lambda item: item[0]))


def build_report(config: dict[str, Any], installed: tuple[SkillRecord, ...]) -> SkillSyncReport:
    installed_names = {record.name for record in installed}
    allowed = tuple(sorted(set(string_list(config.get("allowedSkills")))))
    references = route_references(config)
    referenced_names = {reference.skill for reference in references}

    return SkillSyncReport(
        installed=installed,
        allowed_skills=allowed,
        route_references=references,
        duplicate_installed=duplicate_records(installed),
        allowed_missing=tuple(skill for skill in allowed if skill not in installed_names),
        route_references_missing=tuple(reference for reference in references if reference.skill not in installed_names),
        installed_not_allowlisted=tuple(sorted(installed_names - set(allowed) - referenced_names)),
    )


def append_section(lines: list[str], title: str, items: tuple[str, ...], empty: str) -> None:
    lines.append("")
    lines.append(title)
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append(f"- {empty}")


def format_report(report: SkillSyncReport, route_path: Path) -> str:
    lines = [
        "lazy-skill-router skill sync report",
        f"Routes: {route_path}",
        f"Installed skills: {len(report.installed)}",
        f"allowedSkills: {len(report.allowed_skills)}",
        f"Route references: {len(report.route_references)}",
    ]
    missing_routes = tuple(f"{ref.route}.{ref.field}: {ref.skill}" for ref in report.route_references_missing)
    duplicates = tuple(f"{name}: {len(paths)} copies" for name, paths in report.duplicate_installed)

    append_section(lines, "Missing installed skills used by allowedSkills", report.allowed_missing, "none")
    append_section(lines, "Route references to missing installed skills", missing_routes, "none")
    append_section(lines, "Installed skills not in allowedSkills or routes", report.installed_not_allowlisted, "none")
    append_section(lines, "Duplicate installed skill names", duplicates, "none")
    return "\n".join(lines)


def report_json(report: SkillSyncReport) -> dict[str, Any]:
    return {
        "installed": [{"name": record.name, "path": str(record.path)} for record in report.installed],
        "allowedSkills": list(report.allowed_skills),
        "routeReferences": [reference.__dict__ for reference in report.route_references],
        "duplicateInstalled": [
            {"name": name, "paths": [str(path) for path in paths]} for name, paths in report.duplicate_installed
        ],
        "allowedMissing": list(report.allowed_missing),
        "routeReferencesMissing": [reference.__dict__ for reference in report.route_references_missing],
        "installedNotAllowlisted": list(report.installed_not_allowlisted),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare installed Codex skills with lazy-skill-router routes.")
    parser.add_argument(
        "--codex-home", default=str(codex_home()), help="Codex home directory. Defaults to CODEX_HOME or ~/.codex."
    )
    parser.add_argument(
        "--agents-home", default=str(Path.home() / ".agents"), help="Agents home directory. Defaults to ~/.agents."
    )
    parser.add_argument(
        "--routes", help="Routes JSON. Defaults to installed routes.json or bundled routes.default.json."
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when configured skills are missing.")
    args = parser.parse_args()

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    route_path = Path(args.routes).expanduser() if args.routes else default_routes_path(codex_root, Path(__file__))
    report = build_report(load_json_object(route_path, "config root"), scan_installed_skills(codex_root, agents_root))

    if args.json:
        print(json.dumps(report_json(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report, route_path))

    if args.strict and (report.allowed_missing or report.route_references_missing):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
