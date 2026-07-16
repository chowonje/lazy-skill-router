from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

from eval_router_ab import parse_manifest, require_exact_fields, require_fields, require_object

OVERLAY_SCHEMA: Final = "lazy-skill-router.router-ab-manifest-overlay/v1"
OVERLAY_FIELDS: Final = frozenset({"schema", "baseManifestRevision", "patch"})
PATCH_FIELDS: Final = frozenset({"frozen", "evidence"})
FROZEN_PATCH_REQUIRED_FIELDS: Final = frozenset({"inventoryRevision", "indexRevision"})
FROZEN_PATCH_FIELDS: Final = FROZEN_PATCH_REQUIRED_FIELDS | frozenset(
    {"indexSchema", "retrievalAlgorithm", "experimentCodeRevision"}
)
EVIDENCE_PATCH_FIELDS: Final = frozenset({"metadataProvenance"})


def load_json_object(path: Path, location: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{location} is unreadable or invalid JSON") from exc
    return require_object(raw, location)


def materialize_manifest(base: dict[str, Any], overlay: dict[str, Any]) -> tuple[dict[str, Any], str]:
    require_exact_fields(overlay, OVERLAY_FIELDS, "overlay")
    if overlay["schema"] != OVERLAY_SCHEMA:
        raise ValueError("overlay.schema is unsupported")
    parsed_base = parse_manifest(base)
    if "evidence" not in base:
        raise ValueError("base manifest must declare evidence")
    expected_base_revision = overlay["baseManifestRevision"]
    if not isinstance(expected_base_revision, str) or expected_base_revision != parsed_base.revision:
        raise ValueError("overlay.baseManifestRevision does not match the base manifest")

    patch = require_object(overlay["patch"], "overlay.patch")
    require_exact_fields(patch, PATCH_FIELDS, "overlay.patch")
    frozen_patch = require_object(patch["frozen"], "overlay.patch.frozen")
    evidence_patch = require_object(patch["evidence"], "overlay.patch.evidence")
    require_fields(
        frozen_patch,
        required=FROZEN_PATCH_REQUIRED_FIELDS,
        allowed=FROZEN_PATCH_FIELDS,
        location="overlay.patch.frozen",
    )
    require_exact_fields(evidence_patch, EVIDENCE_PATCH_FIELDS, "overlay.patch.evidence")

    materialized = json.loads(json.dumps(base, ensure_ascii=False))
    materialized["frozen"].update(frozen_patch)
    materialized["evidence"].update(evidence_patch)
    parsed = parse_manifest(materialized)
    return materialized, parsed.revision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize a frozen router A/B manifest overlay")
    parser.add_argument("base", type=Path, help="canonical base manifest")
    parser.add_argument("overlay", type=Path, help="validated manifest overlay")
    parser.add_argument("--output", required=True, type=Path, help="generated manifest path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        materialized, revision = materialize_manifest(
            load_json_object(args.base, "base manifest"),
            load_json_object(args.overlay, "overlay"),
        )
        args.output.write_text(json.dumps(materialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except ValueError as exc:
        raise SystemExit(f"INVALID: {exc}") from exc
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
