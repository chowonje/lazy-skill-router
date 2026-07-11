from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lazy_skill_router_inventory import (
    MAX_SKILL_DOCUMENT_BYTES,
    InventoryDiff,
    build_inventory_manifest,
    content_digest,
)
from sync_skills import (
    MAX_SKILL_FRONTMATTER_BYTES,
    MAX_SKILL_FRONTMATTER_LINES,
    SkillRecord,
    SkillScanIssue,
    build_report,
    format_report,
    format_sync_plan,
    frontmatter_metadata,
)

ROOT = Path(__file__).resolve().parents[1]
ROUTE_PATHS = (ROOT / "routes.default.json", ROOT / "routes.template.json")


def load_routes(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def docs_exclusion(path: Path) -> str:
    routes = load_routes(path)["routes"]
    docs_route = next(route for route in routes if route["name"] == "docs")
    return docs_route["excludePatterns"][0]


class RegexAndInputSafetyTest(unittest.TestCase):
    def assert_injected_controls_escaped(self, output: str) -> None:
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)
        self.assertNotIn("\U000e0001", output)
        self.assertIn("\\u001B", output)
        self.assertIn("\\u202E", output)
        self.assertIn("\\U000E0001", output)

    def test_shipped_route_tables_reject_unanchored_leading_lookaheads(self) -> None:
        for path in ROUTE_PATHS:
            with self.subTest(path=path.name):
                data = load_routes(path)
                for route in data["routes"]:
                    for field in ("patterns", "excludePatterns"):
                        for raw_pattern in route.get(field, []):
                            pattern = raw_pattern.get("regex") if isinstance(raw_pattern, dict) else raw_pattern
                            if not isinstance(pattern, str):
                                continue
                            self.assertFalse(
                                pattern.startswith(("(?=", "(?!")),
                                f"{path.name}:{route['name']}.{field} must anchor leading lookaheads",
                            )

    def test_anchored_docs_exclusion_preserves_action_and_prose_semantics(self) -> None:
        prose_prompts = (
            "README의 코드 예시 설명 문장 다듬어줘",
            "Fix the README code example wording",
        )
        action_prompts = (
            "Python 코드 고치고 README 문서도 같이 업데이트해줘",
            "Fix the code and update the documentation",
            "새 function을 추가하고 README 예시도 고쳐줘",
        )

        for path in ROUTE_PATHS:
            pattern = docs_exclusion(path)
            with self.subTest(path=path.name):
                self.assertTrue(pattern.startswith("^(?="))
                for prompt in prose_prompts:
                    self.assertIsNone(re.search(pattern, prompt, re.IGNORECASE), prompt)
                for prompt in action_prompts:
                    self.assertIsNotNone(re.search(pattern, prompt, re.IGNORECASE), prompt)

    def test_frontmatter_metadata_reads_only_a_bounded_prefix(self) -> None:
        class GuardedReader(io.BytesIO):
            def __init__(self, value: bytes) -> None:
                super().__init__(value)
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                if size < 0 or size > MAX_SKILL_FRONTMATTER_BYTES:
                    raise AssertionError("frontmatter read exceeded its byte bound")
                return super().read(size)

        reader = GuardedReader(
            b"---\nname: bounded\ndescription: bounded metadata\n---\n" + b"x" * (MAX_SKILL_FRONTMATTER_BYTES * 2)
        )

        with mock.patch.object(Path, "open", return_value=reader):
            name, description = frontmatter_metadata(Path("/bounded/SKILL.md"))

        self.assertEqual(name, "bounded")
        self.assertEqual(description, "bounded metadata")
        self.assertEqual(reader.read_sizes, [MAX_SKILL_FRONTMATTER_BYTES])

    def test_frontmatter_metadata_bounds_a_huge_unclosed_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "SKILL.md"
            path.write_bytes(b"---\nname: " + b"x" * (MAX_SKILL_FRONTMATTER_BYTES * 2))

            name, description = frontmatter_metadata(path)

        self.assertIsNone(name)
        self.assertEqual(description, "")

    def test_frontmatter_metadata_handles_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "SKILL.md"
            path.write_bytes(b"---\nname: invalid-\xff\n---\n")

            name, description = frontmatter_metadata(path)

        self.assertIsNone(name)
        self.assertEqual(description, "")

    def test_frontmatter_metadata_rejects_a_delimiter_beyond_the_line_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "SKILL.md"
            metadata_lines = ["---", "name: too-late"]
            metadata_lines.extend("description: filler" for _ in range(MAX_SKILL_FRONTMATTER_LINES - 2))
            metadata_lines.append("---")
            path.write_text("\n".join(metadata_lines), encoding="utf-8")

            name, description = frontmatter_metadata(path)

        self.assertIsNone(name)
        self.assertEqual(description, "")

    def test_content_digest_streams_without_path_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "SKILL.md"
            path.write_bytes(b"abc")

            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("full byte read")):
                digest, reason = content_digest(path)

        self.assertEqual(digest, "sha256:" + hashlib.sha256(b"abc").hexdigest())
        self.assertIsNone(reason)

    def test_oversized_skill_document_gets_a_distinct_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_root = root / "codex"
            agents_root = root / "agents"
            path = codex_root / "skills" / "oversized" / "SKILL.md"
            path.parent.mkdir(parents=True)
            with path.open("wb") as handle:
                handle.write(b"---\nname: oversized\n---\n")
                handle.truncate(MAX_SKILL_DOCUMENT_BYTES + 1)

            digest, reason = content_digest(path)
            manifest = build_inventory_manifest((SkillRecord("oversized", path),), codex_root, agents_root)

        skill = manifest["skills"][0]
        self.assertIsNone(digest)
        self.assertEqual(reason, "skill_document_too_large")
        self.assertIsNone(skill["content_digest"])
        self.assertEqual(skill["availability"]["checks"]["skill_document"], "invalid")
        self.assertIn("skill_document_too_large", skill["availability"]["reason_codes"])
        self.assertNotIn("skill_document_unreadable", skill["availability"]["reason_codes"])

    def test_unreadable_skill_document_preserves_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_root = root / "codex"
            agents_root = root / "agents"
            missing_path = codex_root / "skills" / "missing" / "SKILL.md"

            manifest = build_inventory_manifest((SkillRecord("missing", missing_path),), codex_root, agents_root)

        skill = manifest["skills"][0]
        self.assertIsNone(skill["content_digest"])
        self.assertIn("skill_document_unreadable", skill["availability"]["reason_codes"])
        self.assertNotIn("skill_document_too_large", skill["availability"]["reason_codes"])

    def test_human_report_escapes_category_c_values(self) -> None:
        unsafe = "safe-skill\x1b\u202e\U000e0001AUDIT"
        report = build_report({}, (SkillRecord(unsafe, Path("/scan-only/SKILL.md")),))

        output = format_report(report, Path(f"/routes-{unsafe}.json"))

        self.assert_injected_controls_escaped(output)

    def test_human_sync_plan_and_scan_warnings_escape_category_c_values(self) -> None:
        unsafe = "safe-skill\x1b\u202e\U000e0001AUDIT"
        report = build_report({}, (SkillRecord(unsafe, Path("/scan-only/SKILL.md")),))
        diff = InventoryDiff(
            "missing",
            None,
            f"sha256:{unsafe}",
            ({"configured_name": unsafe},),
            (),
            (),
            (),
        )
        issues = (SkillScanIssue("codex", unsafe, f"reason-{unsafe}"),)

        output = format_sync_plan(
            diff,
            report,
            Path(f"/manifest-{unsafe}.json"),
            applied=False,
            scan_issues=issues,
        )

        self.assert_injected_controls_escaped(output)


if __name__ == "__main__":
    unittest.main()
