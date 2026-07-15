from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_capability_index import build_capability_index
from lazy_skill_router_host_catalog import (
    build_host_catalog,
    effective_skill_names,
    load_host_catalog,
    reconcile_inventory,
)
from lazy_skill_router_inventory import build_inventory_manifest, diff_inventory, load_inventory_manifest
from lazy_skill_router_retrieval import retrieve_capabilities
from sync_skills import SkillRecord

ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = ROOT / "sync_skills.py"
INSTALL_PATH = ROOT / "install.py"
DOCTOR_PATH = ROOT / "doctor.py"
CLI_MODULE = "lazy_skill_router_cli.cli"


def write_skill(path: Path, name: str, description: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description or name}\n---\n# {name}\nprivate body\n",
        encoding="utf-8",
    )


class HostCatalogTest(unittest.TestCase):
    def test_catalog_build_cli_seals_an_app_llm_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            draft_path = root / "host-catalog.draft.json"
            output_path = root / "host-catalog.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "host": "codex",
                        "complete": False,
                        "skills": [
                            {
                                "name": "pdf",
                                "description": "Work with PDF files.",
                                "source": "user",
                                "enabled": True,
                                "allowImplicitInvocation": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "catalog",
                    "build",
                    "--input",
                    str(draft_path),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            validate = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "catalog",
                    "validate",
                    "--input",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            snapshot = load_host_catalog(output_path)

        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(snapshot.skills[0]["name"], "pdf")
        self.assertIn("Revision: sha256:", build.stdout)

    def test_catalog_build_rejects_a_symlinked_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            linked_parent = root / "linked"
            draft_path = root / "host-catalog.draft.json"
            (outside / "nested").mkdir(parents=True)
            linked_parent.symlink_to(outside, target_is_directory=True)
            draft_path.write_text(
                json.dumps(
                    {
                        "host": "codex",
                        "complete": False,
                        "skills": [
                            {
                                "name": "pdf",
                                "description": "Work with PDF files.",
                                "source": "user",
                                "enabled": True,
                                "allowImplicitInvocation": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "catalog",
                    "build",
                    "--codex-home",
                    str(root),
                    "--input",
                    str(draft_path),
                    "--output",
                    str(linked_parent / "nested" / "host-catalog.json"),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("refusing unsafe catalog write", completed.stderr)
        self.assertFalse((outside / "nested" / "host-catalog.json").exists())

    def test_catalog_build_rejects_unknown_skill_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            draft_path = root / "host-catalog.draft.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "host": "codex",
                        "complete": False,
                        "skills": [
                            {
                                "name": "pdf",
                                "description": "Work with PDF files.",
                                "source": "user",
                                "enabled": True,
                                "allowImplicitInvocation": True,
                                "command": "do-not-run",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "catalog",
                    "build",
                    "--input",
                    str(draft_path),
                    "--output",
                    str(root / "host-catalog.json"),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("unsupported fields: command", completed.stderr)

    def test_catalog_revision_is_deterministic_and_loader_rejects_tampering(self) -> None:
        skills = [
            {
                "name": "system-skill",
                "description": "System capability",
                "source": "system",
                "enabled": True,
                "allowImplicitInvocation": True,
            }
        ]
        first = build_host_catalog("codex", skills, complete=True, generated_at="2026-07-10T00:00:00Z")
        second = build_host_catalog("codex", skills, complete=True, generated_at="2026-07-11T00:00:00Z")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "host-catalog.json"
            path.write_text(json.dumps(first), encoding="utf-8")
            loaded = load_host_catalog(path)
            tampered = dict(first)
            tampered["complete"] = False
            path.write_text(json.dumps(tampered), encoding="utf-8")
            invalid = load_host_catalog(path)

        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(loaded.state, "available")
        self.assertEqual(invalid.state, "invalid")
        self.assertEqual(invalid.reason_codes, ("host_catalog_revision_mismatch",))

    def test_optional_bilingual_metadata_flows_from_host_catalog_to_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            skill_path = codex_home / "skills" / "security-threat-model" / "SKILL.md"
            write_skill(skill_path, "security-threat-model", "Build a repository grounded threat model.")
            filesystem = build_inventory_manifest(
                (SkillRecord("security-threat-model", skill_path),),
                codex_home,
                agents_home,
            )
            catalog = build_host_catalog(
                "codex",
                [
                    {
                        "name": "security-threat-model",
                        "description": "Build a repository grounded threat model.",
                        "source": "user",
                        "enabled": True,
                        "allowImplicitInvocation": True,
                        "aliases": ["보안 위협 모델", "위협 모델링"],
                        "capabilities": ["신뢰 경계와 공격 경로 분석"],
                    }
                ],
                complete=True,
            )
            catalog_path = root / "host-catalog.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            loaded_catalog = load_host_catalog(catalog_path)
            reconciled = reconcile_inventory(filesystem, loaded_catalog)
            inventory_path = root / "skills.manifest.json"
            inventory_path.write_text(json.dumps(reconciled, ensure_ascii=False), encoding="utf-8")
            inventory = load_inventory_manifest(inventory_path)
            index_path = root / "capability-index.json"
            index_path.write_text(
                json.dumps(build_capability_index(inventory), ensure_ascii=False),
                encoding="utf-8",
            )
            result = retrieve_capabilities(
                "이 저장소의 보안 위협 모델을 작성해줘",
                {
                    "_loaded_from": str(root / "routes.json"),
                    "capabilityRetrieval": {"mode": "shadow", "maxCandidates": 3},
                },
                inventory,
            )

        resolved = inventory.resolve("security-threat-model")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["aliases"], ["보안 위협 모델", "위협 모델링"])
        self.assertEqual(resolved["capabilities"], ["신뢰 경계와 공격 경로 분석"])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "security-threat-model")
        self.assertIn("metadata.alias.word", result["candidates"][0]["evidenceIds"])

    def test_optional_metadata_is_bounded_and_rejects_invalid_values(self) -> None:
        base = {
            "name": "pdf",
            "description": "PDF work",
            "source": "user",
            "enabled": True,
            "allowImplicitInvocation": True,
        }
        invalid_values = (
            {"aliases": "not-a-list"},
            {"aliases": ["same", "same"]},
            {"aliases": ["x"] * 9},
            {"capabilities": ["x"] * 17},
            {"capabilities": ["x" * 161]},
        )
        for metadata in invalid_values:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    build_host_catalog("codex", [{**base, **metadata}], complete=True)

    def test_complete_host_catalog_marks_disabled_and_filesystem_only_skills_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            pdf_path = codex_home / "skills" / "pdf" / "SKILL.md"
            stale_path = codex_home / "skills" / "stale" / "SKILL.md"
            write_skill(pdf_path, "pdf")
            write_skill(stale_path, "stale")
            filesystem = build_inventory_manifest(
                (SkillRecord("pdf", pdf_path), SkillRecord("stale", stale_path)),
                codex_home,
                agents_home,
            )
            catalog_data = build_host_catalog(
                "codex",
                [
                    {
                        "name": "pdf",
                        "description": "PDF work",
                        "source": "user",
                        "enabled": False,
                        "allowImplicitInvocation": False,
                    },
                    {
                        "name": "system-skill",
                        "description": "System capability",
                        "source": "system",
                        "enabled": True,
                        "allowImplicitInvocation": True,
                        "aliases": ["시스템 스킬"],
                        "capabilities": ["호스트 전용 기능"],
                    },
                    {
                        "name": "manual-only",
                        "description": "Available only when invoked explicitly.",
                        "source": "system",
                        "enabled": True,
                        "allowImplicitInvocation": False,
                    },
                ],
                complete=True,
            )
            catalog_path = root / "host-catalog.json"
            catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")
            reconciled = reconcile_inventory(filesystem, load_host_catalog(catalog_path))
            manifest_path = root / "skills.manifest.json"
            manifest_path.write_text(json.dumps(reconciled), encoding="utf-8")
            snapshot = load_inventory_manifest(manifest_path)

        statuses = {skill["configured_name"]: skill["availability"]["status"] for skill in reconciled["skills"]}
        self.assertEqual(
            statuses,
            {"manual-only": "unavailable", "pdf": "disabled", "stale": "inactive", "system-skill": "available"},
        )
        self.assertEqual(effective_skill_names(reconciled), {"system-skill"})
        self.assertIsNone(snapshot.resolve("manual-only"))
        self.assertIsNone(snapshot.resolve("pdf"))
        self.assertIsNone(snapshot.resolve("stale"))
        self.assertEqual(snapshot.resolve("system-skill")["provider"], {"type": "host", "id": "codex"})
        self.assertEqual(snapshot.resolve("system-skill")["aliases"], ["시스템 스킬"])
        self.assertEqual(snapshot.resolve("system-skill")["capabilities"], ["호스트 전용 기능"])
        encoded = json.dumps(reconciled)
        self.assertNotIn(str(root), encoded)
        self.assertNotIn("private body", encoded)

    def test_host_catalog_resolves_a_visible_name_when_filesystem_copies_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            first_path = codex_home / "skills" / "same" / "SKILL.md"
            second_path = agents_home / "skills" / "same" / "SKILL.md"
            write_skill(first_path, "same")
            write_skill(second_path, "same")
            filesystem = build_inventory_manifest(
                (SkillRecord("same", first_path), SkillRecord("same", second_path)),
                codex_home,
                agents_home,
            )
            catalog_data = build_host_catalog(
                "codex",
                [
                    {
                        "name": "same",
                        "description": "Host-visible same skill",
                        "source": "user",
                        "enabled": True,
                        "allowImplicitInvocation": True,
                    }
                ],
                complete=True,
            )
            catalog_path = root / "host-catalog.json"
            catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")
            reconciled = reconcile_inventory(filesystem, load_host_catalog(catalog_path))
            manifest_path = root / "skills.manifest.json"
            manifest_path.write_text(json.dumps(reconciled), encoding="utf-8")
            snapshot = load_inventory_manifest(manifest_path)
            diff = diff_inventory(load_inventory_manifest(root / "missing.json"), reconciled)

        self.assertEqual(snapshot.match_count("same"), 3)
        self.assertEqual(snapshot.resolve("same")["provider"], {"type": "host", "id": "codex"})
        self.assertEqual(diff.ambiguous_names, ())

    def test_sync_plan_uses_host_visible_system_skills_without_modifying_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = root / "routes.json"
            catalog_path = root / "host-catalog.json"
            write_skill(codex_home / "skills" / "stale-cache" / "SKILL.md", "stale-cache")
            routes_path.write_text(
                json.dumps(
                    {
                        "allowedSkills": ["system-skill"],
                        "routes": [{"name": "system", "primary": "system-skill"}],
                    }
                ),
                encoding="utf-8",
            )
            routes_before = routes_path.read_bytes()
            catalog = build_host_catalog(
                "codex",
                [
                    {
                        "name": "system-skill",
                        "description": "System capability",
                        "source": "system",
                        "enabled": True,
                        "allowImplicitInvocation": True,
                    }
                ],
                complete=True,
            )
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

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
                    "--host-catalog",
                    str(catalog_path),
                    "--plan",
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            result = json.loads(completed.stdout)
            routes_after = routes_path.read_bytes()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["hostCatalog"]["state"], "available")
        self.assertEqual(result["routes"]["routeReferencesMissing"], [])
        self.assertEqual(result["routes"]["installedNotAllowlisted"], [])
        self.assertEqual(routes_before, routes_after)

    def test_install_preserves_host_catalog_and_doctor_accepts_reconciled_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            catalog_path = codex_home / "lazy-skill-router" / "host-catalog.json"
            catalog_path.parent.mkdir(parents=True)
            catalog = build_host_catalog(
                "codex",
                [
                    {
                        "name": "personal-skill-router",
                        "description": "Route tasks to installed skills.",
                        "source": "user",
                        "enabled": True,
                        "allowImplicitInvocation": True,
                    }
                ],
                complete=True,
            )
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            catalog_before = catalog_path.read_bytes()

            install = subprocess.run(
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
            catalog_after = catalog_path.read_bytes()

        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn("[OK] skill inventory freshness checked", doctor.stdout)
        self.assertEqual(catalog_before, catalog_after)

    def test_doctor_fails_when_host_catalog_disables_an_active_filesystem_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            catalog_path = codex_home / "lazy-skill-router" / "host-catalog.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(
                json.dumps(
                    build_host_catalog(
                        "codex",
                        [
                            {
                                "name": "personal-skill-router",
                                "description": "Route tasks.",
                                "source": "user",
                                "enabled": False,
                                "allowImplicitInvocation": False,
                            }
                        ],
                        complete=True,
                    )
                ),
                encoding="utf-8",
            )
            install = subprocess.run(
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

        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(doctor.returncode, 1)
        self.assertIn("[FAIL] skill sync checked: 1 active route skills missing", doctor.stdout)

    def test_doctor_fails_when_host_catalog_changed_after_inventory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            catalog_path = codex_home / "lazy-skill-router" / "host-catalog.json"
            catalog_path.parent.mkdir(parents=True)

            def catalog(description: str) -> dict:
                return build_host_catalog(
                    "codex",
                    [
                        {
                            "name": "personal-skill-router",
                            "description": description,
                            "source": "user",
                            "enabled": True,
                            "allowImplicitInvocation": True,
                        }
                    ],
                    complete=True,
                )

            catalog_path.write_text(json.dumps(catalog("Old description")), encoding="utf-8")
            install = subprocess.run(
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
            self.assertEqual(install.returncode, 0, install.stderr)
            catalog_path.write_text(json.dumps(catalog("New description")), encoding="utf-8")

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

        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        self.assertIn("[FAIL] skill inventory freshness checked", doctor.stdout)
        self.assertIn("host catalog changed", doctor.stdout)

    def test_sync_apply_refreshes_generated_bundle_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = subprocess.run(
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
            self.assertEqual(install.returncode, 0, install.stderr)
            catalog_path = codex_home / "lazy-skill-router" / "host-catalog.json"
            catalog_path.write_text(
                json.dumps(
                    build_host_catalog(
                        "codex",
                        [
                            {
                                "name": "personal-skill-router",
                                "description": "Route tasks.",
                                "source": "user",
                                "enabled": True,
                                "allowImplicitInvocation": True,
                            }
                        ],
                        complete=True,
                    )
                ),
                encoding="utf-8",
            )
            sync = subprocess.run(
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

        self.assertEqual(sync.returncode, 0, sync.stderr)
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn("[OK] capability index validates", doctor.stdout)
        self.assertIn("[OK] install ownership manifest validates", doctor.stdout)
        self.assertIn("[OK] hook smoke test passed", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
