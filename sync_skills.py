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

from lazy_skill_router_capability_index import (
    DEFAULT_CAPABILITY_INDEX_NAME,
    build_capability_index,
    load_capability_index,
)
from lazy_skill_router_common import (
    ConfinedPathIdentity,
    ConfinedStagedWrite,
    codex_home,
    confined_atomic_write_bytes,
    confined_discard_staged,
    confined_ensure_managed_root,
    confined_ensure_parent,
    confined_path_identity,
    confined_read_bytes,
    confined_read_regular_snapshot,
    confined_replace_staged,
    confined_rmdir,
    confined_stage_bytes,
    confined_unlink,
    ensure_safe_write_target,
    load_json_object,
)
from lazy_skill_router_host_catalog import effective_skill_names, load_host_catalog, reconcile_inventory
from lazy_skill_router_install_manifest import (
    load_install_manifest,
    physical_artifact_aliases,
    refresh_generated_artifact_digests,
    safe_relative_path,
    sha256_bytes,
)
from lazy_skill_router_inventory import (
    MAX_SKILL_DOCUMENT_BYTES,
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
    content_digest: str | None = None
    content_digest_reason: str | None = None
    snapshot_validated: bool = False


@dataclass(frozen=True)
class SkillFileCandidate:
    path: Path
    root_alias: str
    root: Path


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


@dataclass(frozen=True)
class JsonBundleItem:
    name: str
    path: Path
    data: dict[str, Any]


class SyncBundleRollbackError(OSError):
    def __init__(self, original: Exception, restoration_errors: tuple[Exception, ...]) -> None:
        self.original = original
        self.restoration_errors = restoration_errors
        details = "; ".join(str(error) for error in restoration_errors)
        super().__init__(f"sync failed and rollback was incomplete: {original}; restore errors: {details}")


def encoded_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def _stage_bytes(
    path: Path,
    content: bytes,
    mode: int,
    managed_root: Path,
    expected: ConfinedPathIdentity,
) -> ConfinedStagedWrite:
    return confined_stage_bytes(path, content, managed_root, expected, mode=mode)


def replace_staged_file(staged: ConfinedStagedWrite, destination: Path) -> ConfinedPathIdentity:
    """Replace one staged sync artifact; kept separate for failure injection tests."""

    if staged.path != destination.absolute():
        raise ValueError("staged sync destination mismatch")
    return confined_replace_staged(staged)


def _restore_bundle_snapshot(
    path: Path,
    content: bytes | None,
    mode: int,
    managed_root: Path,
    current: ConfinedPathIdentity,
) -> None:
    if content is None:
        if current.state != "missing":
            confined_unlink(path, managed_root, current)
        return
    confined_atomic_write_bytes(path, content, managed_root, current, mode=mode)


def apply_json_bundle(items: tuple[JsonBundleItem, ...], managed_root: Path) -> None:
    """Atomically apply a small same-filesystem JSON bundle.

    All targets are staged before the first replacement.  Expected exceptions
    restore every target's prior bytes; the caller orders the ownership
    manifest last so an abrupt process exit leaves detectable digest drift.
    """

    if not items:
        return
    paths = tuple(item.path.absolute() for item in items)
    if len(set(paths)) != len(paths):
        raise ValueError("sync bundle contains duplicate target paths")
    for path in paths:
        try:
            ensure_safe_write_target(path, managed_root)
        except ValueError as exc:
            raise ValueError(f"unsafe sync target {path}: {exc}") from exc

    created_parents: list[Path] = []
    created_managed_roots: tuple[Path, ...] = ()
    snapshots: dict[Path, tuple[bytes | None, int, ConfinedPathIdentity]] = {}
    staged_files: list[tuple[ConfinedStagedWrite, Path]] = []
    committed_identities: dict[Path, ConfinedPathIdentity] = {}
    try:
        created_managed_roots = confined_ensure_managed_root(managed_root)
        for path in paths:
            for parent in confined_ensure_parent(path, managed_root):
                if parent not in created_parents:
                    created_parents.append(parent)
        for path in paths:
            identity = confined_path_identity(path, managed_root)
            if identity.state == "available":
                if identity.kind != "file" or identity.mode is None:
                    raise ValueError(f"sync target is not a regular file: {path}")
                snapshots[path] = (
                    confined_read_bytes(path, managed_root, identity),
                    identity.mode,
                    identity,
                )
            else:
                snapshots[path] = (None, 0o600, identity)
        for item, path in zip(items, paths):
            _, mode, identity = snapshots[path]
            staged_files.append(
                (
                    _stage_bytes(
                        path,
                        encoded_json(item.data),
                        mode,
                        managed_root,
                        identity,
                    ),
                    path,
                )
            )

        try:
            for staged, destination in staged_files:
                try:
                    replaced = replace_staged_file(staged, destination)
                except Exception:
                    current = confined_path_identity(destination, managed_root)
                    if current == staged.temp_identity:
                        committed_identities[destination] = current
                    raise
                current = (
                    replaced
                    if isinstance(replaced, ConfinedPathIdentity)
                    else confined_path_identity(destination, managed_root)
                )
                if current != staged.temp_identity:
                    raise ValueError(f"sync replacement identity verification failed: {destination}")
                committed_identities[destination] = current
        except Exception as original:
            restoration_errors: list[Exception] = []
            for path in reversed(paths):
                content, mode, snapshot_identity = snapshots[path]
                try:
                    current = confined_path_identity(path, managed_root)
                    expected_current = committed_identities.get(path, snapshot_identity)
                    if current != expected_current:
                        raise ValueError(
                            f"sync rollback target identity changed; preserving concurrent replacement: {path}"
                        )
                    _restore_bundle_snapshot(path, content, mode, managed_root, current)
                except Exception as restore_error:
                    restoration_errors.append(restore_error)
            if restoration_errors:
                raise SyncBundleRollbackError(original, tuple(restoration_errors)) from original
            raise
    except Exception as original:
        cleanup_errors: list[Exception] = []
        for parent in reversed(created_parents):
            try:
                current = confined_path_identity(parent, managed_root)
                confined_rmdir(parent, managed_root, current)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        for created_root in reversed(created_managed_roots):
            try:
                root_parent = created_root.parent
                current = confined_path_identity(created_root, root_parent)
                confined_rmdir(created_root, root_parent, current)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            if isinstance(original, SyncBundleRollbackError):
                raise SyncBundleRollbackError(
                    original.original,
                    (*original.restoration_errors, *cleanup_errors),
                ) from original
            raise SyncBundleRollbackError(original, tuple(cleanup_errors)) from original
        raise
    finally:
        for staged, _ in staged_files:
            confined_discard_staged(staged)


def existing_json_for_revision(path: Path, revision: str) -> dict[str, Any] | None:
    try:
        data = load_json_object(path, "generated artifact")
    except (OSError, ValueError):
        return None
    return data if data.get("revision") == revision else None


def artifact_status(path: Path, data: dict[str, Any]) -> str:
    if path.is_symlink() or not path.is_file():
        return "updated"
    try:
        return "unchanged" if path.read_bytes() == encoded_json(data) else "updated"
    except OSError:
        return "updated"


def paths_alias(left: Path, right: Path) -> bool:
    if str(left.absolute()).casefold() == str(right.absolute()).casefold():
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def custom_sync_collision(
    manifest_path: Path,
    index_path: Path,
    codex_root: Path,
    default_manifest_path: Path,
    install_manifest_path: Path,
) -> str | None:
    custom_paths = (manifest_path.absolute(), index_path.absolute())
    if paths_alias(*custom_paths):
        return "custom inventory and capability index alias each other"

    owned: list[tuple[Path, str]] = [
        (default_manifest_path.absolute(), "file"),
        (default_manifest_path.with_name(DEFAULT_CAPABILITY_INDEX_NAME).absolute(), "file"),
        (install_manifest_path.absolute(), "file"),
    ]
    snapshot = load_install_manifest(install_manifest_path)
    if snapshot.state == "available":
        for artifact in snapshot.artifacts:
            relative = artifact.get("path")
            kind = artifact.get("kind")
            if isinstance(relative, str) and isinstance(kind, str):
                owned.append(((codex_root / relative).absolute(), kind))

    for custom in custom_paths:
        custom_key = str(custom).casefold()
        for owned_path, kind in owned:
            owned_key = str(owned_path).casefold()
            if paths_alias(custom, owned_path):
                return f"{custom} aliases install-owned artifact {owned_path}"
            if kind == "directory" and custom_key.startswith(owned_key.rstrip(os.sep).casefold() + os.sep):
                return f"{custom} is inside install-owned directory {owned_path}"
    return None


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


def frontmatter_metadata_bytes(content: bytes) -> tuple[str | None, str]:
    prefix = content[:MAX_SKILL_FRONTMATTER_BYTES]
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


def frontmatter_metadata(path: Path) -> tuple[str | None, str]:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(MAX_SKILL_FRONTMATTER_BYTES)
    except OSError:
        return None, ""
    return frontmatter_metadata_bytes(prefix)


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


def skill_name(path: Path, configured_name: str | None = None, *, metadata_loaded: bool = False) -> str:
    raw_name = configured_name or (None if metadata_loaded else frontmatter_name(path)) or path.parent.name
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
) -> tuple[tuple[SkillFileCandidate, ...], tuple[SkillScanIssue, ...]]:
    files: list[SkillFileCandidate] = []
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
                files.append(SkillFileCandidate(candidate, root_alias, root))
    return (
        tuple(sorted(files, key=lambda candidate: candidate.path.as_posix())),
        tuple(sorted(issues, key=lambda issue: (issue.root_alias, issue.relative_locator, issue.reason_code))),
    )


def skill_files(codex_root: Path, agents_root: Path) -> tuple[Path, ...]:
    files, _ = scan_skill_files(codex_root, agents_root)
    return tuple(candidate.path for candidate in files)


def scan_installed_skills_with_issues(codex_root: Path, agents_root: Path) -> SkillScanResult:
    files, issues = scan_skill_files(codex_root, agents_root)
    records: list[SkillRecord] = []
    scan_issues = list(issues)
    for candidate in files:
        path = candidate.path
        try:
            content, identity = confined_read_regular_snapshot(
                path,
                candidate.root,
                MAX_SKILL_DOCUMENT_BYTES,
            )
            if content is None:
                configured_name, description = None, ""
                digest = None
                digest_reason = "skill_document_too_large"
            else:
                configured_name, description = frontmatter_metadata_bytes(content)
                digest = identity.digest
                digest_reason = None
        except (OSError, ValueError):
            reason = SKILL_DOCUMENT_SYMLINK if path.is_symlink() else SKILL_DOCUMENT_RESOLUTION_FAILED
            scan_issues.append(SkillScanIssue(candidate.root_alias, relative_locator(path, candidate.root), reason))
            continue
        records.append(
            SkillRecord(
                skill_name(path, configured_name, metadata_loaded=True),
                path,
                description,
                digest,
                digest_reason,
                True,
            )
        )
    return SkillScanResult(
        tuple(sorted(records, key=lambda item: (item.name, item.path.as_posix()))),
        tuple(
            sorted(
                scan_issues,
                key=lambda issue: (issue.root_alias, issue.relative_locator, issue.reason_code),
            )
        ),
    )


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
    lines.append(
        f"Result: {'inventory/index bundle updated; routes preserved' if applied else 'read-only; no files changed'}"
    )
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
        default_manifest_path = codex_root / "lazy-skill-router" / "skills.manifest.json"
        manifest_path = (
            Path(args.manifest).expanduser()
            if args.manifest
            else Path(args.manifest_output).expanduser()
            if args.manifest_output
            else default_manifest_path
        )
        index_path = manifest_path.with_name(DEFAULT_CAPABILITY_INDEX_NAME)
        install_manifest_path = codex_root / "lazy-skill-router" / "install.manifest.json"
        installed_target = manifest_path.absolute() == default_manifest_path.absolute()
        if not installed_target:
            collision = custom_sync_collision(
                manifest_path,
                index_path,
                codex_root,
                default_manifest_path,
                install_manifest_path,
            )
            if collision is not None:
                print(f"ERROR: custom sync target collides with installed state: {collision}", file=sys.stderr)
                return 1
        previous = load_inventory_manifest(manifest_path)
        if previous.state == "invalid":
            reason = ", ".join(previous.reason_codes) or "invalid"
            print(f"ERROR: cannot sync from invalid inventory manifest: {reason}", file=sys.stderr)
            return 1
        diff = diff_inventory(previous, current_manifest)
        strict_failure = bool(report.route_references_missing or has_blocking_policy_findings(report))
        if args.apply and args.strict and strict_failure:
            print("ERROR: strict sync checks failed; no files changed", file=sys.stderr)
            return 1

        inventory_data = current_manifest
        if previous.state == "available" and previous.revision == current_manifest.get("revision"):
            reusable = existing_json_for_revision(manifest_path, str(current_manifest["revision"]))
            if reusable is not None:
                inventory_data = reusable

        index_snapshot = load_capability_index(index_path)
        fresh_index = build_capability_index(current_snapshot)
        index_data = fresh_index
        if index_snapshot.state == "available" and index_snapshot.revision == fresh_index.get("revision"):
            reusable_index = existing_json_for_revision(index_path, str(fresh_index["revision"]))
            if reusable_index is not None:
                index_data = reusable_index

        artifact_statuses: dict[str, dict[str, str]] = {
            "inventory": {
                "path": str(manifest_path),
                "status": artifact_status(manifest_path, inventory_data),
            },
            "capabilityIndex": {
                "path": str(index_path),
                "status": artifact_status(index_path, index_data),
            },
            "installManifest": {
                "path": str(install_manifest_path),
                "status": "not-applicable",
            },
        }
        install_manifest_data: dict[str, Any] | None = None
        if installed_target:
            install_snapshot = load_install_manifest(install_manifest_path)
            manifest_aliases = physical_artifact_aliases(install_snapshot, codex_root)
            if manifest_aliases:
                detail = ", ".join(f"{left} = {right}" for left, right in manifest_aliases)
                print(
                    f"ERROR: install ownership manifest contains physical aliases: {detail}",
                    file=sys.stderr,
                )
                return 1
            if args.apply and install_snapshot.state != "available":
                reason = ", ".join(install_snapshot.reason_codes) or install_snapshot.state
                print(
                    f"ERROR: default sync apply requires a valid install ownership manifest; reinstall first: {reason}",
                    file=sys.stderr,
                )
                return 1
            if install_snapshot.state == "available":
                try:
                    replacement_digests = {
                        safe_relative_path(codex_root, manifest_path): sha256_bytes(encoded_json(inventory_data)),
                        safe_relative_path(codex_root, index_path): sha256_bytes(encoded_json(index_data)),
                    }
                    install_manifest_data = refresh_generated_artifact_digests(
                        install_snapshot,
                        replacement_digests,
                    )
                except ValueError as exc:
                    print(f"ERROR: install ownership manifest cannot authorize sync: {exc}", file=sys.stderr)
                    return 1
                previous_digests = {
                    str(artifact.get("path")): str(artifact.get("digest")) for artifact in install_snapshot.artifacts
                }
                manifest_changed = any(
                    previous_digests.get(path) != digest for path, digest in replacement_digests.items()
                )
                if any(artifact_statuses[name]["status"] == "updated" for name in ("inventory", "capabilityIndex")):
                    manifest_changed = True
                artifact_statuses["installManifest"]["status"] = "updated" if manifest_changed else "unchanged"
            elif args.plan:
                artifact_statuses["installManifest"]["status"] = "updated"

        if args.apply:
            bundle_managed_root = codex_root if installed_target else manifest_path.absolute().parent
            preflight_paths = [manifest_path, index_path]
            if installed_target:
                preflight_paths.append(install_manifest_path)
            try:
                for target in preflight_paths:
                    ensure_safe_write_target(target, bundle_managed_root)
            except ValueError as exc:
                print(f"ERROR: sync bundle was not applied: unsafe sync target {target}: {exc}", file=sys.stderr)
                return 1
            bundle_items: list[JsonBundleItem] = []
            if artifact_statuses["inventory"]["status"] == "updated":
                bundle_items.append(JsonBundleItem("inventory", manifest_path, inventory_data))
            if artifact_statuses["capabilityIndex"]["status"] == "updated":
                bundle_items.append(JsonBundleItem("capabilityIndex", index_path, index_data))
            if (
                installed_target
                and artifact_statuses["installManifest"]["status"] == "updated"
                and install_manifest_data is not None
            ):
                bundle_items.append(JsonBundleItem("installManifest", install_manifest_path, install_manifest_data))
            try:
                apply_json_bundle(tuple(bundle_items), bundle_managed_root)
            except (OSError, ValueError) as exc:
                print(f"ERROR: sync bundle was not applied: {exc}", file=sys.stderr)
                return 1
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
                        "artifacts": artifact_statuses,
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
        if args.strict and strict_failure:
            return 1
        return 0

    if args.json:
        print(json.dumps(report_json(report, scan_result.issues), ensure_ascii=False, indent=2))
    else:
        print(format_report(report, route_path, scan_result.issues))

    if args.manifest_output:
        manifest_path = Path(args.manifest_output).expanduser()
        manifest_managed_root = manifest_path.absolute().parent
        try:
            ensure_safe_write_target(manifest_path, manifest_managed_root)
            apply_json_bundle(
                (JsonBundleItem("inventory", manifest_path, current_manifest),),
                manifest_managed_root,
            )
        except ValueError as exc:
            print(f"ERROR: unsafe manifest output target {manifest_path}: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"ERROR: manifest output was not written: {exc}", file=sys.stderr)
            return 1
        message = f"wrote skill inventory manifest {human_text(manifest_path)}"
        print(message, file=sys.stderr if args.json else sys.stdout)

    if args.strict and (report.route_references_missing or has_blocking_policy_findings(report)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
