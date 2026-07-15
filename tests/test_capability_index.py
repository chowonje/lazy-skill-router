from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from lazy_skill_router_capability_index import (
    CAPABILITY_INDEX_SCHEMA_V1,
    CAPABILITY_INDEX_SCHEMA_V2,
    FEATURE_EXTRACTOR_V1,
    build_capability_index,
    build_capability_index_v1,
    capability_index_revision,
    index_statistics,
    load_capability_index,
)
from lazy_skill_router_inventory import InventorySnapshot


def skill(
    name: str,
    description: str,
    *,
    canonical_id: str | None = None,
    status: str = "available",
    aliases: list[str] | None = None,
) -> dict[str, object]:
    return {
        "configured_name": name,
        "canonical_id": canonical_id or f"user/codex/skills/{name}",
        "description": description,
        "aliases": aliases or [],
        "capabilities": [],
        "phases": [],
        "availability": {"status": status},
    }


def inventory(*skills: dict[str, object], revision: str = "sha256:inventory-v1") -> InventorySnapshot:
    return InventorySnapshot("available", revision, tuple(skills))


def resign(index: dict[str, object]) -> None:
    entries = index["entries"]
    assert isinstance(entries, list)
    document_frequency, average_document_length = index_statistics(entries)
    index["document_frequency"] = document_frequency
    index["average_document_length"] = average_document_length
    index["revision"] = capability_index_revision(
        str(index["inventory_revision"]),
        entries,
        document_frequency,
        average_document_length,
    )


class CapabilityIndexTest(unittest.TestCase):
    def test_product_builder_emits_v2_with_revision_bound_feature_extractor(self) -> None:
        built = build_capability_index(
            inventory(skill("code-review", "Review code and pull requests.")),
            generated_at="2026-07-12T00:00:00Z",
        )

        self.assertEqual(built["schema"], CAPABILITY_INDEX_SCHEMA_V2)
        self.assertEqual(built["feature_extractor"], FEATURE_EXTRACTOR_V1)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            loaded = load_capability_index(path)

        self.assertEqual(loaded.state, "available")
        self.assertEqual(loaded.schema, CAPABILITY_INDEX_SCHEMA_V2)
        self.assertEqual(loaded.feature_extractor, FEATURE_EXTRACTOR_V1)

    def test_v1_frozen_builder_is_replay_only_and_remains_loadable_without_rewriting(self) -> None:
        built = build_capability_index_v1(
            inventory(skill("code-review", "Review code and pull requests.")),
            generated_at="2026-07-12T00:00:00Z",
        )
        original_bytes = json.dumps(built, ensure_ascii=False, sort_keys=True).encode()

        self.assertEqual(built["schema"], CAPABILITY_INDEX_SCHEMA_V1)
        self.assertNotIn("feature_extractor", built)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            path.write_bytes(original_bytes)
            product = load_capability_index(path)
            loaded = load_capability_index(path, frozen_replay=True)

        self.assertEqual(product.state, "invalid")
        self.assertEqual(product.reason_codes, ("capability_index_v1_replay_only",))
        self.assertEqual(loaded.state, "available")
        self.assertEqual(loaded.schema, CAPABILITY_INDEX_SCHEMA_V1)
        self.assertEqual(loaded.feature_extractor, FEATURE_EXTRACTOR_V1)
        self.assertEqual(json.dumps(built, ensure_ascii=False, sort_keys=True).encode(), original_bytes)

    def test_v2_loader_rejects_unknown_feature_extractor(self) -> None:
        built = build_capability_index(
            inventory(skill("code-review", "Review code and pull requests.")),
            generated_at="2026-07-12T00:00:00Z",
        )
        built["feature_extractor"] = "unknown/v9"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            loaded = load_capability_index(path)

        self.assertEqual(loaded.state, "invalid")
        self.assertEqual(loaded.reason_codes, ("capability_index_feature_extractor_unsupported",))

    def test_build_is_deterministic_and_excludes_unavailable_or_ambiguous_skills(self) -> None:
        duplicate_a = skill("duplicate", "first", canonical_id="skill/duplicate-a")
        duplicate_b = skill("duplicate", "second", canonical_id="skill/duplicate-b")
        source = inventory(
            skill("zeta", "Review release notes."),
            skill("disabled", "Must not be indexed.", status="disabled"),
            duplicate_a,
            skill("alpha", "Analyze API contracts."),
            duplicate_b,
        )

        first = build_capability_index(source, generated_at="2026-07-12T00:00:00Z")
        second = build_capability_index(source, generated_at="2026-07-13T00:00:00Z")

        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual(first["document_frequency"], second["document_frequency"])
        self.assertEqual(
            [entry["configured_name"] for entry in first["entries"]],
            ["alpha", "zeta"],
        )

    def test_load_accepts_canonical_index_and_rejects_revision_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            built = build_capability_index(
                inventory(skill("code-review", "Review code and pull requests.")),
                generated_at="2026-07-12T00:00:00Z",
            )
            path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")

            loaded = load_capability_index(path)
            self.assertEqual(loaded.state, "available")
            self.assertEqual(loaded.revision, built["revision"])
            self.assertEqual(loaded.inventory_revision, built["inventory_revision"])

            built["revision"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            tampered = load_capability_index(path)

        self.assertEqual(tampered.state, "invalid")
        self.assertEqual(tampered.reason_codes, ("capability_index_revision_mismatch",))

    def test_bilingual_alias_change_produces_a_new_index_revision(self) -> None:
        base = build_capability_index(
            inventory(skill("security-threat-model", "Build a threat model.")),
            generated_at="2026-07-12T00:00:00Z",
        )
        enriched = build_capability_index(
            inventory(
                skill(
                    "security-threat-model",
                    "Build a threat model.",
                    aliases=["보안 위협 모델"],
                )
            ),
            generated_at="2026-07-12T00:00:00Z",
        )

        self.assertNotEqual(base["revision"], enriched["revision"])
        self.assertEqual(enriched["entries"][0]["aliases"], ["보안 위협 모델"])

    def test_load_rejects_symlink_even_when_target_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "real-index.json"
            link = root / "capability-index.json"
            built = build_capability_index(
                inventory(skill("security-threat-model", "Model security threats.")),
                generated_at="2026-07-12T00:00:00Z",
            )
            target.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            link.symlink_to(target)

            loaded = load_capability_index(link)

        self.assertEqual(loaded.state, "invalid")
        self.assertEqual(loaded.reason_codes, ("capability_index_symlink",))

    def test_load_rejects_resigned_blocked_or_ambiguous_entries(self) -> None:
        built = build_capability_index(
            inventory(skill("ponytail", "Choose the smallest implementation.")),
            generated_at="2026-07-12T00:00:00Z",
        )
        variants: list[tuple[str, dict[str, object], str]] = []

        blocked = deepcopy(built)
        blocked["entries"][0]["availability_status"] = "inactive"
        resign(blocked)
        variants.append(("blocked", blocked, "capability_index_availability_invalid"))

        duplicate_name = deepcopy(built)
        second = deepcopy(duplicate_name["entries"][0])
        second["canonical_id"] = "user/codex/skills/ponytail-copy"
        duplicate_name["entries"].append(second)
        duplicate_name["entries"].sort(key=lambda entry: (entry["configured_name"], entry["canonical_id"]))
        resign(duplicate_name)
        variants.append(("duplicate-name", duplicate_name, "capability_index_identity_duplicate"))

        duplicate_canonical = deepcopy(built)
        second = deepcopy(duplicate_canonical["entries"][0])
        second["configured_name"] = "ponytail-copy"
        duplicate_canonical["entries"].append(second)
        duplicate_canonical["entries"].sort(key=lambda entry: (entry["configured_name"], entry["canonical_id"]))
        resign(duplicate_canonical)
        variants.append(("duplicate-canonical", duplicate_canonical, "capability_index_identity_duplicate"))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            for label, variant, reason in variants:
                with self.subTest(label=label):
                    path.write_text(json.dumps(variant, ensure_ascii=False), encoding="utf-8")
                    loaded = load_capability_index(path)
                    self.assertEqual(loaded.state, "invalid")
                    self.assertEqual(loaded.reason_codes, (reason,))

    def test_long_legal_tokens_round_trip_through_writer_and_reader(self) -> None:
        long_name = "a" * 100
        long_description_token = "b" * 200
        built = build_capability_index(
            inventory(skill(long_name, long_description_token)),
            generated_at="2026-07-12T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capability-index.json"
            path.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            loaded = load_capability_index(path)

        self.assertEqual(loaded.state, "available")
        self.assertEqual(loaded.entries[0]["configured_name"], long_name)
        self.assertTrue(all(len(feature) <= 96 for feature in loaded.entries[0]["feature_counts"]))


if __name__ == "__main__":
    unittest.main()
