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
        matches = tuple(skill for skill in self.skills if skill.get("configured_name") == configured_name)
        return matches[0] if len(matches) == 1 else None

    def match_count(self, configured_name: str) -> int:
        return sum(1 for skill in self.skills if skill.get("configured_name") == configured_name)


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
    reason_codes = ["runtime_state_unchecked"]
    if digest is None:
        reason_codes.insert(0, "skill_document_unreadable")
    return {
        "configured_name": record.name,
        **identity,
        "provenance_ref": f"inventory:{identity['canonical_id']}",
        "content_digest": digest,
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


def build_inventory_manifest(
    records: Iterable[SkillRecordLike],
    codex_root: Path,
    agents_root: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    skills = [manifest_skill(record, codex_root, agents_root) for record in records]
    skills.sort(key=lambda skill: (skill["configured_name"], skill["canonical_id"], skill["locator_ref"]))

    counts: dict[str, int] = {}
    for skill in skills:
        configured_name = skill["configured_name"]
        counts[configured_name] = counts.get(configured_name, 0) + 1
    for skill in skills:
        if counts[skill["configured_name"]] > 1:
            availability = skill["availability"]
            availability["reason_codes"] = ["duplicate_configured_name", *availability["reason_codes"]]
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
