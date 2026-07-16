from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lazy_skill_router_common import (
    MAX_ROUTABLE_PROMPT_CHARS,
    codex_home,
    ensure_safe_write_target,
    write_json_atomic,
)
from lazy_skill_router_inventory import InventorySnapshot, load_inventory_manifest

CAPABILITY_INDEX_SCHEMA_V1: Final = "lazy-skill-router.capability-index/v1"
CAPABILITY_INDEX_SCHEMA_V2: Final = "lazy-skill-router.capability-index/v2"
CAPABILITY_INDEX_SCHEMA: Final = CAPABILITY_INDEX_SCHEMA_V2
FEATURE_EXTRACTOR_V1: Final = "lexical-word-char3/v1"
DEFAULT_CAPABILITY_INDEX_NAME: Final = "capability-index.json"
MAX_INDEX_ENTRIES: Final = 2_000
MAX_INDEX_FEATURES: Final = 20_000
MAX_INDEX_BYTES: Final = 8 * 1024 * 1024
MAX_FEATURES_PER_ENTRY: Final = 512
MAX_WORD_FEATURES: Final = 256
MAX_FEATURE_COUNT: Final = 1_024
MAX_SOURCE_VALUES: Final = 64
MAX_SOURCE_VALUE_CHARS: Final = 512
MAX_PROMPT_CHARS: Final = MAX_ROUTABLE_PROMPT_CHARS
MAX_CANONICAL_ID_CHARS: Final = 512
MAX_CONFIGURED_NAME_CHARS: Final = 256
MAX_AVAILABILITY_STATUS_CHARS: Final = 64
MAX_FEATURE_CHARS: Final = 96
BLOCKED_AVAILABILITY: Final = frozenset({"disabled", "inactive", "unavailable"})
WORD_RE: Final = re.compile(r"[a-z0-9][a-z0-9._:+-]*|[가-힣]+", re.IGNORECASE)
DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
INDEX_FIELDS_V1: Final = frozenset(
    {
        "schema",
        "revision",
        "inventory_revision",
        "generated_at",
        "entries",
        "document_frequency",
        "average_document_length",
    }
)
INDEX_FIELDS_V2: Final = INDEX_FIELDS_V1 | {"feature_extractor"}
ENTRY_FIELDS: Final = frozenset(
    {
        "canonical_id",
        "configured_name",
        "description_digest",
        "aliases",
        "capabilities",
        "phases",
        "availability_status",
        "feature_counts",
        "document_length",
    }
)


@dataclass(frozen=True)
class CapabilityIndexSnapshot:
    state: str
    revision: str | None
    inventory_revision: str | None
    entries: tuple[dict[str, Any], ...]
    document_frequency: dict[str, int]
    average_document_length: float
    reason_codes: tuple[str, ...] = ()
    schema: str | None = None
    feature_extractor: str | None = None


def invalid_snapshot(reason: str) -> CapabilityIndexSnapshot:
    return CapabilityIndexSnapshot("invalid", None, None, (), {}, 0.0, (reason,))


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def bounded_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value[:MAX_SOURCE_VALUES]:
        if isinstance(item, str):
            normalized = normalized_text(item[:MAX_SOURCE_VALUE_CHARS])
            if normalized:
                result.append(normalized)
    return tuple(dict.fromkeys(result))


def lexical_features(values: Iterable[str], *, max_features: int = MAX_FEATURES_PER_ENTRY) -> dict[str, int]:
    word_counts: Counter[str] = Counter()
    trigram_counts: Counter[str] = Counter()
    for raw_value in values:
        normalized = normalized_text(raw_value[:MAX_PROMPT_CHARS])
        tokens = WORD_RE.findall(normalized)
        if len(tokens) > 256:
            tokens = [*tokens[:128], *tokens[-128:]]
        for token in tokens:
            if len(token) + 2 <= MAX_FEATURE_CHARS:
                word_counts[f"w:{token}"] += 1
            compact = re.sub(r"[^a-z0-9가-힣]", "", token)
            if len(compact) < 3:
                continue
            for offset in range(min(len(compact) - 2, 64)):
                trigram_counts[f"g:{compact[offset : offset + 3]}"] += 1
    word_budget = min(MAX_WORD_FEATURES, max_features)
    selected_words = sorted(word_counts.items(), key=lambda item: (-item[1], item[0]))[:word_budget]
    trigram_budget = max(0, max_features - len(selected_words))
    selected_trigrams = sorted(trigram_counts.items(), key=lambda item: (-item[1], item[0]))[:trigram_budget]
    selected = [*selected_words, *selected_trigrams]
    return dict(sorted((feature, min(count, MAX_FEATURE_COUNT)) for feature, count in selected))


def availability_status(skill: dict[str, Any]) -> str:
    availability = skill.get("availability")
    status = availability.get("status") if isinstance(availability, dict) else None
    return status if isinstance(status, str) and status else "unknown"


def indexable_skill(skill: dict[str, Any], inventory: InventorySnapshot) -> bool:
    configured_name = skill.get("configured_name")
    canonical_id = skill.get("canonical_id")
    if not isinstance(configured_name, str) or not configured_name:
        return False
    if not isinstance(canonical_id, str) or not canonical_id:
        return False
    if len(configured_name) > MAX_CONFIGURED_NAME_CHARS or len(canonical_id) > MAX_CANONICAL_ID_CHARS:
        return False
    status = availability_status(skill)
    if status in BLOCKED_AVAILABILITY or len(status) > MAX_AVAILABILITY_STATUS_CHARS:
        return False
    resolved = inventory.resolve(configured_name)
    return isinstance(resolved, dict) and resolved.get("canonical_id") == canonical_id


def capability_entry(skill: dict[str, Any]) -> dict[str, Any]:
    configured_name = str(skill["configured_name"])
    canonical_id = str(skill["canonical_id"])
    description = skill.get("description") if isinstance(skill.get("description"), str) else ""
    aliases = bounded_strings(skill.get("aliases"))
    capabilities = bounded_strings(skill.get("capabilities"))
    phases = bounded_strings(skill.get("phases"))
    features = lexical_features((configured_name, description, *aliases, *capabilities, *phases))
    return {
        "canonical_id": canonical_id,
        "configured_name": configured_name,
        "description_digest": "sha256:" + hashlib.sha256(description.encode()).hexdigest(),
        "aliases": list(aliases),
        "capabilities": list(capabilities),
        "phases": list(phases),
        "availability_status": availability_status(skill),
        "feature_counts": features,
        "document_length": sum(features.values()),
    }


def index_statistics(entries: Iterable[dict[str, Any]]) -> tuple[dict[str, int], float]:
    materialized = tuple(entries)
    frequencies: Counter[str] = Counter()
    total_length = 0
    for entry in materialized:
        feature_counts = entry["feature_counts"]
        frequencies.update(feature_counts.keys())
        total_length += int(entry["document_length"])
    average = total_length / len(materialized) if materialized else 0.0
    return dict(sorted(frequencies.items())), round(average, 6)


def capability_index_revision(
    inventory_revision: str,
    entries: list[dict[str, Any]],
    document_frequency: dict[str, int],
    average_document_length: float,
    *,
    schema: str = CAPABILITY_INDEX_SCHEMA,
    feature_extractor: str = FEATURE_EXTRACTOR_V1,
) -> str:
    if schema not in {CAPABILITY_INDEX_SCHEMA_V1, CAPABILITY_INDEX_SCHEMA_V2}:
        raise ValueError("unsupported capability index schema")
    canonical_payload: dict[str, Any] = {
        "schema": schema,
        "inventory_revision": inventory_revision,
        "entries": entries,
        "document_frequency": document_frequency,
        "average_document_length": average_document_length,
    }
    if schema == CAPABILITY_INDEX_SCHEMA_V2:
        if feature_extractor != FEATURE_EXTRACTOR_V1:
            raise ValueError("unsupported capability index feature extractor")
        canonical_payload["feature_extractor"] = feature_extractor
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def build_capability_index(
    inventory: InventorySnapshot,
    *,
    generated_at: str | None = None,
    schema: str = CAPABILITY_INDEX_SCHEMA,
) -> dict[str, Any]:
    if inventory.state != "available" or not isinstance(inventory.revision, str):
        raise ValueError("an available skill inventory is required")
    entries = [capability_entry(skill) for skill in inventory.skills if indexable_skill(skill, inventory)]
    entries.sort(key=lambda entry: (entry["configured_name"], entry["canonical_id"]))
    if len(entries) > MAX_INDEX_ENTRIES:
        raise ValueError(f"capability index has more than {MAX_INDEX_ENTRIES} entries")
    document_frequency, average_document_length = index_statistics(entries)
    if len(document_frequency) > MAX_INDEX_FEATURES:
        raise ValueError(f"capability index has more than {MAX_INDEX_FEATURES} features")
    revision = capability_index_revision(
        inventory.revision,
        entries,
        document_frequency,
        average_document_length,
        schema=schema,
    )
    timestamp = generated_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if any(validate_entry(entry) != entry for entry in entries):
        raise ValueError("capability index builder produced an invalid entry")
    result = {
        "schema": schema,
        "revision": revision,
        "inventory_revision": inventory.revision,
        "generated_at": timestamp,
        "entries": entries,
        "document_frequency": document_frequency,
        "average_document_length": average_document_length,
    }
    if schema == CAPABILITY_INDEX_SCHEMA_V2:
        result["feature_extractor"] = FEATURE_EXTRACTOR_V1
    encoded_bytes = len(json.dumps(result, ensure_ascii=False, indent=2).encode()) + 1
    if encoded_bytes > MAX_INDEX_BYTES:
        raise ValueError(f"capability index exceeds {MAX_INDEX_BYTES} bytes")
    return result


def build_capability_index_v1(
    inventory: InventorySnapshot,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Reproduce the frozen v1 wire format and canonical revision exactly."""
    return build_capability_index(
        inventory,
        generated_at=generated_at,
        schema=CAPABILITY_INDEX_SCHEMA_V1,
    )


def validate_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != ENTRY_FIELDS:
        return None
    canonical_id = value.get("canonical_id")
    configured_name = value.get("configured_name")
    description_digest = value.get("description_digest")
    availability = value.get("availability_status")
    features = value.get("feature_counts")
    document_length = value.get("document_length")
    required_strings = (canonical_id, configured_name, description_digest, availability)
    if not all(isinstance(item, str) and item for item in required_strings):
        return None
    if (
        len(canonical_id) > MAX_CANONICAL_ID_CHARS
        or len(configured_name) > MAX_CONFIGURED_NAME_CHARS
        or len(availability) > MAX_AVAILABILITY_STATUS_CHARS
    ):
        return None
    if DIGEST_RE.fullmatch(description_digest) is None:
        return None
    aliases = bounded_strings(value.get("aliases"))
    capabilities = bounded_strings(value.get("capabilities"))
    phases = bounded_strings(value.get("phases"))
    if list(aliases) != value.get("aliases") or list(capabilities) != value.get("capabilities"):
        return None
    if list(phases) != value.get("phases"):
        return None
    if not isinstance(features, dict) or len(features) > MAX_FEATURES_PER_ENTRY:
        return None
    normalized_features: dict[str, int] = {}
    for feature, count in features.items():
        if not isinstance(feature, str) or not feature or len(feature) > MAX_FEATURE_CHARS:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_FEATURE_COUNT:
            return None
        normalized_features[feature] = count
    normalized_features = dict(sorted(normalized_features.items()))
    if normalized_features != features:
        return None
    expected_length = sum(normalized_features.values())
    if isinstance(document_length, bool) or not isinstance(document_length, int) or document_length != expected_length:
        return None
    return {
        "canonical_id": canonical_id,
        "configured_name": configured_name,
        "description_digest": description_digest,
        "aliases": list(aliases),
        "capabilities": list(capabilities),
        "phases": list(phases),
        "availability_status": availability,
        "feature_counts": normalized_features,
        "document_length": document_length,
    }


def load_capability_index(path: Path, *, frozen_replay: bool = False) -> CapabilityIndexSnapshot:
    if path.is_symlink():
        return invalid_snapshot("capability_index_symlink")
    try:
        if path.stat().st_size > MAX_INDEX_BYTES:
            return invalid_snapshot("capability_index_too_large")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CapabilityIndexSnapshot("missing", None, None, (), {}, 0.0, ("capability_index_missing",))
    except (OSError, json.JSONDecodeError):
        return invalid_snapshot("capability_index_unreadable")
    if not isinstance(data, dict):
        return invalid_snapshot("capability_index_schema_unsupported")
    schema = data.get("schema")
    if schema not in {CAPABILITY_INDEX_SCHEMA_V1, CAPABILITY_INDEX_SCHEMA_V2}:
        return invalid_snapshot("capability_index_schema_unsupported")
    if schema == CAPABILITY_INDEX_SCHEMA_V1 and not frozen_replay:
        return invalid_snapshot("capability_index_v1_replay_only")
    expected_fields = INDEX_FIELDS_V1 if schema == CAPABILITY_INDEX_SCHEMA_V1 else INDEX_FIELDS_V2
    if set(data) != expected_fields:
        return invalid_snapshot("capability_index_fields_invalid")
    feature_extractor = data.get("feature_extractor", FEATURE_EXTRACTOR_V1)
    if feature_extractor != FEATURE_EXTRACTOR_V1:
        return invalid_snapshot("capability_index_feature_extractor_unsupported")
    raw_entries = data.get("entries")
    inventory_revision = data.get("inventory_revision")
    revision = data.get("revision")
    generated_at = data.get("generated_at")
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_INDEX_ENTRIES:
        return invalid_snapshot("capability_index_entries_invalid")
    if not isinstance(inventory_revision, str) or not inventory_revision:
        return invalid_snapshot("capability_index_inventory_revision_invalid")
    if not isinstance(generated_at, str) or not generated_at or len(generated_at) > 64:
        return invalid_snapshot("capability_index_generated_at_invalid")
    entries: list[dict[str, Any]] = []
    for raw_entry in raw_entries:
        entry = validate_entry(raw_entry)
        if entry is None:
            return invalid_snapshot("capability_index_entry_invalid")
        if entry["availability_status"] in BLOCKED_AVAILABILITY:
            return invalid_snapshot("capability_index_availability_invalid")
        entries.append(entry)
    if entries != sorted(entries, key=lambda entry: (entry["configured_name"], entry["canonical_id"])):
        return invalid_snapshot("capability_index_order_invalid")
    configured_names = {entry["configured_name"] for entry in entries}
    canonical_ids = {entry["canonical_id"] for entry in entries}
    if len(configured_names) != len(entries) or len(canonical_ids) != len(entries):
        return invalid_snapshot("capability_index_identity_duplicate")
    document_frequency, average_document_length = index_statistics(entries)
    if len(document_frequency) > MAX_INDEX_FEATURES:
        return invalid_snapshot("capability_index_features_invalid")
    if data.get("document_frequency") != document_frequency:
        return invalid_snapshot("capability_index_statistics_mismatch")
    if data.get("average_document_length") != average_document_length:
        return invalid_snapshot("capability_index_statistics_mismatch")
    expected_revision = capability_index_revision(
        inventory_revision,
        entries,
        document_frequency,
        average_document_length,
        schema=schema,
        feature_extractor=feature_extractor,
    )
    if not isinstance(revision, str) or revision != expected_revision:
        return invalid_snapshot("capability_index_revision_mismatch")
    return CapabilityIndexSnapshot(
        "available",
        revision,
        inventory_revision,
        tuple(entries),
        document_frequency,
        average_document_length,
        (),
        schema,
        feature_extractor,
    )


def capability_index_path(config: dict[str, Any], explicit_path: str | None = None) -> Path | None:
    if explicit_path is not None:
        return Path(explicit_path).expanduser()
    loaded_from = config.get("_loaded_from")
    if not isinstance(loaded_from, str) or not loaded_from:
        return None
    return Path(loaded_from).with_name(DEFAULT_CAPABILITY_INDEX_NAME)


def build_command(args: argparse.Namespace) -> int:
    root = Path(args.codex_home).expanduser()
    inventory_path = (
        Path(args.inventory).expanduser() if args.inventory else root / "lazy-skill-router" / "skills.manifest.json"
    )
    output_path = (
        Path(args.output).expanduser() if args.output else inventory_path.with_name(DEFAULT_CAPABILITY_INDEX_NAME)
    )
    inventory = load_inventory_manifest(inventory_path)
    if inventory.state != "available":
        reason = ", ".join(inventory.reason_codes) or inventory.state
        raise ValueError(f"skill inventory is unavailable: {reason}")
    index = build_capability_index(inventory)
    output_managed_root = output_path.parent if args.output or args.inventory else root
    ensure_safe_write_target(output_path, output_managed_root)
    write_json_atomic(output_path, index, managed_root=output_managed_root)
    print(
        json.dumps(
            {
                "status": "built",
                "path": str(output_path),
                "revision": index["revision"],
                "inventoryRevision": index["inventory_revision"],
                "schema": index["schema"],
                "featureExtractor": index.get("feature_extractor", FEATURE_EXTRACTOR_V1),
                "entries": len(index["entries"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def validate_command(args: argparse.Namespace) -> int:
    root = Path(args.codex_home).expanduser()
    path = Path(args.index).expanduser() if args.index else root / "lazy-skill-router" / DEFAULT_CAPABILITY_INDEX_NAME
    snapshot = load_capability_index(path)
    payload = {
        "status": snapshot.state,
        "path": str(path),
        "revision": snapshot.revision,
        "inventoryRevision": snapshot.inventory_revision,
        "schema": snapshot.schema,
        "featureExtractor": snapshot.feature_extractor,
        "entries": len(snapshot.entries),
        "reasonCodes": list(snapshot.reason_codes),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if snapshot.state == "available" else 1


def capability_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lazy-skill-router capability",
        description="Build or validate the local capability index.",
    )
    parser.add_argument("--codex-home", default=str(codex_home()), help="Codex home directory.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    build_parser = subparsers.add_parser("build", help="Build the product capability index from the skill inventory.")
    build_parser.add_argument(
        "--codex-home",
        default=argparse.SUPPRESS,
        help="Codex home directory.",
    )
    build_parser.add_argument("--inventory", help="Skill inventory manifest path.")
    build_parser.add_argument("--output", help="Capability index output path.")
    validate_parser = subparsers.add_parser("validate", help="Validate a v1 or v2 capability index file.")
    validate_parser.add_argument(
        "--codex-home",
        default=argparse.SUPPRESS,
        help="Codex home directory.",
    )
    validate_parser.add_argument("--index", help="Capability index path.")
    args = parser.parse_args(argv)
    try:
        return build_command(args) if args.action == "build" else validate_command(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(capability_main())
