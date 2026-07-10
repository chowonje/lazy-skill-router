from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lazy_skill_router_contracts import structured_recommendation_v1
from lazy_skill_router_core import load_config


def write_config(path: Path, claimed_trust: str = "spoofed") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_config_trust": claimed_trust,
                "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
            }
        ),
        encoding="utf-8",
    )


class ConfigTrustTest(unittest.TestCase):
    def test_loader_assigns_trust_from_discovery_source_not_config_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = root / "explicit.json"
            environment = root / "environment.json"
            codex_home = root / "codex"
            installed = codex_home / "lazy-skill-router" / "routes.json"
            bundled = root / "package" / "routes.default.json"
            script = bundled.with_name("lazy_skill_router.py")
            for path in (explicit, environment, installed, bundled):
                write_config(path)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                os.environ.pop("LAZY_SKILL_ROUTER_CONFIG", None)
                explicit_config = load_config(script, str(explicit))
                with mock.patch.dict(os.environ, {"LAZY_SKILL_ROUTER_CONFIG": str(environment)}):
                    environment_config = load_config(script, None)
                installed_config = load_config(script, None)
                installed.unlink()
                bundled_config = load_config(script, None)

        self.assertEqual(explicit_config["_config_trust"], "user-selected")
        self.assertEqual(environment_config["_config_trust"], "environment-selected")
        self.assertEqual(installed_config["_config_trust"], "personal-installed")
        self.assertEqual(bundled_config["_config_trust"], "bundled")

    def test_structured_contract_marks_config_trust_as_advisory(self) -> None:
        config = {
            "_config_trust": "personal-installed",
            "routes": [{"name": "pdf", "primary": "pdf", "patterns": ["pdf"]}],
        }

        contract = structured_recommendation_v1("pdf", config)

        self.assertEqual(contract["producer"]["config_trust"], "personal-installed")
        self.assertFalse(contract["semantics"]["config_trust_is_authorization"])


if __name__ == "__main__":
    unittest.main()
