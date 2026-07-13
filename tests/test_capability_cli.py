from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from lazy_skill_router_capability_index import capability_main, load_capability_index
from lazy_skill_router_cli.cli import route_prompt
from lazy_skill_router_inventory import INVENTORY_SCHEMA, inventory_revision


def write_inventory(path: Path) -> None:
    skills = [
        {
            "configured_name": "ponytail",
            "canonical_id": "user/codex/skills/ponytail",
            "description": "Choose the smallest implementation that works with no unnecessary dependency.",
            "aliases": [],
            "capabilities": [],
            "phases": [],
            "availability": {"status": "available"},
        }
    ]
    path.write_text(
        json.dumps(
            {
                "schema": INVENTORY_SCHEMA,
                "revision": inventory_revision(skills),
                "generated_at": "2026-07-12T00:00:00Z",
                "skills": skills,
            }
        ),
        encoding="utf-8",
    )


class CapabilityCliTest(unittest.TestCase):
    def test_build_validate_and_route_shadow_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_path = root / "skills.manifest.json"
            index_path = root / "capability-index.json"
            routes_path = root / "routes.json"
            write_inventory(inventory_path)
            routes_path.write_text(
                json.dumps({"capabilityRetrieval": {"mode": "off"}, "routes": []}),
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                built = capability_main(
                    [
                        "build",
                        "--codex-home",
                        str(root),
                        "--inventory",
                        str(inventory_path),
                        "--output",
                        str(index_path),
                    ]
                )
            self.assertEqual(built, 0)
            self.assertEqual(load_capability_index(index_path).state, "available")

            output = io.StringIO()
            with redirect_stdout(output):
                validated = capability_main(["validate", "--codex-home", str(root), "--index", str(index_path)])
            self.assertEqual(validated, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "available")

            output = io.StringIO()
            with redirect_stdout(output):
                routed = route_prompt(
                    [
                        "--config",
                        str(routes_path),
                        "--inventory",
                        str(inventory_path),
                        "--capability-index",
                        str(index_path),
                        "--capability-shadow-json",
                        "Use ponytail for the smallest implementation.",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(routed, 0)
            self.assertEqual(result["status"], "matched")
            self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "ponytail")
            self.assertFalse(result["semantics"]["affectsLegacySelection"])

    def test_build_failure_is_reported_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            error = io.StringIO()
            with redirect_stderr(error):
                result = capability_main(
                    [
                        "build",
                        "--codex-home",
                        temp_dir,
                        "--inventory",
                        str(Path(temp_dir) / "missing.json"),
                    ]
                )
        self.assertEqual(result, 1)
        self.assertIn("skill inventory is unavailable", error.getvalue())


if __name__ == "__main__":
    unittest.main()
