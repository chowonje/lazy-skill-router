from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import sync_skills as sync_module
from lazy_skill_router_capability_index import load_capability_index
from lazy_skill_router_contracts import structured_recommendation_v1
from lazy_skill_router_install_manifest import manifest_revision
from lazy_skill_router_inventory import build_inventory_manifest, diff_inventory, load_inventory_manifest
from sync_skills import SkillRecord, candidate_issue, scan_installed_skills, scan_installed_skills_with_issues

ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = ROOT / "sync_skills.py"
INSTALL_PATH = ROOT / "install.py"
DOCTOR_PATH = ROOT / "doctor.py"
HOOK_PATH = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"


def write_skill(path: Path, name: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n# {name}\n{body}", encoding="utf-8")


@contextmanager
def forbid_reads_from(sentinel: Path):
    sentinel = sentinel.resolve(strict=True)
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def guarded_read_text(path: Path, *args, **kwargs):
        if path.resolve(strict=False) == sentinel:
            raise AssertionError(f"unexpected text read from {sentinel.name}")
        return original_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path: Path, *args, **kwargs):
        if path.resolve(strict=False) == sentinel:
            raise AssertionError(f"unexpected byte read from {sentinel.name}")
        return original_read_bytes(path, *args, **kwargs)

    with (
        mock.patch.object(Path, "read_text", guarded_read_text),
        mock.patch.object(Path, "read_bytes", guarded_read_bytes),
    ):
        yield


class InventoryManifestTest(unittest.TestCase):
    def test_default_sync_rejects_hardlink_ownership_alias_with_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            installed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            generated_root = codex_home / "lazy-skill-router"
            tracked_paths = (
                generated_root / "skills.manifest.json",
                generated_root / "capability-index.json",
                generated_root / "install.manifest.json",
            )
            manifest_path = tracked_paths[2]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            runtime_record = next(
                artifact for artifact in manifest["artifacts"] if artifact.get("path") == "hooks/lazy_skill_router.py"
            )
            alias_path = codex_home / "hooks" / "lazy_skill_router_alias.py"
            os.link(codex_home / str(runtime_record["path"]), alias_path)
            alias_record = dict(runtime_record)
            alias_record["path"] = "hooks/lazy_skill_router_alias.py"
            manifest["artifacts"].append(alias_record)
            manifest["revision"] = manifest_revision(manifest["artifacts"], manifest["registration"])
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in tracked_paths}

            result = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("physical aliases", result.stderr)
            self.assertEqual({path: path.read_bytes() for path in tracked_paths}, before)

    def test_sync_rejects_parent_swap_after_final_preflight_without_touching_sentinels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            write_skill(codex_home / "skills" / "pdf" / "SKILL.md", "pdf")
            routes_path.write_text(
                json.dumps({"allowedSkills": ["pdf"], "routes": [{"name": "pdf", "primary": "pdf"}]}),
                encoding="utf-8",
            )
            output_parent = root / "output"
            output_parent.mkdir()
            manifest_path = output_parent / "skills.manifest.json"
            moved_parent = root / "moved-output"
            outside = root / "outside"
            outside.mkdir()
            sentinel_bytes = b'{"sentinel":"keep"}\n'
            outside_manifest = outside / "skills.manifest.json"
            outside_index = outside / "capability-index.json"
            outside_manifest.write_bytes(sentinel_bytes)
            outside_index.write_bytes(sentinel_bytes)
            real_stage_bytes = sync_module._stage_bytes
            swapped = False

            def swap_parent_after_preflight(path: Path, content: bytes, mode: int, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    output_parent.rename(moved_parent)
                    output_parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return real_stage_bytes(path, content, mode, *args, **kwargs)

            argv = [
                "sync_skills.py",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
                "--routes",
                str(routes_path),
                "--manifest",
                str(manifest_path),
                "--apply",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(sync_module, "_stage_bytes", side_effect=swap_parent_after_preflight),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = sync_module.main()

            outside_manifest_after = outside_manifest.read_bytes()
            outside_index_after = outside_index.read_bytes()

        self.assertEqual(result, 1, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(outside_manifest_after, sentinel_bytes)
        self.assertEqual(outside_index_after, sentinel_bytes)

    def test_sync_rejects_leaf_swap_after_staging_without_touching_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "skills.manifest.json"
            destination.write_text('{"original":true}\n', encoding="utf-8")
            sentinel = root / "outside.json"
            sentinel_bytes = b'{"sentinel":"keep"}\n'
            sentinel.write_bytes(sentinel_bytes)
            item = sync_module.JsonBundleItem("inventory", destination, {"updated": True})
            real_replace = sync_module.replace_staged_file
            swapped = False

            def swap_leaf_after_staging(staged, target: Path, *args, **kwargs) -> None:
                nonlocal swapped
                if not swapped:
                    target.unlink()
                    target.symlink_to(sentinel)
                    swapped = True
                real_replace(staged, target, *args, **kwargs)

            with (
                mock.patch.object(sync_module, "replace_staged_file", side_effect=swap_leaf_after_staging),
                self.assertRaises(sync_module.SyncBundleRollbackError),
            ):
                sync_module.apply_json_bundle((item,), root)

            sentinel_after = sentinel.read_bytes()

        self.assertEqual(sentinel_after, sentinel_bytes)

    def test_candidate_resolving_outside_root_is_rejected_with_a_redacted_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan_root = root / "codex" / "skills"
            outside = scan_root / ".." / ".." / "outside" / "SKILL.md"
            write_skill(outside, "outside-root-sentinel")

            issue = candidate_issue(outside, "codex-skills", scan_root)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.root_alias, "codex-skills")
        self.assertEqual(issue.relative_locator, "<unresolved>")
        self.assertEqual(issue.reason_code, "skill_document_outside_root")
        self.assertNotIn(str(root), json.dumps(issue.__dict__))

    def test_scanner_rejects_leaf_symlink_without_reading_or_exporting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "private-codex"
            agents_home = root / "private-agents"
            sentinel = root / "outside" / "SKILL.md"
            linked_skill = codex_home / "skills" / "linked" / "SKILL.md"
            write_skill(sentinel, "outside-leaf-sentinel", "OUTSIDE_LEAF_CONTENT")
            linked_skill.parent.mkdir(parents=True)
            linked_skill.symlink_to(sentinel)

            with forbid_reads_from(sentinel):
                result = scan_installed_skills_with_issues(codex_home, agents_home)
                manifest = build_inventory_manifest(result.records, codex_home, agents_home)

            issue_payload = [issue.__dict__ for issue in result.issues]
            exported = json.dumps(manifest, ensure_ascii=False)

        self.assertEqual(result.records, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].root_alias, "codex-skills")
        self.assertEqual(result.issues[0].relative_locator, "linked/SKILL.md")
        self.assertEqual(result.issues[0].reason_code, "skill_document_symlink")
        self.assertEqual(manifest["skills"], [])
        self.assertNotIn("outside-leaf-sentinel", exported)
        self.assertNotIn(str(root), json.dumps(issue_payload))

    def test_scanner_rejects_symlinked_parent_without_reading_or_exporting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "private-codex"
            agents_home = root / "private-agents"
            outside_directory = root / "outside-parent"
            sentinel = outside_directory / "nested" / "SKILL.md"
            linked_parent = codex_home / "skills" / "linked-parent"
            write_skill(sentinel, "outside-parent-sentinel", "OUTSIDE_PARENT_CONTENT")
            linked_parent.parent.mkdir(parents=True)
            linked_parent.symlink_to(outside_directory, target_is_directory=True)

            with forbid_reads_from(sentinel):
                result = scan_installed_skills_with_issues(codex_home, agents_home)
                manifest = build_inventory_manifest(result.records, codex_home, agents_home)

            issue_payload = [issue.__dict__ for issue in result.issues]
            exported = json.dumps(manifest, ensure_ascii=False)

        self.assertEqual(result.records, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].root_alias, "codex-skills")
        self.assertEqual(result.issues[0].relative_locator, "linked-parent")
        self.assertEqual(result.issues[0].reason_code, "skill_document_symlinked_parent")
        self.assertEqual(manifest["skills"], [])
        self.assertNotIn("outside-parent-sentinel", exported)
        self.assertNotIn(str(root), json.dumps(issue_payload))

    def test_scanner_rejects_symlinked_skill_root_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "private-codex"
            agents_home = root / "private-agents"
            outside_directory = root / "outside-skills"
            sentinel = outside_directory / "nested" / "SKILL.md"
            skill_root = codex_home / "skills"
            write_skill(sentinel, "outside-root-sentinel", "OUTSIDE_ROOT_CONTENT")
            skill_root.parent.mkdir(parents=True)
            skill_root.symlink_to(outside_directory, target_is_directory=True)

            with forbid_reads_from(sentinel):
                result = scan_installed_skills_with_issues(codex_home, agents_home)

        self.assertEqual(result.records, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].root_alias, "codex-skills")
        self.assertEqual(result.issues[0].relative_locator, ".")
        self.assertEqual(result.issues[0].reason_code, "skill_document_symlinked_parent")
        self.assertNotIn(str(root), json.dumps([issue.__dict__ for issue in result.issues]))

    def test_richer_scan_result_preserves_ordinary_and_legacy_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            write_skill(codex_home / "skills" / "codex-skill" / "SKILL.md", "codex-skill")
            write_skill(agents_home / "skills" / "agents-skill" / "SKILL.md", "agents-skill")

            result = scan_installed_skills_with_issues(codex_home, agents_home)
            legacy_records = scan_installed_skills(codex_home, agents_home)

        self.assertEqual([record.name for record in result.records], ["agents-skill", "codex-skill"])
        self.assertEqual(result.issues, ())
        self.assertEqual(legacy_records, result.records)

    def test_scanner_includes_frontmatter_description_but_not_skill_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = codex_home / "skills" / "multiline" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\n"
                "name: multiline\n"
                "description: >\n"
                "  Handle a focused task.\n"
                "  name: must-not-override\n"
                "  Use only when requested.\n"
                "---\n"
                "private body must not enter inventory\n",
                encoding="utf-8",
            )

            records = sync_records = scan_installed_skills(codex_home, agents_home)
            manifest = build_inventory_manifest(records, codex_home, agents_home)
            encoded = json.dumps(manifest)

        self.assertEqual(
            sync_records[0].description,
            "Handle a focused task. name: must-not-override Use only when requested.",
        )
        self.assertEqual(sync_records[0].name, "multiline")
        self.assertEqual(manifest["skills"][0]["description"], sync_records[0].description)
        self.assertNotIn("private body must not enter inventory", encoded)

    def test_scanner_ignores_unclosed_frontmatter_instead_of_reading_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = codex_home / "skills" / "broken" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: claimed-name\ndescription: claimed description\nprivate body\n",
                encoding="utf-8",
            )

            records = scan_installed_skills(codex_home, agents_home)

        self.assertEqual(records[0].name, "broken")
        self.assertEqual(records[0].description, "")

    def test_scanner_ignores_hidden_host_managed_skill_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            write_skill(codex_home / "skills" / "visible" / "SKILL.md", "visible")
            write_skill(codex_home / "skills" / ".system" / "transient" / "SKILL.md", "transient")

            records = scan_installed_skills(codex_home, agents_home)

        self.assertEqual([record.name for record in records], ["visible"])

    def test_inventory_diff_reports_added_removed_and_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            old_pdf = codex_home / "skills" / "pdf" / "SKILL.md"
            removed = codex_home / "skills" / "removed" / "SKILL.md"
            added = codex_home / "skills" / "added" / "SKILL.md"
            write_skill(old_pdf, "pdf", "old\n")
            write_skill(removed, "removed")
            previous_manifest = build_inventory_manifest(
                (SkillRecord("pdf", old_pdf), SkillRecord("removed", removed)),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )
            previous_path = root / "previous.json"
            previous_path.write_text(json.dumps(previous_manifest), encoding="utf-8")

            write_skill(old_pdf, "pdf", "new\n")
            write_skill(added, "added")
            current_manifest = build_inventory_manifest(
                (SkillRecord("pdf", old_pdf), SkillRecord("added", added)),
                codex_home,
                agents_home,
                generated_at="2026-07-11T00:00:00Z",
            )
            diff = diff_inventory(load_inventory_manifest(previous_path), current_manifest)

        self.assertEqual([item["configured_name"] for item in diff.added], ["added"])
        self.assertEqual([item["configured_name"] for item in diff.removed], ["removed"])
        self.assertEqual([item["configured_name"] for item in diff.changed], ["pdf"])
        self.assertEqual(diff.changed[0]["fields"], ["content_digest"])

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
                codex_home / "plugins" / "cache" / "acme" / "custom" / "4.13.0" / "skills" / "programming" / "SKILL.md"
            )
            write_skill(skill_path, "programming")

            manifest = build_inventory_manifest(
                (SkillRecord("custom:programming", skill_path),),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )

        skill = manifest["skills"][0]
        self.assertEqual(skill["canonical_id"], "plugin/acme/custom/programming")
        self.assertEqual(skill["provider"], {"type": "plugin", "id": "acme"})
        self.assertEqual(skill["namespace"], "custom")
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

    def test_duplicate_canonical_ids_across_names_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = (
                codex_home / "plugins" / "cache" / "provider" / "namespace" / "1.0" / "skills" / "pdf" / "SKILL.md"
            )
            write_skill(skill_path, "pdf")

            manifest = build_inventory_manifest(
                (SkillRecord("namespace:pdf", skill_path), SkillRecord("alias:pdf", skill_path)),
                codex_home,
                agents_home,
                generated_at="2026-07-10T00:00:00Z",
            )
            manifest_path = root / "skills.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = load_inventory_manifest(manifest_path)

        self.assertEqual(len({skill["canonical_id"] for skill in manifest["skills"]}), 1)
        for skill in manifest["skills"]:
            self.assertIn("duplicate_canonical_id", skill["availability"]["reason_codes"])
            self.assertEqual(skill["availability"]["checks"]["identity"], "ambiguous")
        self.assertIsNone(snapshot.resolve("namespace:pdf"))
        self.assertIsNone(snapshot.resolve("alias:pdf"))

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

    def test_sync_reports_redacted_scan_issues_without_changing_strict_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "private-codex"
            agents_home = root / "private-agents"
            routes_path = root / "routes.json"
            sentinel = root / "outside" / "SKILL.md"
            linked_skill = codex_home / "skills" / "linked" / "SKILL.md"
            write_skill(codex_home / "skills" / "visible" / "SKILL.md", "visible")
            write_skill(sentinel, "outside-cli-sentinel", "OUTSIDE_CLI_CONTENT")
            linked_skill.parent.mkdir(parents=True)
            linked_skill.symlink_to(sentinel)
            routes_path.write_text(
                json.dumps({"allowedSkills": ["visible"], "routes": [{"name": "visible", "primary": "visible"}]}),
                encoding="utf-8",
            )
            common_args = [
                sys.executable,
                str(SYNC_PATH),
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
                "--routes",
                str(routes_path),
                "--strict",
            ]

            json_completed = subprocess.run(
                [*common_args, "--json"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            plan_json_completed = subprocess.run(
                [*common_args, "--plan", "--json"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            human_completed = subprocess.run(
                common_args,
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            payload = json.loads(json_completed.stdout)
            plan_payload = json.loads(plan_json_completed.stdout)

        self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
        self.assertEqual(plan_json_completed.returncode, 0, plan_json_completed.stderr)
        self.assertEqual(human_completed.returncode, 0, human_completed.stderr)
        expected_issues = [
            {
                "rootAlias": "codex-skills",
                "relativeLocator": "linked/SKILL.md",
                "reasonCode": "skill_document_symlink",
            }
        ]
        self.assertEqual(payload["scanIssues"], expected_issues)
        self.assertEqual(plan_payload["scanIssues"], expected_issues)
        self.assertNotIn(str(root), json.dumps(payload["scanIssues"]))
        self.assertNotIn(str(root), json.dumps(plan_payload["scanIssues"]))
        self.assertNotIn("outside-cli-sentinel", json.dumps(payload))
        self.assertNotIn("outside-cli-sentinel", json.dumps(plan_payload))
        self.assertIn("WARNING: Inventory scan issues", human_completed.stdout)
        self.assertIn(
            "codex-skills:linked/SKILL.md (skill_document_symlink)",
            human_completed.stdout,
        )

    def test_sync_strict_ignores_missing_allowlist_entry_used_only_by_disabled_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            routes_path.write_text(
                json.dumps(
                    {
                        "allowedSkills": ["removed-skill"],
                        "routes": [
                            {
                                "name": "retired",
                                "primary": "removed-skill",
                                "patterns": ["removed"],
                                "lifecycle": {"state": "disabled"},
                            }
                        ],
                    }
                ),
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
                    "--strict",
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["allowedMissing"], ["removed-skill"])
        self.assertEqual(payload["routeReferencesMissing"], [])
        self.assertEqual(payload["resolvedReferences"], [])

    def test_sync_plan_is_read_only_and_custom_apply_updates_inventory_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = root / "skills.manifest.json"
            write_skill(codex_home / "skills" / "pdf" / "SKILL.md", "pdf")
            routes_bytes = json.dumps(
                {"allowedSkills": ["pdf"], "routes": [{"name": "pdf", "primary": "pdf"}]},
                sort_keys=True,
            ).encode()
            routes_path.write_bytes(routes_bytes)

            plan = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--routes",
                    str(routes_path),
                    "--manifest",
                    str(manifest_path),
                    "--plan",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertFalse(manifest_path.exists())

            apply = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--routes",
                    str(routes_path),
                    "--manifest",
                    str(manifest_path),
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

            snapshot = load_inventory_manifest(manifest_path)
            index = load_capability_index(manifest_path.with_name("capability-index.json"))
            final_routes_bytes = routes_path.read_bytes()

        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("read-only; no files changed", plan.stdout)
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(index.state, "available")
        self.assertEqual(index.inventory_revision, snapshot.revision)
        self.assertIn("inventory/index bundle updated; routes preserved", apply.stdout)
        self.assertEqual(final_routes_bytes, routes_bytes)

    def test_sync_apply_honors_manifest_output_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = root / "explicit" / "skills.manifest.json"
            default_manifest_path = codex_home / "lazy-skill-router" / "skills.manifest.json"
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
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

            snapshot = load_inventory_manifest(manifest_path)
            index = load_capability_index(manifest_path.with_name("capability-index.json"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(index.state, "available")
        self.assertFalse(default_manifest_path.exists())
        self.assertIn(str(manifest_path), completed.stdout)

    def test_sync_apply_rejects_a_symlinked_custom_parent_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            outside = root / "outside"
            outside.mkdir()
            linked_output = root / "linked-output"
            linked_output.symlink_to(outside, target_is_directory=True)
            manifest_path = linked_output / "skills.manifest.json"
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
                    "--manifest",
                    str(manifest_path),
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            outside_entries = tuple(outside.iterdir())

        self.assertEqual(completed.returncode, 1)
        self.assertIn("unsafe sync target", completed.stderr)
        self.assertEqual(outside_entries, ())

    def test_default_sync_apply_rejects_a_symlinked_codex_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_parent = root / "real-parent"
            real_codex_home = real_parent / "codex"
            agents_home = root / "agents"
            install = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(real_codex_home),
                    "--agents-home",
                    str(agents_home),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            linked_codex_home = linked_parent / "codex"
            artifact_paths = (
                real_codex_home / "lazy-skill-router" / "skills.manifest.json",
                real_codex_home / "lazy-skill-router" / "capability-index.json",
                real_codex_home / "lazy-skill-router" / "install.manifest.json",
            )
            before = {path: path.read_bytes() for path in artifact_paths}

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_PATH),
                    "--codex-home",
                    str(linked_codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            after = {path: path.read_bytes() for path in artifact_paths}

        self.assertEqual(completed.returncode, 1)
        self.assertIn("unsafe sync target", completed.stderr)
        self.assertEqual(after, before)

    def test_sync_strict_failure_is_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = root / "output" / "skills.manifest.json"
            routes_path.write_text(
                json.dumps(
                    {
                        "allowedSkills": ["missing"],
                        "routes": [{"name": "missing", "primary": "missing", "patterns": ["missing"]}],
                    }
                ),
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
                    "--manifest",
                    str(manifest_path),
                    "--strict",
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertFalse(manifest_path.exists())
        self.assertFalse(manifest_path.with_name("capability-index.json").exists())

    def test_default_sync_apply_requires_an_install_ownership_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = codex_home / "lazy-skill-router" / "skills.manifest.json"
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
                    "--apply",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("install ownership manifest", completed.stderr)
        self.assertFalse(manifest_path.exists())

    def test_custom_sync_rejects_owned_path_collisions_and_aliases_with_zero_writes(self) -> None:
        for collision_kind in ("sibling", "casefold", "hardlink", "owned-directory"):
            with self.subTest(collision_kind=collision_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                agents_home = root / "agents"
                installed = subprocess.run(
                    [
                        sys.executable,
                        str(INSTALL_PATH),
                        "--codex-home",
                        str(codex_home),
                        "--agents-home",
                        str(agents_home),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)
                default_parent = codex_home / "lazy-skill-router"
                default_inventory = default_parent / "skills.manifest.json"
                default_index = default_parent / "capability-index.json"
                install_manifest = default_parent / "install.manifest.json"
                if collision_kind == "sibling":
                    custom_manifest = default_parent / "custom-inventory.json"
                elif collision_kind == "casefold":
                    custom_manifest = default_parent / "Skills.Manifest.JSON"
                elif collision_kind == "hardlink":
                    custom_parent = root / "custom"
                    custom_parent.mkdir()
                    custom_manifest = custom_parent / "skills.manifest.json"
                    os.link(default_inventory, custom_manifest)
                else:
                    custom_manifest = codex_home / "skills" / "personal-skill-router" / "custom-inventory.json"
                owned_before = {
                    default_inventory: default_inventory.read_bytes(),
                    default_index: default_index.read_bytes(),
                    install_manifest: install_manifest.read_bytes(),
                }

                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SYNC_PATH),
                        "--codex-home",
                        str(codex_home),
                        "--agents-home",
                        str(agents_home),
                        "--manifest",
                        str(custom_manifest),
                        "--apply",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                )
                owned_after = {path: path.read_bytes() for path in owned_before}
                sibling_index = custom_manifest.with_name("capability-index.json")

                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn("custom sync target collides", completed.stderr)
                self.assertEqual(owned_after, owned_before)
                if collision_kind not in {"casefold", "hardlink"}:
                    self.assertFalse(custom_manifest.exists())
                if sibling_index != default_index:
                    self.assertFalse(sibling_index.exists())

    def test_sync_bundle_rolls_back_every_replacement_phase(self) -> None:
        for failure_phase in (1, 2, 3):
            with self.subTest(failure_phase=failure_phase), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                paths = (
                    root / "skills.manifest.json",
                    root / "capability-index.json",
                    root / "install.manifest.json",
                )
                originals = {}
                for index, path in enumerate(paths):
                    content = (json.dumps({"original": index}, sort_keys=True) + "\n").encode()
                    path.write_bytes(content)
                    originals[path] = content
                items = tuple(
                    sync_module.JsonBundleItem(f"artifact-{index}", path, {"updated": index})
                    for index, path in enumerate(paths)
                )
                real_replace = sync_module.replace_staged_file
                replacement_count = 0

                def injected_replace(
                    staged: Path,
                    destination: Path,
                    failure_phase: int = failure_phase,
                    real_replace=real_replace,
                ) -> None:
                    nonlocal replacement_count
                    replacement_count += 1
                    if replacement_count == failure_phase:
                        raise OSError(f"injected replacement failure {failure_phase}")
                    real_replace(staged, destination)

                with (
                    mock.patch.object(sync_module, "replace_staged_file", side_effect=injected_replace),
                    self.assertRaisesRegex(OSError, "injected replacement failure"),
                ):
                    sync_module.apply_json_bundle(items, root)

                self.assertEqual({path: path.read_bytes() for path in paths}, originals)
                self.assertEqual(tuple(root.glob(".*.sync-*")), ())

    def test_sync_bundle_attempts_every_restore_and_leaves_partial_state_doctor_detectable(self) -> None:
        for failed_restore_index in range(3):
            with self.subTest(failed_restore_index=failed_restore_index), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex"
                agents_home = root / "agents"
                installed = subprocess.run(
                    [
                        sys.executable,
                        str(INSTALL_PATH),
                        "--codex-home",
                        str(codex_home),
                        "--agents-home",
                        str(agents_home),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)
                generated_root = codex_home / "lazy-skill-router"
                paths = (
                    generated_root / "skills.manifest.json",
                    generated_root / "capability-index.json",
                    generated_root / "install.manifest.json",
                )
                originals = {path: path.read_bytes() for path in paths}
                items = tuple(
                    sync_module.JsonBundleItem(f"artifact-{index}", path, {"invalid": index})
                    for index, path in enumerate(paths)
                )
                real_replace = sync_module.replace_staged_file
                real_restore = sync_module._restore_bundle_snapshot
                replacement_count = 0
                total_paths = len(paths)
                restore_attempts: list[Path] = []
                failed_path = paths[failed_restore_index]

                def fail_after_all_replacements(
                    staged,
                    destination: Path,
                    *args,
                    real_replace=real_replace,
                    total_paths=total_paths,
                    **kwargs,
                ) -> None:
                    nonlocal replacement_count
                    real_replace(staged, destination, *args, **kwargs)
                    replacement_count += 1
                    if replacement_count == total_paths:
                        raise OSError("injected post-commit failure")

                def fail_one_restore(
                    path: Path,
                    *args,
                    restore_attempts=restore_attempts,
                    failed_path=failed_path,
                    real_restore=real_restore,
                    **kwargs,
                ) -> None:
                    restore_attempts.append(path)
                    if path == failed_path:
                        raise OSError(f"injected restore failure: {path.name}")
                    real_restore(path, *args, **kwargs)

                with (
                    mock.patch.object(
                        sync_module,
                        "replace_staged_file",
                        side_effect=fail_after_all_replacements,
                    ),
                    mock.patch.object(sync_module, "_restore_bundle_snapshot", side_effect=fail_one_restore),
                    self.assertRaises(sync_module.SyncBundleRollbackError),
                ):
                    sync_module.apply_json_bundle(items, codex_home)

                doctor = subprocess.run(
                    [
                        sys.executable,
                        str(DOCTOR_PATH),
                        "--codex-home",
                        str(codex_home),
                        "--agents-home",
                        str(agents_home),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                )

                self.assertEqual(set(restore_attempts), set(paths))
                self.assertNotEqual(failed_path.read_bytes(), originals[failed_path])
                self.assertEqual(doctor.returncode, 1, doctor.stdout)

    def test_sync_rollback_preserves_concurrent_replacement_and_restores_other_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = tuple(root / f"artifact-{index}.json" for index in range(3))
            originals = {path: f'{{"original":{index}}}\n'.encode() for index, path in enumerate(paths)}
            for path, content in originals.items():
                path.write_bytes(content)
            items = tuple(
                sync_module.JsonBundleItem(f"artifact-{index}", path, {"updated": index})
                for index, path in enumerate(paths)
            )
            concurrent_path = paths[1]
            concurrent_bytes = b'{"user":"concurrent replacement"}\n'
            real_replace = sync_module.replace_staged_file
            replacement_count = 0

            def replace_all_then_inject_user_write(staged, destination: Path):
                nonlocal replacement_count
                result = real_replace(staged, destination)
                replacement_count += 1
                if replacement_count == len(paths):
                    concurrent_path.unlink()
                    concurrent_path.write_bytes(concurrent_bytes)
                    raise OSError("injected failure after concurrent replacement")
                return result

            with (
                mock.patch.object(
                    sync_module,
                    "replace_staged_file",
                    side_effect=replace_all_then_inject_user_write,
                ),
                self.assertRaises(sync_module.SyncBundleRollbackError),
            ):
                sync_module.apply_json_bundle(items, root)

            self.assertEqual(concurrent_path.read_bytes(), concurrent_bytes)
            self.assertEqual(paths[0].read_bytes(), originals[paths[0]])
            self.assertEqual(paths[2].read_bytes(), originals[paths[2]])

    def test_sync_bundle_failure_removes_created_managed_root_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created_ancestor = root / "created-ancestor"
            managed_root = created_ancestor / "managed"
            target = managed_root / "artifact.json"
            item = sync_module.JsonBundleItem("artifact", target, {"updated": True})

            with (
                mock.patch.object(sync_module, "_stage_bytes", side_effect=OSError("injected stage failure")),
                self.assertRaisesRegex(OSError, "injected stage failure"),
            ):
                sync_module.apply_json_bundle((item,), managed_root)

            self.assertFalse(managed_root.exists())
            self.assertFalse(created_ancestor.exists())

    def test_sync_apply_json_reports_each_artifact_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            manifest_path = root / "output" / "skills.manifest.json"
            write_skill(codex_home / "skills" / "pdf" / "SKILL.md", "pdf")
            routes_path.write_text(
                json.dumps({"allowedSkills": ["pdf"], "routes": [{"name": "pdf", "primary": "pdf"}]}),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SYNC_PATH),
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
                "--routes",
                str(routes_path),
                "--manifest",
                str(manifest_path),
                "--apply",
                "--json",
            ]

            first = subprocess.run(command, check=False, capture_output=True, text=True, cwd=ROOT)
            second = subprocess.run(command, check=False, capture_output=True, text=True, cwd=ROOT)
            first_artifacts = json.loads(first.stdout)["artifacts"]
            second_artifacts = json.loads(second.stdout)["artifacts"]

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first_artifacts["inventory"]["status"], "updated")
        self.assertEqual(first_artifacts["capabilityIndex"]["status"], "updated")
        self.assertEqual(first_artifacts["installManifest"]["status"], "not-applicable")
        self.assertEqual(second_artifacts["inventory"]["status"], "unchanged")
        self.assertEqual(second_artifacts["capabilityIndex"]["status"], "unchanged")
        self.assertEqual(second_artifacts["installManifest"]["status"], "not-applicable")

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
