from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_common import codex_home, load_json_object, write_json_atomic
from lazy_skill_router_host_catalog import effective_skill_names, load_host_catalog, reconcile_inventory
from lazy_skill_router_inventory import (
    InventoryDiff,
    InventorySnapshot,
    build_inventory_manifest,
    diff_inventory,
    load_inventory_manifest,
)
from lazy_skill_router_policy_ir import (
    PolicyFinding,
    PolicyIR,
    PolicyReferenceResolution,
    parse_policy_config,
    policy_references,
    resolve_policy,
)

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

SKILL_DOCUMENT_SYMLINK = "skill_document_symlink"
SKILL_DOCUMENT_SYMLINKED_PARENT = "skill_document_symlinked_parent"
SKILL_DOCUMENT_OUTSIDE_ROOT = "skill_document_outside_root"
SKILL_DOCUMENT_RESOLUTION_FAILED = "skill_document_resolution_failed"
BLOCKING_POLICY_FINDING_CODES = frozenset(
    {
        "route_primary_capability_missing",
        "route_id_invalid",
        "route_intent_invalid",
        "route_pattern_id_invalid",
        "route_skill_name_invalid",
        "route_skill_binding_missing",
        "skill_binding_invalid",
        "skill_binding_name_invalid",
        "skill_bindings_invalid",
        "skill_canonical_id_mismatch",
        "skill_canonical_id_missing",
        "skill_ambiguous",
        "skill_inactive",
        "skill_missing",
        "skill_unavailable_or_ambiguous",
    }
)
MAX_SKILL_FRONTMATTER_BYTES = 64 * 1024
MAX_SKILL_FRONTMATTER_LINES = 200


@dataclass(frozen=True)
class SkillRecord:
    name: str
    path: Path
    description: str = ""


@dataclass(frozen=True)
class SkillScanIssue:
    root_alias: str
    relative_locator: str
    reason_code: str


@dataclass(frozen=True)
class SkillScanResult:
    records: tuple[SkillRecord, ...]
    issues: tuple[SkillScanIssue, ...]


@dataclass(frozen=True)
class RouteReference:
    route: str
    field: str
    skill: str


@dataclass(frozen=True)
class SkillSyncReport:
    policy_schema_version: int
    installed: tuple[SkillRecord, ...]
    allowed_skills: tuple[str, ...]
    route_references: tuple[RouteReference, ...]
    duplicate_installed: tuple[tuple[str, tuple[Path, ...]], ...]
    allowed_missing: tuple[str, ...]
    route_references_missing: tuple[RouteReference, ...]
    installed_not_allowlisted: tuple[str, ...]
    policy_findings: tuple[PolicyFinding, ...]
    resolved_references: tuple[PolicyReferenceResolution, ...]


def default_routes_path(home: Path, script_path: Path) -> Path:
    installed = home / "lazy-skill-router" / "routes.json"
    return installed if installed.is_file() else script_path.with_name("routes.default.json")


def string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def frontmatter_scalar(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def frontmatter_metadata(path: Path) -> tuple[str | None, str]:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(MAX_SKILL_FRONTMATTER_BYTES)
    except OSError:
        return None, ""
    raw_lines = prefix.splitlines()
    if not raw_lines or raw_lines[0].strip() != b"---":
        return None, ""
    closing_index = next(
        (
            index
            for index, line in enumerate(raw_lines[1:MAX_SKILL_FRONTMATTER_LINES], start=1)
            if line.strip() == b"---"
        ),
        None,
    )
    if closing_index is None:
        return None, ""
    try:
        lines = [line.decode("utf-8") for line in raw_lines[: closing_index + 1]]
    except UnicodeDecodeError:
        return None, ""
    frontmatter_lines = lines[1:closing_index]
    name: str | None = None
    description = ""
    index = 0
    while index < len(frontmatter_lines):
        line = frontmatter_lines[index]
        stripped = line.strip()
        match = re.match(r"^name:\s*['\"]?([^'\"]+)['\"]?\s*$", stripped)
        if match:
            name = match.group(1).strip()
            index += 1
            continue
        description_match = re.match(r"^description:\s*(.*)$", stripped)
        if not description_match:
            index += 1
            continue
        raw_description = description_match.group(1)
        if raw_description not in {"|", ">", "|-", ">-", "|+", ">+"}:
            description = frontmatter_scalar(raw_description)
            index += 1
            continue
        block_lines = []
        block_index = index + 1
        while block_index < len(frontmatter_lines):
            block_line = frontmatter_lines[block_index]
            if block_line and not block_line[0].isspace():
                break
            if block_line.strip():
                block_lines.append(block_line.strip())
            block_index += 1
        description = ("\n" if raw_description.startswith("|") else " ").join(block_lines)
        index = block_index
    return name, description[:4000]


def frontmatter_name(path: Path) -> str | None:
    name, _ = frontmatter_metadata(path)
    return name


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


def skill_name(path: Path, configured_name: str | None = None) -> str:
    raw_name = configured_name or frontmatter_name(path) or path.parent.name
    prefix = plugin_prefix(path)
    if prefix is None or raw_name.startswith(f"{prefix}:"):
        return raw_name
    return f"{prefix}:{raw_name}"


def scan_roots(codex_root: Path, agents_root: Path) -> tuple[tuple[str, Path], ...]:
    return (
        ("codex-skills", codex_root / "skills"),
        ("agents-skills", agents_root / "skills"),
        ("codex-plugin-cache", codex_root / "plugins" / "cache"),
    )


def path_is_within(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return ".." not in relative.parts


def relative_locator(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "<unresolved>"
    if ".." in relative.parts:
        return "<unresolved>"
    return relative.as_posix() or "."


def symlinked_parent(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            return True
    return False


def candidate_issue(path: Path, root_alias: str, root: Path) -> SkillScanIssue | None:
    locator = relative_locator(path, root)
    if not path_is_within(path, root):
        return SkillScanIssue(root_alias, locator, SKILL_DOCUMENT_OUTSIDE_ROOT)
    if path.is_symlink():
        return SkillScanIssue(root_alias, locator, SKILL_DOCUMENT_SYMLINK)
    if symlinked_parent(path, root):
        return SkillScanIssue(root_alias, locator, SKILL_DOCUMENT_SYMLINKED_PARENT)
    try:
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return SkillScanIssue(root_alias, locator, SKILL_DOCUMENT_RESOLUTION_FAILED)
    if not path_is_within(resolved_path, resolved_root):
        return SkillScanIssue(root_alias, locator, SKILL_DOCUMENT_OUTSIDE_ROOT)
    return None


def scan_skill_files(
    codex_root: Path,
    agents_root: Path,
) -> tuple[tuple[Path, ...], tuple[SkillScanIssue, ...]]:
    files: list[Path] = []
    issues: list[SkillScanIssue] = []
    for root_alias, root in scan_roots(codex_root, agents_root):
        if root.is_symlink():
            issues.append(SkillScanIssue(root_alias, ".", SKILL_DOCUMENT_SYMLINKED_PARENT))
            continue
        if not root.is_dir():
            continue
        for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            directory_path = Path(directory)
            visible_directories: list[str] = []
            for name in sorted(directory_names):
                if name.startswith("."):
                    continue
                child = directory_path / name
                if child.is_symlink():
                    reason = SKILL_DOCUMENT_SYMLINK if name == "SKILL.md" else SKILL_DOCUMENT_SYMLINKED_PARENT
                    issues.append(SkillScanIssue(root_alias, relative_locator(child, root), reason))
                    continue
                visible_directories.append(name)
            directory_names[:] = visible_directories

            if "SKILL.md" not in file_names:
                continue
            candidate = directory_path / "SKILL.md"
            issue = candidate_issue(candidate, root_alias, root)
            if issue is not None:
                issues.append(issue)
            elif candidate.is_file():
                files.append(candidate)
    return (
        tuple(sorted(files)),
        tuple(sorted(issues, key=lambda issue: (issue.root_alias, issue.relative_locator, issue.reason_code))),
    )


def skill_files(codex_root: Path, agents_root: Path) -> tuple[Path, ...]:
    files, _ = scan_skill_files(codex_root, agents_root)
    return files


def scan_installed_skills_with_issues(codex_root: Path, agents_root: Path) -> SkillScanResult:
    files, issues = scan_skill_files(codex_root, agents_root)
    records = []
    for path in files:
        configured_name, description = frontmatter_metadata(path)
        records.append(SkillRecord(skill_name(path, configured_name), path, description))
    return SkillScanResult(tuple(sorted(records, key=lambda item: (item.name, item.path.as_posix()))), issues)


def scan_installed_skills(codex_root: Path, agents_root: Path) -> tuple[SkillRecord, ...]:
    return scan_installed_skills_with_issues(codex_root, agents_root).records


def route_references(config: dict[str, Any] | PolicyIR) -> tuple[RouteReference, ...]:
    policy = config if isinstance(config, PolicyIR) else parse_policy_config(config).policy
    return tuple(
        RouteReference(reference.route_id, reference.field, reference.skill.configured_name)
        for reference in policy_references(policy)
    )


def duplicate_records(records: tuple[SkillRecord, ...]) -> tuple[tuple[str, tuple[Path, ...]], ...]:
    by_name: dict[str, list[Path]] = {}
    for record in records:
        by_name.setdefault(record.name, []).append(record.path)
    duplicates = [(name, tuple(paths)) for name, paths in by_name.items() if len(paths) > 1]
    return tuple(sorted(duplicates, key=lambda item: item[0]))


def build_report_for_names(
    config: dict[str, Any],
    installed: tuple[SkillRecord, ...],
    installed_names: set[str],
    inventory: InventorySnapshot | None = None,
) -> SkillSyncReport:
    allowed = tuple(sorted(set(string_list(config.get("allowedSkills")))))
    parsed = parse_policy_config(config)
    references = route_references(parsed.policy)
    referenced_names = {reference.skill for reference in references}
    policy_findings = list(parsed.findings)
    resolved_references: tuple[PolicyReferenceResolution, ...] = ()
    if inventory is not None:
        resolved = resolve_policy(parsed.policy, inventory)
        policy_findings.extend(resolved.findings)
        resolved_references = resolved.references

    return SkillSyncReport(
        policy_schema_version=parsed.policy.schema_version,
        installed=installed,
        allowed_skills=allowed,
        route_references=references,
        duplicate_installed=duplicate_records(installed),
        allowed_missing=tuple(skill for skill in allowed if skill not in installed_names),
        route_references_missing=tuple(reference for reference in references if reference.skill not in installed_names),
        installed_not_allowlisted=tuple(sorted(installed_names - set(allowed) - referenced_names)),
        policy_findings=tuple(dict.fromkeys(policy_findings)),
        resolved_references=resolved_references,
    )


def build_report(config: dict[str, Any], installed: tuple[SkillRecord, ...]) -> SkillSyncReport:
    return build_report_for_names(config, installed, {record.name for record in installed})


def human_text(value: Any) -> str:
    escaped = []
    for character in str(value):
        if unicodedata.category(character).startswith("C"):
            codepoint = ord(character)
            escaped.append(f"\\u{codepoint:04X}" if codepoint <= 0xFFFF else f"\\U{codepoint:08X}")
        else:
            escaped.append(character)
    return "".join(escaped)


def append_section(lines: list[str], title: str, items: tuple[str, ...], empty: str) -> None:
    lines.append("")
    lines.append(title)
    if items:
        lines.extend(f"- {human_text(item)}" for item in items)
    else:
        lines.append(f"- {empty}")


def scan_issue_json(issue: SkillScanIssue) -> dict[str, str]:
    return {
        "rootAlias": issue.root_alias,
        "relativeLocator": issue.relative_locator,
        "reasonCode": issue.reason_code,
    }


def resolved_reference_json(reference: PolicyReferenceResolution) -> dict[str, Any]:
    return {
        "route": reference.route_id,
        "field": reference.field,
        "lifecycle": reference.lifecycle_state,
        "configuredName": reference.configured_name,
        "requestedCanonicalId": reference.requested_canonical_id,
        "resolvedCanonicalId": reference.resolved_canonical_id,
        "status": reference.status,
    }


def append_scan_warning(lines: list[str], issues: tuple[SkillScanIssue, ...]) -> None:
    if not issues:
        return
    lines.append("")
    lines.append("WARNING: Inventory scan issues")
    lines.extend(
        "- " + human_text(f"{issue.root_alias}:{issue.relative_locator} ({issue.reason_code})") for issue in issues
    )


def has_blocking_policy_findings(report: SkillSyncReport) -> bool:
    return any(
        finding.severity == "ERROR" and finding.code in BLOCKING_POLICY_FINDING_CODES
        for finding in report.policy_findings
    )


def format_report(
    report: SkillSyncReport,
    route_path: Path,
    scan_issues: tuple[SkillScanIssue, ...] = (),
) -> str:
    lines = [
        "lazy-skill-router skill sync report",
        f"Routes: {human_text(route_path)}",
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
    append_section(
        lines,
        "Policy resolution findings",
        tuple(f"{finding.severity}: {finding.message}" for finding in report.policy_findings),
        "none",
    )
    append_scan_warning(lines, scan_issues)
    return "\n".join(lines)


def report_json(report: SkillSyncReport, scan_issues: tuple[SkillScanIssue, ...] = ()) -> dict[str, Any]:
    return {
        "policySchemaVersion": report.policy_schema_version,
        "installed": [{"name": record.name, "path": str(record.path)} for record in report.installed],
        "allowedSkills": list(report.allowed_skills),
        "routeReferences": [reference.__dict__ for reference in report.route_references],
        "duplicateInstalled": [
            {"name": name, "paths": [str(path) for path in paths]} for name, paths in report.duplicate_installed
        ],
        "allowedMissing": list(report.allowed_missing),
        "routeReferencesMissing": [reference.__dict__ for reference in report.route_references_missing],
        "installedNotAllowlisted": list(report.installed_not_allowlisted),
        "policyFindings": [finding.__dict__ for finding in report.policy_findings],
        "resolvedReferences": [resolved_reference_json(reference) for reference in report.resolved_references],
        "scanIssues": [scan_issue_json(issue) for issue in scan_issues],
    }


def diff_json(diff: InventoryDiff) -> dict[str, Any]:
    return {
        "previousState": diff.previous_state,
        "previousRevision": diff.previous_revision,
        "currentRevision": diff.current_revision,
        "hasChanges": diff.has_changes,
        "added": list(diff.added),
        "removed": list(diff.removed),
        "changed": list(diff.changed),
        "ambiguousNames": list(diff.ambiguous_names),
    }


def redacted_route_report(report: SkillSyncReport) -> dict[str, Any]:
    return {
        "policySchemaVersion": report.policy_schema_version,
        "allowedMissing": list(report.allowed_missing),
        "routeReferencesMissing": [reference.__dict__ for reference in report.route_references_missing],
        "installedNotAllowlisted": list(report.installed_not_allowlisted),
        "duplicateInstalled": [{"name": name, "copies": len(paths)} for name, paths in report.duplicate_installed],
        "policyFindings": [finding.__dict__ for finding in report.policy_findings],
        "resolvedReferences": [resolved_reference_json(reference) for reference in report.resolved_references],
    }


def change_names(items: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    return tuple(str(item.get("configured_name")) for item in items if isinstance(item.get("configured_name"), str))


def format_sync_plan(
    diff: InventoryDiff,
    report: SkillSyncReport,
    manifest_path: Path,
    *,
    applied: bool,
    scan_issues: tuple[SkillScanIssue, ...] = (),
) -> str:
    lines = [
        "lazy-skill-router sync plan",
        f"Manifest: {human_text(manifest_path)}",
        f"Previous inventory: {human_text(diff.previous_revision or diff.previous_state)}",
        f"Current inventory: {human_text(diff.current_revision)}",
        (
            f"Changes: {len(diff.added)} added, {len(diff.removed)} removed, "
            f"{len(diff.changed)} changed, {len(diff.ambiguous_names)} ambiguous names"
        ),
    ]
    append_section(lines, "Added skills", change_names(diff.added), "none")
    append_section(lines, "Removed skills", change_names(diff.removed), "none")
    append_section(
        lines,
        "Changed skills",
        tuple(f"{item.get('configured_name')}: {', '.join(item.get('fields', []))}" for item in diff.changed),
        "none",
    )
    append_section(lines, "Ambiguous skill names", diff.ambiguous_names, "none")
    missing_routes = tuple(f"{ref.route}.{ref.field}: {ref.skill}" for ref in report.route_references_missing)
    append_section(lines, "Route references needing attention", missing_routes, "none")
    append_section(
        lines,
        "Policy resolution findings",
        tuple(f"{finding.severity}: {finding.message}" for finding in report.policy_findings),
        "none",
    )
    append_section(
        lines,
        "Installed skills not represented by current routes",
        report.installed_not_allowlisted,
        "none",
    )
    append_scan_warning(lines, scan_issues)
    lines.append("")
    lines.append(f"Result: {'manifest updated; routes preserved' if applied else 'read-only; no files changed'}")
    return "\n".join(lines)


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
    parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero when active route bindings are unresolved."
    )
    parser.add_argument(
        "--manifest-output",
        help="Write a path-redacted, version-stamped skill inventory manifest.",
    )
    parser.add_argument(
        "--manifest",
        help="Inventory manifest used for sync diff and apply. Defaults to the installed skills.manifest.json.",
    )
    parser.add_argument(
        "--host-catalog",
        help="App-provided host skill catalog. Defaults to the installed host-catalog.json when present.",
    )
    sync_mode = parser.add_mutually_exclusive_group()
    sync_mode.add_argument("--plan", action="store_true", help="Show inventory drift without writing files.")
    sync_mode.add_argument(
        "--apply", action="store_true", help="Write the current inventory manifest; routes are preserved."
    )
    args = parser.parse_args()

    if args.manifest and args.manifest_output:
        manifest_path = Path(args.manifest).expanduser().resolve(strict=False)
        output_path = Path(args.manifest_output).expanduser().resolve(strict=False)
        if manifest_path != output_path:
            parser.error("--manifest and --manifest-output must refer to the same path when used together")

    codex_root = Path(args.codex_home).expanduser()
    agents_root = Path(args.agents_home).expanduser()
    route_path = Path(args.routes).expanduser() if args.routes else default_routes_path(codex_root, Path(__file__))
    scan_result = scan_installed_skills_with_issues(codex_root, agents_root)
    installed = scan_result.records
    filesystem_manifest = build_inventory_manifest(installed, codex_root, agents_root)
    host_catalog_path = (
        Path(args.host_catalog).expanduser()
        if args.host_catalog
        else codex_root / "lazy-skill-router" / "host-catalog.json"
    )
    host_catalog = load_host_catalog(host_catalog_path)
    if args.host_catalog and host_catalog.state == "missing":
        print(f"ERROR: host catalog not found: {host_catalog_path}", file=sys.stderr)
        return 1
    if host_catalog.state == "invalid":
        reason = ", ".join(host_catalog.reason_codes) or "invalid"
        print(f"ERROR: cannot use invalid host catalog: {reason}", file=sys.stderr)
        return 1
    current_manifest = (
        reconcile_inventory(filesystem_manifest, host_catalog)
        if host_catalog.state == "available"
        else filesystem_manifest
    )
    installed_names = (
        effective_skill_names(current_manifest)
        if host_catalog.state == "available"
        else {record.name for record in installed}
    )
    current_snapshot = InventorySnapshot(
        "available",
        str(current_manifest["revision"]),
        tuple(current_manifest["skills"]),
    )
    report = build_report_for_names(
        load_json_object(route_path, "config root"),
        installed,
        installed_names,
        current_snapshot,
    )

    if args.plan or args.apply:
        manifest_path = (
            Path(args.manifest).expanduser()
            if args.manifest
            else Path(args.manifest_output).expanduser()
            if args.manifest_output
            else codex_root / "lazy-skill-router" / "skills.manifest.json"
        )
        previous = load_inventory_manifest(manifest_path)
        if previous.state == "invalid":
            reason = ", ".join(previous.reason_codes) or "invalid"
            print(f"ERROR: cannot sync from invalid inventory manifest: {reason}", file=sys.stderr)
            return 1
        diff = diff_inventory(previous, current_manifest)
        if args.apply:
            if manifest_path.is_symlink():
                print(f"ERROR: refusing to overwrite symlink manifest: {manifest_path}", file=sys.stderr)
                return 1
            write_json_atomic(manifest_path, current_manifest)
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "lazy-skill-router.sync-plan/v1",
                        "mode": "apply" if args.apply else "plan",
                        "inventory": diff_json(diff),
                        "routes": redacted_route_report(report),
                        "hostCatalog": {
                            "state": host_catalog.state,
                            "revision": host_catalog.revision,
                            "complete": host_catalog.complete,
                        },
                        "scanIssues": [scan_issue_json(issue) for issue in scan_result.issues],
                        "routesPreserved": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                format_sync_plan(
                    diff,
                    report,
                    manifest_path,
                    applied=args.apply,
                    scan_issues=scan_result.issues,
                )
            )
        if args.strict and (report.route_references_missing or has_blocking_policy_findings(report)):
            return 1
        return 0

    if args.json:
        print(json.dumps(report_json(report, scan_result.issues), ensure_ascii=False, indent=2))
    else:
        print(format_report(report, route_path, scan_result.issues))

    if args.manifest_output:
        manifest_path = Path(args.manifest_output).expanduser()
        write_json_atomic(manifest_path, current_manifest)
        message = f"wrote skill inventory manifest {human_text(manifest_path)}"
        print(message, file=sys.stderr if args.json else sys.stdout)

    if args.strict and (report.route_references_missing or has_blocking_policy_findings(report)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
