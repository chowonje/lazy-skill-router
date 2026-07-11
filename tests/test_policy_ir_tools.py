from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from install import smoke_config_for_prompt
from lazy_skill_router_inventory import InventorySnapshot
from sync_skills import build_report_for_names, has_blocking_policy_findings

ROOT = Path(__file__).resolve().parents[1]


def v2_router_config(*, state: str = "active", skill: str = "personal-skill-router") -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "policyVersion": "tool-integration-test",
        "selection": {
            "mode": "ranked",
            "maxRecommendations": 1,
            "minMatchStrength": 0.55,
            "minScoreMargin": 0.05,
        },
        "skillBindings": {"router": skill},
        "routes": [
            {
                "id": "router",
                "intent": "route_skills",
                "capabilityRequirements": {"primary": ["router"]},
                "match": {"any": [{"id": "router.token", "regex": "router"}]},
                "lifecycle": {"state": state},
            }
        ],
    }


def run_script(
    script: str,
    codex_home: Path,
    agents_home: Path,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / script),
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            *extra_args,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class PolicyIRToolIntegrationTest(unittest.TestCase):
    def test_v2_existing_routes_install_and_doctor_smoke_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            route_path = codex_home / "lazy-skill-router" / "routes.json"
            route_path.parent.mkdir(parents=True)
            original = json.dumps(v2_router_config(), indent=2) + "\n"
            route_path.write_text(original, encoding="utf-8")

            probe, prompt = smoke_config_for_prompt(v2_router_config(), None)
            installed = run_script("install.py", codex_home, agents_home)
            doctor = run_script("doctor.py", codex_home, agents_home)

            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
            self.assertEqual(route_path.read_text(encoding="utf-8"), original)
            self.assertEqual(probe["schemaVersion"], 2)
            self.assertEqual(prompt, "lazy-skill-router-internal-probe")
            self.assertTrue((codex_home / "hooks" / "lazy_skill_router_policy_ir.py").is_file())
            self.assertIn("[OK] hook smoke test passed", doctor.stdout)

    def test_v2_without_an_active_primary_fails_before_install_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            route_path = codex_home / "lazy-skill-router" / "routes.json"
            route_path.parent.mkdir(parents=True)
            route_path.write_text(json.dumps(v2_router_config(state="shadow")), encoding="utf-8")

            installed = run_script("install.py", codex_home, agents_home)

            self.assertEqual(installed.returncode, 1)
            self.assertIn("eligible active route primary unavailable", installed.stderr)
            self.assertFalse((codex_home / "hooks" / "lazy_skill_router.py").exists())
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_sync_report_resolves_v2_references_and_blocks_canonical_mismatch(self) -> None:
        config = v2_router_config(skill="missing-skill")
        inventory = InventorySnapshot("available", "revision", ())

        missing = build_report_for_names(config, (), set(), inventory)

        self.assertEqual([reference.skill for reference in missing.route_references_missing], ["missing-skill"])
        self.assertTrue(has_blocking_policy_findings(missing))
        self.assertEqual(missing.policy_schema_version, 2)
        self.assertEqual(missing.resolved_references[0].status, "missing")

        config["skillBindings"] = {"router": {"skill": "personal-skill-router", "canonicalId": "plugin/wrong/router"}}
        available = {
            "configured_name": "personal-skill-router",
            "canonical_id": "user/codex/skills/personal-skill-router",
            "availability": {"status": "available"},
        }
        mismatch = build_report_for_names(
            config,
            (),
            {"personal-skill-router"},
            InventorySnapshot("available", "revision", (available,)),
        )

        self.assertTrue(has_blocking_policy_findings(mismatch))
        self.assertIn("skill_canonical_id_mismatch", {finding.code for finding in mismatch.policy_findings})
        self.assertEqual(mismatch.resolved_references[0].status, "canonical_mismatch")

    def test_disabled_v2_route_with_missing_binding_is_not_blocking(self) -> None:
        config = v2_router_config(state="disabled", skill="removed-skill")
        report = build_report_for_names(config, (), set(), InventorySnapshot("available", "revision", ()))

        self.assertEqual(report.route_references, ())
        self.assertEqual(report.route_references_missing, ())
        self.assertFalse(has_blocking_policy_findings(report))
        self.assertNotIn("skill_unavailable_or_ambiguous", {finding.code for finding in report.policy_findings})

    def test_shadow_v2_route_with_missing_binding_is_not_blocking(self) -> None:
        config = v2_router_config(state="shadow", skill="removed-skill")
        report = build_report_for_names(config, (), set(), InventorySnapshot("available", "revision", ()))

        self.assertEqual(report.route_references, ())
        self.assertEqual(report.route_references_missing, ())
        self.assertFalse(has_blocking_policy_findings(report))
        self.assertEqual(report.resolved_references, ())


if __name__ == "__main__":
    unittest.main()
