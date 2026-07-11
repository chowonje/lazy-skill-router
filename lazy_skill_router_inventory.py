from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

INVENTORY_SCHEMA = "lazy-skill-router.skill-inventory/v1"


class SkillRecordLike(Protocol):
    name: str
    path: Path


@dataclass(frozen=True)
class InventorySnapshot:
    state: str
    revision: str | None
    skills: tuple[dict[str, Any], ...]
    reason_codes: tuple[str, ...] = ()

    def resolve(self, configured_name: str) -> dict[str, Any] | None:
        usable_matches = tuple(
            skill
            for skill in self.skills
            if skill.get("configured_name") == configured_name
            if not isinstance(skill.get("availability"), dict)
            or skill["availability"].get("status") not in {"disabled", "inactive", "unavailable"}
        )
        if len(usable_matches) != 1:
            return None
        candidate = usable_matches[0]
        canonical_id = candidate.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id:
            return candidate
        canonical_matches = tuple(
            skill
            for skill in self.skills
            if skill.get("canonical_id") == canonical_id
            if not isinstance(skill.get("availability"), dict)
            or skill["availability"].get("status") not in {"disabled", "inactive", "unavailable"}
        )
        return candidate if len(canonical_matches) == 1 else None

    def match_count(self, configured_name: str) -> int:
        return sum(1 for skill in self.skills if skill.get("configured_name") == configured_name)


@dataclass(frozen=True)
class InventoryDiff:
    previous_state: str
    previous_revision: str | None
    current_revision: str
    added: tuple[dict[str, Any], ...]
    removed: tuple[dict[str, Any], ...]
    changed: tuple[dict[str, Any], ...]
    ambiguous_names: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def canonical_segment(value: str) -> str:
    return quote(value, safe="-._~")


def content_digest(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def relative_path(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def configured_skill_name_parts(configured_name: str) -> tuple[str, str]:
    if ":" in configured_name:
        namespace, name = configured_name.split(":", 1)
        return namespace, name
    return "default", configured_name


def skill_identity(record: SkillRecordLike, codex_root: Path, agents_root: Path) -> dict[str, Any]:
    path = record.path
    plugin_relative = relative_path(path, codex_root / "plugins" / "cache")
    if plugin_relative is not None:
        parts = plugin_relative.parts
        if "skills" in parts:
            skills_index = parts.index("skills")
            if skills_index >= 2 and skills_index + 1 < len(parts):
                provider_id = parts[0]
                namespace = parts[1]
                revision = parts[skills_index - 1] if skills_index >= 3 else None
                name = parts[skills_index + 1]
                canonical_id = "/".join(
                    canonical_segment(segment) for segment in ("plugin", provider_id, namespace, name)
                )
                return {
                    "canonical_id": canonical_id,
                    "provider": {"type": "plugin", "id": provider_id},
                    "namespace": namespace,
                    "name": name,
                    "revision": revision,
                    "locator_ref": f"codex-plugin:{plugin_relative.as_posix()}",
                }

    codex_relative = relative_path(path, codex_root / "skills")
    if codex_relative is not None:
        canonical_id = "/".join(canonical_segment(segment) for segment in ("user", "codex", "skills", record.name))
        return {
            "canonical_id": canonical_id,
            "provider": {"type": "user", "id": "codex"},
            "namespace": "skills",
            "name": record.name,
            "revision": None,
            "locator_ref": f"codex:skills/{codex_relative.as_posix()}",
        }

    agents_relative = relative_path(path, agents_root / "skills")
    if agents_relative is not None:
        canonical_id = "/".join(canonical_segment(segment) for segment in ("user", "agents", "skills", record.name))
        return {
            "canonical_id": canonical_id,
            "provider": {"type": "user", "id": "agents"},
            "namespace": "skills",
            "name": record.name,
            "revision": None,
            "locator_ref": f"agents:skills/{agents_relative.as_posix()}",
        }

    namespace, name = configured_skill_name_parts(record.name)
    canonical_id = "/".join(canonical_segment(segment) for segment in ("configured", "external", namespace, name))
    locator_digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return {
        "canonical_id": canonical_id,
        "provider": {"type": "configured", "id": "external"},
        "namespace": namespace,
        "name": name,
        "revision": None,
        "locator_ref": f"unresolved:{locator_digest}",
    }


def manifest_skill(record: SkillRecordLike, codex_root: Path, agents_root: Path) -> dict[str, Any]:
    identity = skill_identity(record, codex_root, agents_root)
    digest = content_digest(record.path)
    description = getattr(record, "description", "")
    if not isinstance(description, str):
        description = ""
    reason_codes = ["runtime_state_unchecked"]
    if digest is None:
        reason_codes.insert(0, "skill_document_unreadable")
    return {
        "configured_name": record.name,
        **identity,
        "provenance_ref": f"inventory:{identity['canonical_id']}",
        "content_digest": digest,
        "description": description,
        "description_digest": "sha256:" + hashlib.sha256(description.encode()).hexdigest(),
        "aliases": [],
        "capabilities": [],
        "phases": [],
        "availability": {
            "status": "unknown",
            "reason_codes": reason_codes,
            "authorization": False,
            "checks": {
                "identity": "resolved",
                "skill_document": "loadable" if digest is not None else "invalid",
                "runtime_dependencies": "unknown",
                "connector_auth": "unknown",
                "mcp_enablement": "unknown",
                "managed_allowlist": "unknown",
            },
        },
    }


def inventory_revision(skills: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"schema": INVENTORY_SCHEMA, "skills": skills},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def skill_change_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    tracked_fields = (
        "configured_name",
        "revision",
        "content_digest",
        "description",
        "description_digest",
        "host_source",
        "aliases",
        "capabilities",
        "phases",
        "availability",
    )
    return tuple(field for field in tracked_fields if before.get(field) != after.get(field))


def inventory_groups(skills: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        canonical_id = skill.get("canonical_id")
        if isinstance(canonical_id, str) and canonical_id:
            groups.setdefault(canonical_id, []).append(skill)
    return groups


def inventory_entry_summary(skill: dict[str, Any]) -> dict[str, Any]:
    availability = skill.get("availability")
    status = availability.get("status") if isinstance(availability, dict) else None
    return {
        "configured_name": skill.get("configured_name"),
        "canonical_id": skill.get("canonical_id"),
        "revision": skill.get("revision"),
        "content_digest": skill.get("content_digest"),
        "availability_status": status,
    }


def diff_inventory(previous: InventorySnapshot, current: dict[str, Any]) -> InventoryDiff:
    current_skills = current.get("skills")
    current_revision = current.get("revision")
    if not isinstance(current_skills, list) or not all(isinstance(skill, dict) for skill in current_skills):
        raise ValueError("current inventory skills are invalid")
    if not isinstance(current_revision, str) or current_revision != inventory_revision(current_skills):
        raise ValueError("current inventory revision is invalid")

    previous_groups = inventory_groups(previous.skills)
    current_groups = inventory_groups(current_skills)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for canonical_id in sorted(set(previous_groups) | set(current_groups)):
        before_group = previous_groups.get(canonical_id, [])
        after_group = current_groups.get(canonical_id, [])
        if len(before_group) == 1 and len(after_group) == 1:
            before = before_group[0]
            after = after_group[0]
            fields = skill_change_fields(before, after)
            if fields:
                changed.append(
                    {
                        "configured_name": after.get("configured_name"),
                        "canonical_id": canonical_id,
                        "fields": list(fields),
                        "before": inventory_entry_summary(before),
                        "after": inventory_entry_summary(after),
                    }
                )
            continue

        before_by_locator = {
            str(skill.get("locator_ref")): skill for skill in before_group if isinstance(skill.get("locator_ref"), str)
        }
        after_by_locator = {
            str(skill.get("locator_ref")): skill for skill in after_group if isinstance(skill.get("locator_ref"), str)
        }
        for locator in sorted(set(before_by_locator) | set(after_by_locator)):
            before = before_by_locator.get(locator)
            after = after_by_locator.get(locator)
            if before is None and after is not None:
                added.append(inventory_entry_summary(after))
            elif after is None and before is not None:
                removed.append(inventory_entry_summary(before))
            elif before is not None and after is not None:
                fields = skill_change_fields(before, after)
                if fields:
                    changed.append(
                        {
                            "configured_name": after.get("configured_name"),
                            "canonical_id": canonical_id,
                            "fields": list(fields),
                            "before": inventory_entry_summary(before),
                            "after": inventory_entry_summary(after),
                        }
                    )

    ambiguous_names = sorted(
        {
            str(skill.get("configured_name"))
            for skill in current_skills
            if isinstance(skill.get("availability"), dict)
            and skill["availability"].get("status") not in {"disabled", "inactive", "unavailable"}
            and skill["availability"].get("checks", {}).get("identity") == "ambiguous"
        }
    )
    return InventoryDiff(
        previous.state,
        previous.revision,
        current_revision,
        tuple(added),
        tuple(removed),
        tuple(changed),
        tuple(ambiguous_names),
    )


def build_inventory_manifest(
    records: Iterable[SkillRecordLike],
    codex_root: Path,
    agents_root: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    skills = [manifest_skill(record, codex_root, agents_root) for record in records]
    skills.sort(key=lambda skill: (skill["configured_name"], skill["canonical_id"], skill["locator_ref"]))

    name_counts: dict[str, int] = {}
    canonical_counts: dict[str, int] = {}
    for skill in skills:
        configured_name = skill["configured_name"]
        canonical_id = skill["canonical_id"]
        name_counts[configured_name] = name_counts.get(configured_name, 0) + 1
        canonical_counts[canonical_id] = canonical_counts.get(canonical_id, 0) + 1
    for skill in skills:
        reason_codes: list[str] = []
        if name_counts[skill["configured_name"]] > 1:
            reason_codes.append("duplicate_configured_name")
        if canonical_counts[skill["canonical_id"]] > 1:
            reason_codes.append("duplicate_canonical_id")
        if reason_codes:
            availability = skill["availability"]
            availability["reason_codes"] = [*reason_codes, *availability["reason_codes"]]
            availability["checks"]["identity"] = "ambiguous"

    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": INVENTORY_SCHEMA,
        "revision": inventory_revision(skills),
        "generated_at": timestamp,
        "skills": skills,
    }


def invalid_snapshot(reason: str) -> InventorySnapshot:
    return InventorySnapshot("invalid", None, (), (reason,))


def load_inventory_manifest(path: Path) -> InventorySnapshot:
    if path.is_symlink():
        return invalid_snapshot("inventory_manifest_symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InventorySnapshot("missing", None, (), ("inventory_manifest_missing",))
    except (OSError, json.JSONDecodeError):
        return invalid_snapshot("inventory_manifest_unreadable")
    if not isinstance(data, dict) or data.get("schema") != INVENTORY_SCHEMA:
        return invalid_snapshot("inventory_schema_unsupported")
    skills = data.get("skills")
    revision = data.get("revision")
    if not isinstance(skills, list) or not all(isinstance(skill, dict) for skill in skills):
        return invalid_snapshot("inventory_skills_invalid")
    if not isinstance(revision, str) or revision != inventory_revision(skills):
        return invalid_snapshot("inventory_revision_mismatch")
    return InventorySnapshot("available", revision, tuple(skills))


def inventory_for_config(config: dict[str, Any], explicit_path: str | None) -> InventorySnapshot | None:
    if explicit_path is not None:
        return load_inventory_manifest(Path(explicit_path).expanduser())
    loaded_from = config.get("_loaded_from")
    if not isinstance(loaded_from, str) or not loaded_from:
        return None
    candidate = Path(loaded_from).with_name("skills.manifest.json")
    if not candidate.exists() and not candidate.is_symlink():
        return None
    return load_inventory_manifest(candidate)
