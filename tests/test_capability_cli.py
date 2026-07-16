from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lazy_skill_router_capability_index import capability_main, load_capability_index
from lazy_skill_router_cli.cli import route_prompt
from lazy_skill_router_inventory import INVENTORY_SCHEMA, inventory_revision
from lazy_skill_router_retrieval import PRODUCT_PREVIEW_ALGORITHM, RETRIEVAL_ALGORITHM_V2


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
    def test_overlong_prompt_skips_inventory_before_shadow_abstention(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("lazy_skill_router_cli.cli.inventory_for_config") as load_inventory,
            redirect_stdout(output),
        ):
            result = route_prompt(["--capability-shadow-json", "x" * 4097])

        load_inventory.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["reasonCodes"], ["prompt_too_long"])

    def test_route_help_does_not_hardcode_the_retrieval_algorithm_version(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            route_prompt(["--help"])

        help_text = " ".join(output.getvalue().split())
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("configured capability retrieval shadow diagnostic", help_text)
        self.assertNotIn("retrieval v1 shadow diagnostic", help_text)

    def test_build_validate_and_route_shadow_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_path = root / "skills.manifest.json"
            index_path = root / "new" / "nested" / "capability-index.json"
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
            self.assertEqual(result["algorithm"], PRODUCT_PREVIEW_ALGORITHM)
            self.assertEqual(result["algorithm"], RETRIEVAL_ALGORITHM_V2)
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

    def test_human_route_keeps_no_route_output_when_capability_index_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_path = root / "skills.manifest.json"
            routes_path = root / "routes.json"
            write_inventory(inventory_path)
            routes_path.write_text(json.dumps({"routes": []}), encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                routed = route_prompt(
                    [
                        "--config",
                        str(routes_path),
                        "--inventory",
                        str(inventory_path),
                        "Use ponytail for the smallest implementation.",
                    ]
                )

        self.assertEqual(routed, 0)
        self.assertIn("No route", output.getvalue())
        self.assertNotIn("Possible installed skill matches", output.getvalue())

    def test_json_no_route_contract_does_not_gain_capability_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_path = root / "skills.manifest.json"
            index_path = root / "capability-index.json"
            routes_path = root / "routes.json"
            write_inventory(inventory_path)
            routes_path.write_text(json.dumps({"routes": []}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    capability_main(
                        [
                            "build",
                            "--codex-home",
                            str(root),
                            "--inventory",
                            str(inventory_path),
                            "--output",
                            str(index_path),
                        ]
                    ),
                    0,
                )

            output = io.StringIO()
            with redirect_stdout(output):
                routed = route_prompt(
                    [
                        "--json",
                        "--config",
                        str(routes_path),
                        "--inventory",
                        str(inventory_path),
                        "--capability-index",
                        str(index_path),
                        "Use ponytail for the smallest implementation.",
                    ]
                )

        result = json.loads(output.getvalue())
        self.assertEqual(routed, 0)
        self.assertFalse(result["shouldInject"])
        self.assertNotIn("capabilityCandidates", result)
        self.assertNotIn("retrieval", result)

    def test_answer_only_no_route_does_not_show_capability_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_path = root / "skills.manifest.json"
            index_path = root / "capability-index.json"
            routes_path = root / "routes.json"
            write_inventory(inventory_path)
            routes_path.write_text(
                json.dumps({"answerOnlyPatterns": ["explain"], "routes": []}),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    capability_main(
                        [
                            "build",
                            "--codex-home",
                            str(root),
                            "--inventory",
                            str(inventory_path),
                            "--output",
                            str(index_path),
                        ]
                    ),
                    0,
                )

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
                        "explain ponytail",
                    ]
                )

        self.assertEqual(routed, 0)
        self.assertIn("Answer-only: true", output.getvalue())
        self.assertNotIn("Possible installed skill matches", output.getvalue())


if __name__ == "__main__":
    unittest.main()
