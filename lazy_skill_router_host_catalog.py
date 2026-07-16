from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lazy_skill_router_common import codex_home, ensure_safe_write_target, load_json_object, write_json_atomic
from lazy_skill_router_inventory import INVENTORY_SCHEMA, inventory_revision

HOST_CATALOG_SCHEMA = "lazy-skill-router.host-skill-catalog/v1"
HOST_SKILL_SOURCES = frozenset({"admin", "plugin", "repository", "system", "user", "unknown"})
MAX_SKILL_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 4000
MAX_ALIAS_COUNT = 8
MAX_CAPABILITY_COUNT = 16
MAX_METADATA_VALUE_LENGTH = 160


@dataclass(frozen=True)
class HostCatalogSnapshot:
    state: str
    revision: str | None
    host: str | None
    complete: bool
    skills: tuple[dict[str, Any], ...]
    reason_codes: tuple[str, ...] = ()


def canonical_segment(value: str) -> str:
    return quote(value, safe="-._~")


def normalized_metadata_values(value: Any, field: str, *, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"host catalog skill {field} is invalid")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"host catalog skill {field} is invalid")
        stripped = item.strip()
        if not stripped or len(stripped) > MAX_METADATA_VALUE_LENGTH:
            raise ValueError(f"host catalog skill {field} is invalid")
        normalized.append(stripped)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"host catalog skill {field} contains duplicates")
    return normalized


def normalized_host_skill(value: dict[str, Any]) -> dict[str, Any]:
    unknown = set(value) - {
        "name",
        "description",
        "source",
        "enabled",
        "allowImplicitInvocation",
        "aliases",
        "capabilities",
    }
    if unknown:
        raise ValueError("host catalog skill contains unsupported fields: " + ", ".join(sorted(unknown)))
    name = value.get("name")
    description = value.get("description", "")
    source = value.get("source", "unknown")
    enabled = value.get("enabled")
    allow_implicit = value.get("allowImplicitInvocation")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_SKILL_NAME_LENGTH:
        raise ValueError("host catalog skill name is invalid")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"host catalog description is invalid for {name}")
    if source not in HOST_SKILL_SOURCES:
        raise ValueError(f"host catalog source is invalid for {name}")
    if not isinstance(enabled, bool):
        raise ValueError(f"host catalog enabled state is invalid for {name}")
    if allow_implicit is not None and not isinstance(allow_implicit, bool):
        raise ValueError(f"host catalog implicit invocation state is invalid for {name}")
    normalized = {
        "name": name.strip(),
        "description": description.strip(),
        "source": source,
        "enabled": enabled,
        "allowImplicitInvocation": allow_implicit,
    }
    if "aliases" in value:
        normalized["aliases"] = normalized_metadata_values(value["aliases"], "aliases", limit=MAX_ALIAS_COUNT)
    if "capabilities" in value:
        normalized["capabilities"] = normalized_metadata_values(
            value["capabilities"],
            "capabilities",
            limit=MAX_CAPABILITY_COUNT,
        )
    return normalized


def normalize_host_skills(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalized_host_skill(skill) for skill in skills]
    normalized.sort(key=lambda skill: skill["name"])
    names = [skill["name"] for skill in normalized]
    if len(names) != len(set(names)):
        raise ValueError("host catalog contains duplicate skill names")
    return normalized


def host_catalog_revision(host: str, complete: bool, skills: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"schema": HOST_CATALOG_SCHEMA, "host": host, "complete": complete, "skills": skills},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def build_host_catalog(
    host: str,
    skills: list[dict[str, Any]],
    *,
    complete: bool,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not isinstance(host, str) or not host.strip() or len(host) > 100:
        raise ValueError("host catalog host is invalid")
    if not isinstance(complete, bool):
        raise ValueError("host catalog complete must be a boolean")
    normalized = normalize_host_skills(skills)
    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": HOST_CATALOG_SCHEMA,
        "revision": host_catalog_revision(host.strip(), complete, normalized),
        "generatedAt": timestamp,
        "host": host.strip(),
        "complete": complete,
        "skills": normalized,
    }


def invalid_snapshot(reason: str) -> HostCatalogSnapshot:
    return HostCatalogSnapshot("invalid", None, None, False, (), (reason,))


def load_host_catalog(path: Path) -> HostCatalogSnapshot:
    if path.is_symlink():
        return invalid_snapshot("host_catalog_symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HostCatalogSnapshot("missing", None, None, False, (), ("host_catalog_missing",))
    except (OSError, json.JSONDecodeError):
        return invalid_snapshot("host_catalog_unreadable")
    if not isinstance(data, dict) or data.get("schema") != HOST_CATALOG_SCHEMA:
        return invalid_snapshot("host_catalog_schema_unsupported")
    host = data.get("host")
    complete = data.get("complete")
    skills = data.get("skills")
    revision = data.get("revision")
    if not isinstance(host, str) or not host or not isinstance(complete, bool):
        return invalid_snapshot("host_catalog_header_invalid")
    if not isinstance(skills, list) or not all(isinstance(skill, dict) for skill in skills):
        return invalid_snapshot("host_catalog_skills_invalid")
    try:
        normalized = normalize_host_skills(skills)
    except ValueError:
        return invalid_snapshot("host_catalog_skills_invalid")
    if normalized != skills:
        return invalid_snapshot("host_catalog_not_canonical")
    if not isinstance(revision, str) or revision != host_catalog_revision(host, complete, normalized):
        return invalid_snapshot("host_catalog_revision_mismatch")
    return HostCatalogSnapshot("available", revision, host, complete, tuple(normalized))


def availability_for_host(skill: dict[str, Any], host_revision: str) -> dict[str, Any]:
    enabled = skill["enabled"]
    allow_implicit = skill.get("allowImplicitInvocation")
    if not enabled:
        status = "disabled"
        reason = "host_catalog_disabled"
    elif allow_implicit is False:
        status = "unavailable"
        reason = "host_catalog_implicit_invocation_disabled"
    else:
        status = "available"
        reason = "host_catalog_enabled"
    return {
        "status": status,
        "reason_codes": [reason],
        "authorization": False,
        "checks": {
            "identity": "resolved",
            "skill_document": "unknown",
            "runtime_dependencies": "unknown",
            "connector_auth": "unknown",
            "mcp_enablement": "unknown",
            "managed_allowlist": "unknown",
            "host_visibility": "visible" if enabled else "disabled",
            "implicit_invocation": (
                "allowed" if allow_implicit is True else "disabled" if allow_implicit is False else "unknown"
            ),
        },
        "host_catalog_revision": host_revision,
    }


def host_inventory_skill(host: str, host_revision: str, skill: dict[str, Any]) -> dict[str, Any]:
    canonical_id = "/".join(canonical_segment(segment) for segment in ("host", host, "skills", skill["name"]))
    description = skill.get("description", "")
    description_digest = "sha256:" + hashlib.sha256(description.encode()).hexdigest()
    return {
        "configured_name": skill["name"],
        "canonical_id": canonical_id,
        "provider": {"type": "host", "id": host},
        "namespace": "skills",
        "name": skill["name"],
        "revision": None,
        "locator_ref": f"host-catalog:{canonical_id}",
        "provenance_ref": f"host-catalog:{host_revision}",
        "content_digest": None,
        "description": description,
        "description_digest": description_digest,
        "host_source": skill["source"],
        "aliases": list(skill.get("aliases", [])),
        "capabilities": list(skill.get("capabilities", [])),
        "phases": [],
        "availability": availability_for_host(skill, host_revision),
    }


def update_filesystem_skill(
    skill: dict[str, Any],
    host_skill: dict[str, Any] | None,
    catalog: HostCatalogSnapshot,
) -> dict[str, Any]:
    updated = copy.deepcopy(skill)
    if host_skill is not None:
        updated["description"] = host_skill.get("description", "")
        updated["description_digest"] = "sha256:" + hashlib.sha256(updated["description"].encode()).hexdigest()
        updated["host_source"] = host_skill["source"]
        for field in ("aliases", "capabilities"):
            if field in host_skill:
                updated[field] = list(host_skill[field])
        updated["availability"] = availability_for_host(host_skill, str(catalog.revision))
        updated["availability"]["checks"]["skill_document"] = (
            "loadable" if updated.get("content_digest") is not None else "invalid"
        )
        return updated

    availability = updated.get("availability")
    if not isinstance(availability, dict):
        availability = {}
        updated["availability"] = availability
    if catalog.complete:
        availability["status"] = "inactive"
        availability["reason_codes"] = ["host_catalog_not_visible"]
        checks = availability.get("checks")
        if isinstance(checks, dict):
            checks["host_visibility"] = "not-visible"
    else:
        reasons = availability.get("reason_codes")
        if isinstance(reasons, list) and "host_catalog_incomplete" not in reasons:
            reasons.append("host_catalog_incomplete")
    return updated


def reconcile_inventory(
    filesystem_manifest: dict[str, Any],
    catalog: HostCatalogSnapshot,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if catalog.state != "available" or catalog.revision is None or catalog.host is None:
        raise ValueError("host catalog is unavailable")
    raw_skills = filesystem_manifest.get("skills")
    if not isinstance(raw_skills, list) or not all(isinstance(skill, dict) for skill in raw_skills):
        raise ValueError("filesystem inventory is invalid")

    by_name: dict[str, list[dict[str, Any]]] = {}
    for skill in raw_skills:
        name = skill.get("configured_name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(skill)
    host_by_name = {skill["name"]: skill for skill in catalog.skills}

    reconciled: list[dict[str, Any]] = []
    for name, filesystem_skills in by_name.items():
        host_skill = host_by_name.get(name)
        if len(filesystem_skills) == 1:
            reconciled.append(update_filesystem_skill(filesystem_skills[0], host_skill, catalog))
            continue
        reconciled.extend(update_filesystem_skill(skill, None, catalog) for skill in filesystem_skills)
        if host_skill is not None:
            reconciled.append(host_inventory_skill(catalog.host, catalog.revision, host_skill))

    for name, host_skill in host_by_name.items():
        if name not in by_name:
            reconciled.append(host_inventory_skill(catalog.host, catalog.revision, host_skill))

    reconciled.sort(
        key=lambda skill: (
            str(skill.get("configured_name", "")),
            str(skill.get("canonical_id", "")),
            str(skill.get("locator_ref", "")),
        )
    )
    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": INVENTORY_SCHEMA,
        "revision": inventory_revision(reconciled),
        "generated_at": timestamp,
        "sources": {
            "filesystemRevision": filesystem_manifest.get("revision"),
            "hostCatalogRevision": catalog.revision,
            "host": catalog.host,
            "hostCatalogComplete": catalog.complete,
        },
        "skills": reconciled,
    }


def effective_skill_names(manifest: dict[str, Any]) -> set[str]:
    skills = manifest.get("skills")
    if not isinstance(skills, list):
        return set()
    sources = manifest.get("sources")
    complete = isinstance(sources, dict) and sources.get("hostCatalogComplete") is True
    grouped: dict[str, list[str]] = {}
    for skill in skills:
        if not isinstance(skill, dict) or not isinstance(skill.get("configured_name"), str):
            continue
        availability = skill.get("availability")
        status = availability.get("status") if isinstance(availability, dict) else "unknown"
        grouped.setdefault(skill["configured_name"], []).append(str(status))
    names = set()
    for name, statuses in grouped.items():
        if statuses.count("available") == 1:
            names.add(name)
        elif not complete and len(statuses) == 1 and statuses[0] == "unknown":
            names.add(name)
    return names


def catalog_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router catalog",
        description="Build or validate an app-provided host skill catalog.",
    )
    parser.add_argument("command", choices=("build", "validate"))
    parser.add_argument("--codex-home", default=str(codex_home()))
    parser.add_argument("--input")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    root = Path(args.codex_home).expanduser() / "lazy-skill-router"
    input_path = (
        Path(args.input).expanduser()
        if args.input
        else root / ("host-catalog.draft.json" if args.command == "build" else "host-catalog.json")
    )

    if args.command == "validate":
        snapshot = load_host_catalog(input_path)
        if snapshot.state != "available":
            reason = ", ".join(snapshot.reason_codes) or snapshot.state
            print(f"ERROR: invalid host catalog: {reason}", file=sys.stderr)
            return 1
        print(f"OK: host catalog validates: {snapshot.revision}")
        print(f"Skills: {len(snapshot.skills)}; complete: {str(snapshot.complete).lower()}")
        return 0

    try:
        draft = load_json_object(input_path, "host catalog draft")
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read host catalog draft: {exc}", file=sys.stderr)
        return 1
    unknown = set(draft) - {"host", "complete", "skills"}
    if unknown:
        print(
            "ERROR: host catalog draft contains unsupported fields: " + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )
        return 1
    skills = draft.get("skills")
    if not isinstance(skills, list) or not all(isinstance(skill, dict) for skill in skills):
        print("ERROR: host catalog draft skills must be a list of objects", file=sys.stderr)
        return 1
    try:
        catalog = build_host_catalog(draft.get("host"), skills, complete=draft.get("complete"))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    output_path = Path(args.output).expanduser() if args.output else root / "host-catalog.json"
    output_managed_root = output_path.parent if args.output else root
    try:
        ensure_safe_write_target(output_path, output_managed_root)
        write_json_atomic(output_path, catalog, managed_root=output_managed_root)
    except (OSError, ValueError) as exc:
        print(f"ERROR: refusing unsafe catalog write: {output_path}: {exc}", file=sys.stderr)
        return 1
    print(f"Built host catalog with {len(catalog['skills'])} skills at {output_path}")
    print(f"Revision: {catalog['revision']}")
    return 0
