from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"
ROUTED_PROMPT = "PDF 만들어줘"
NO_MATCH_FIELDS = {
    "activation",
    "activationDecision",
    "activationReason",
    "answerOnly",
    "candidates",
    "confidence",
    "matchedPatterns",
    "matchedPatternIds",
    "matchedSignals",
    "reason",
    "requestMode",
    "score",
    "shouldInject",
    "shouldActivate",
}


def isolated_env(codex_home: Path, *, config_env: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env.pop("LAZY_SKILL_ROUTER_CONFIG", None)
    env.pop("LAZY_SKILL_ROUTER_DEBUG", None)
    if config_env is not None:
        env["LAZY_SKILL_ROUTER_CONFIG"] = str(config_env)
    return env


def run_hook(env: dict[str, str], config: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(HOOK_PATH)]
    if config is not None:
        args.extend(["--config", str(config)])
    return subprocess.run(
        args,
        input=json.dumps({"prompt": ROUTED_PROMPT}, ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


def run_diagnostics(
    env: dict[str, str],
    *,
    config: Path | None = None,
    cli: bool,
) -> subprocess.CompletedProcess[str]:
    if cli:
        args = [sys.executable, "-m", CLI_MODULE, "route", "--json"]
        if config is not None:
            args.extend(["--config", str(config)])
        args.append(ROUTED_PROMPT)
    else:
        args = [sys.executable, str(HOOK_PATH)]
        if config is not None:
            args.extend(["--config", str(config)])
        args.extend(["--dry-run", ROUTED_PROMPT])
    return subprocess.run(args, check=False, capture_output=True, text=True, cwd=ROOT, env=env)


def write_route_config(path: Path, route: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "allowedSkills": ["personal-skill-router"],
                "routes": [
                    {
                        "name": route,
                        "primary": "personal-skill-router",
                        "supporting": [],
                        "verification": "",
                        "reason": f"{route} precedence fixture",
                        "patterns": ["PDF"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class FailOpenMatrixTest(unittest.TestCase):
    def assert_quiet_no_route(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def assert_no_route_diagnostics(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), NO_MATCH_FIELDS)
        self.assertFalse(payload["shouldInject"])
        self.assertFalse(payload["shouldActivate"])
        self.assertEqual(payload["activationDecision"], "abstain")
        self.assertEqual(payload["requestMode"], "action")
        self.assertEqual(payload["candidates"], [])

    def assert_override_fails_open(self, override: Path, env: dict[str, str]) -> None:
        self.assert_quiet_no_route(run_hook(env, override))
        for cli in (False, True):
            with self.subTest(surface="cli" if cli else "dry-run"):
                self.assert_no_route_diagnostics(run_diagnostics(env, config=override, cli=cli))

    def test_explicit_missing_or_invalid_config_blocks_lower_precedence_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = isolated_env(root / "codex")
            invalid = root / "invalid.json"
            invalid.write_text("{bad json\n", encoding="utf-8")
            non_object = root / "non-object.json"
            non_object.write_text("[]\n", encoding="utf-8")
            unreadable = root / "config-directory"
            unreadable.mkdir()

            for override in (root / "missing.json", invalid, non_object, unreadable):
                with self.subTest(override=override.name):
                    self.assert_override_fails_open(override, env)

    def test_environment_missing_or_invalid_config_blocks_lower_precedence_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid = root / "invalid.json"
            invalid.write_text("{bad json\n", encoding="utf-8")
            non_object = root / "non-object.json"
            non_object.write_text("[]\n", encoding="utf-8")
            unreadable = root / "config-directory"
            unreadable.mkdir()

            for override in (root / "missing.json", invalid, non_object, unreadable):
                env = isolated_env(root / "codex", config_env=override)
                with self.subTest(override=override.name):
                    self.assert_quiet_no_route(run_hook(env))
                    for cli in (False, True):
                        self.assert_no_route_diagnostics(run_diagnostics(env, cli=cli))

    def test_invalid_installed_config_blocks_bundled_default_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            installed = codex_home / "lazy-skill-router" / "routes.json"
            installed.parent.mkdir(parents=True)
            env = isolated_env(codex_home)

            for installed_state in ("invalid", "non-object", "unreadable", "broken-symlink"):
                with self.subTest(installed_state=installed_state):
                    if installed.is_symlink() or installed.is_file():
                        installed.unlink()
                    elif installed.is_dir():
                        installed.rmdir()
                    if installed_state == "invalid":
                        installed.write_text("{bad json\n", encoding="utf-8")
                    elif installed_state == "non-object":
                        installed.write_text("[]\n", encoding="utf-8")
                    elif installed_state == "unreadable":
                        installed.mkdir()
                    else:
                        installed.symlink_to(root / "missing-target.json")

                    self.assert_quiet_no_route(run_hook(env))
                    for cli in (False, True):
                        self.assert_no_route_diagnostics(run_diagnostics(env, cli=cli))

    def test_missing_installed_config_still_uses_bundled_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = isolated_env(Path(temp_dir) / "codex")

            hook = run_hook(env)
            self.assertEqual(hook.returncode, 0, hook.stderr)
            output = json.loads(hook.stdout)["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "UserPromptSubmit")
            self.assertIn("Route: pdf", output["additionalContext"])

            source = json.loads(run_diagnostics(env, cli=False).stdout)
            cli = json.loads(run_diagnostics(env, cli=True).stdout)
            self.assertEqual(source, cli)
            self.assertTrue(source["shouldInject"])
            self.assertEqual(source["route"], "pdf")

    def test_valid_config_precedence_is_explicit_then_environment_then_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            installed = codex_home / "lazy-skill-router" / "routes.json"
            environment = root / "environment.json"
            explicit = root / "explicit.json"
            write_route_config(installed, "installed")
            write_route_config(environment, "environment")
            write_route_config(explicit, "explicit")

            scenarios = (
                ("explicit", isolated_env(codex_home, config_env=environment), explicit),
                ("environment", isolated_env(codex_home, config_env=environment), None),
                ("installed", isolated_env(codex_home), None),
            )
            for expected_route, env, config in scenarios:
                with self.subTest(expected_route=expected_route):
                    hook = run_hook(env, config)
                    self.assertEqual(hook.returncode, 0, hook.stderr)
                    context = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
                    self.assertIn(f"Route: {expected_route}", context)
                    for cli in (False, True):
                        diagnostics = json.loads(run_diagnostics(env, config=config, cli=cli).stdout)
                        self.assertTrue(diagnostics["shouldInject"])
                        self.assertEqual(diagnostics["route"], expected_route)

    def test_invalid_route_entries_are_skipped_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "routes.json"
            config.write_text(json.dumps({"routes": [{"name": "broken"}]}), encoding="utf-8")
            env = isolated_env(root / "codex")

            self.assert_quiet_no_route(run_hook(env, config))
            for cli in (False, True):
                self.assert_no_route_diagnostics(run_diagnostics(env, config=config, cli=cli))


if __name__ == "__main__":
    unittest.main()
