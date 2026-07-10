from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_contracts import structured_recommendation_v1
from lazy_skill_router_inventory import build_inventory_manifest, load_inventory_manifest
from sync_skills import SkillRecord

ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = ROOT / "sync_skills.py"
HOOK_PATH = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"


def write_skill(path: Path, name: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n# {name}\n{body}", encoding="utf-8")


class InventoryManifestTest(unittest.TestCase):
    def test_manifest_revision_is_deterministic_and_paths_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "private-codex"
            agents_home = root / "private-agents"
            skill_path = codex_home / "skills" / "pdf" / "SKILL.md"
            write_skill(skill_path, "pdf", "layout guidance\n")
            records = (SkillRecord("pdf", skill_path),)

            first = build_inventory_manifest(records, codex_home, agents_home, generated_at="2026-07-10T00:00:00Z")
            second = build_inventory_manifest(records, codex_home, agents_home, generated_at="2026-07-11T00:00:00Z")

        self.assertEqual(first["revision"], second["revision"])
        encoded = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(str(root), encoded)
        self.assertNotIn("layout guidance", encoded)
        self.assertEqual(first["skills"][0]["canonical_id"], "user/codex/skills/pdf")
        self.assertEqual(first["skills"][0]["locator_ref"], "codex:skills/pdf/SKILL.md")

    def test_plugin_identity_uses_owner_plugin_namespace_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = (
                codex_home
                / "plugins"
                / "cache"
                / "sisyphuslabs"
                / "omo"
                / "4.13.0"
                / "skills"
                / "programming"
                / "SKILL.md"
            )
            write_skill(skill_path, "programming")

            manifest = build_inventory_manifest(
                (SkillRecord("omo:programming", skill_path),),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )

        skill = manifest["skills"][0]
        self.assertEqual(skill["canonical_id"], "plugin/sisyphuslabs/omo/programming")
        self.assertEqual(skill["provider"], {"type": "plugin", "id": "sisyphuslabs"})
        self.assertEqual(skill["namespace"], "omo")
        self.assertEqual(skill["revision"], "4.13.0")

    def test_duplicate_configured_names_are_ambiguous_not_arbitrarily_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            codex_skill = codex_home / "skills" / "same" / "SKILL.md"
            agents_skill = agents_home / "skills" / "same" / "SKILL.md"
            write_skill(codex_skill, "same")
            write_skill(agents_skill, "same")

            manifest = build_inventory_manifest(
                (SkillRecord("same", codex_skill), SkillRecord("same", agents_skill)),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )

        self.assertEqual(len(manifest["skills"]), 2)
        for skill in manifest["skills"]:
            self.assertEqual(skill["availability"]["status"], "unknown")
            self.assertIn("duplicate_configured_name", skill["availability"]["reason_codes"])

    def test_manifest_loader_validates_revision_and_structured_contract_resolves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = codex_home / "skills" / "pdf" / "SKILL.md"
            manifest_path = root / "skills.manifest.json"
            write_skill(skill_path, "pdf")
            manifest = build_inventory_manifest(
                (SkillRecord("pdf", skill_path),),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            snapshot = load_inventory_manifest(manifest_path)
            config = {"routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}]}
            contract = structured_recommendation_v1("pdf", config, snapshot)

            tampered = dict(manifest)
            tampered["skills"] = []
            tampered_path = root / "tampered.json"
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            invalid_snapshot = load_inventory_manifest(tampered_path)

        self.assertEqual(snapshot.state, "available")
        self.assertEqual(contract["producer"]["inventory_state"], "available")
        self.assertEqual(contract["producer"]["inventory_revision"], manifest["revision"])
        skill_ref = contract["recommendations"][0]["skills"][0]["skill_ref"]
        self.assertEqual(skill_ref["canonical_id"], "user/codex/skills/pdf")
        self.assertEqual(invalid_snapshot.state, "invalid")

    def test_sync_cli_writes_inventory_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = root / "skills.manifest.json"
            write_skill(codex_home / "skills" / "pdf" / "SKILL.md", "pdf")
            routes_path.write_text(
                json.dumps({"allowedSkills": ["pdf"], "routes": [{"name": "pdf", "primary": "pdf"}]}),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--routes",
                    str(routes_path),
                    "--manifest-output",
                    str(manifest_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

            snapshot = load_inventory_manifest(manifest_path)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(snapshot.state, "available")
        self.assertIn("wrote skill inventory manifest", completed.stdout)

    def test_source_and_packaged_cli_use_the_same_explicit_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = codex_home / "skills" / "pdf" / "SKILL.md"
            manifest_path = root / "skills.manifest.json"
            config_path = root / "routes.json"
            write_skill(skill_path, "pdf")
            manifest = build_inventory_manifest(
                (SkillRecord("pdf", skill_path),),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            config_path.write_text(
                json.dumps({"routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}]}),
                encoding="utf-8",
            )
            source = subprocess.run(
                [
                    sys.executable,
                    str(HOOK_PATH),
                    "--config",
                    str(config_path),
                    "--inventory",
                    str(manifest_path),
                    "--recommendation-json",
                    "pdf",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            packaged = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "route",
                    "--recommendation-json",
                    "--inventory",
                    str(manifest_path),
                    "--config",
                    str(config_path),
                    "pdf",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(source.returncode, 0, source.stderr)
        self.assertEqual(packaged.returncode, 0, packaged.stderr)
        self.assertEqual(json.loads(source.stdout), json.loads(packaged.stdout))
        self.assertEqual(json.loads(source.stdout)["producer"]["inventory_state"], "available")


if __name__ == "__main__":
    unittest.main()
