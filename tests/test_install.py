from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from install import InstallError, install_hook_command, smoke_hook
from lazy_skill_router_inventory import load_inventory_manifest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "install.py"
DOCTOR_PATH = ROOT / "doctor.py"


def write_skill(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


def run_install(codex_home: Path, agents_home: Path) -> subprocess.CompletedProcess[str]:
    return run_install_with_args(codex_home, agents_home, [])


def run_install_with_args(
    codex_home: Path, agents_home: Path, extra_args: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(INSTALL_PATH),
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def run_doctor(codex_home: Path, agents_home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
    )


def write_hooks(codex_home: Path, commands: list[str]) -> None:
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command} for command in commands]}]
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def lazy_router_command(marker: Path) -> str:
    return shlex.join(
        [
            "python3",
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            "lazy_skill_router.py",
        ]
    )


def enable_route_logging(codex_home: Path, log_path: Path) -> None:
    routes_path = codex_home / "lazy-skill-router" / "routes.json"
    routes = json.loads(routes_path.read_text(encoding="utf-8"))
    routes["logging"] = {"enabled": True, "path": str(log_path)}
    routes_path.write_text(json.dumps(routes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def narrow_custom_routes() -> dict[str, Any]:
    return {
        "allowedSkills": ["personal-skill-router"],
        "routes": [
            {
                "name": "custom-only",
                "primary": "personal-skill-router",
                "supporting": [],
                "verification": "",
                "reason": "custom only",
                "patterns": ["^specialtoken$"],
            }
        ],
    }


def narrow_custom_template() -> dict[str, Any]:
    return {
        "routes": [
            {
                "name": "custom-only",
                "primaryCandidates": ["personal-skill-router"],
                "reason": "custom only",
                "patterns": ["^specialtoken$"],
            }
        ]
    }


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class InstallTest(unittest.TestCase):
    def test_install_hook_command_uses_standalone_python3_with_spaced_paths(self) -> None:
        # Given: hook and route paths that require shell quoting.
        hook_path = Path("/tmp/Codex Home/hooks/lazy_skill_router.py")
        routes_path = Path("/tmp/Codex Home/lazy-skill-router/routes.json")

        # When: the installer serializes the hook command.
        command = install_hook_command(hook_path, routes_path)

        # Then: the command keeps standalone python3 semantics and round-trips through shell parsing.
        self.assertEqual(shlex.split(command), ["python3", str(hook_path), "--config", str(routes_path)])

    def test_installer_generates_routes_smokes_hook_then_registers_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
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
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("generate routes", completed.stdout)
            self.assertIn("validate generated routes", completed.stdout)
            self.assertIn("smoke test hook", completed.stdout)
            self.assertIn("added hook entry", completed.stdout)

            routes = json.loads((codex_home / "lazy-skill-router" / "routes.json").read_text(encoding="utf-8"))
            self.assertEqual(routes["allowedSkills"], ["personal-skill-router"])
            self.assertEqual(routes["routes"][0]["primary"], "personal-skill-router")

            manifest_path = codex_home / "lazy-skill-router" / "skills.manifest.json"
            manifest = load_inventory_manifest(manifest_path)
            self.assertEqual(manifest.state, "available")
            self.assertEqual(manifest.match_count("personal-skill-router"), 1)
            self.assertIn("write skill inventory manifest", completed.stdout)

            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            hook_command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            self.assertIn("lazy_skill_router.py", hook_command)

            structured = subprocess.run(
                [
                    sys.executable,
                    str(codex_home / "hooks" / "lazy_skill_router.py"),
                    "--config",
                    str(codex_home / "lazy-skill-router" / "routes.json"),
                    "--recommendation-json",
                    "스킬 추천해줘",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(structured.returncode, 0, structured.stderr)
            self.assertEqual(json.loads(structured.stdout)["producer"]["inventory_state"], "available")

    def test_registered_command_emits_hook_envelope_with_spaced_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lazy router ") as temp_dir:
            # Given: a temp install rooted at paths containing spaces.
            root = Path(temp_dir)
            codex_home = root / "Codex Home"
            agents_home = root / "Agents Home"
            install = run_install(codex_home, agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            # When: the trusted generated command is executed with a controlled hook event.
            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            completed = subprocess.run(
                shlex.split(command),
                input=json.dumps({"prompt": "스킬 추천해줘"}, ensure_ascii=False),
                check=False,
                capture_output=True,
                text=True,
            )

            # Then: the command uses python3 and emits the real UserPromptSubmit envelope.
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(shlex.split(command)[0], "python3")
            payload = json.loads(completed.stdout)
            output = payload["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "UserPromptSubmit")
            self.assertTrue(output["additionalContext"])

    def test_smoke_hook_rejects_non_envelope_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: a hook-shaped executable that only prints dry-run diagnostics.
            root = Path(temp_dir)
            fake_hook = root / "lazy_skill_router.py"
            fake_routes = root / "routes.json"
            fake_hook.write_text("import json\nprint(json.dumps({'shouldInject': True}))\n", encoding="utf-8")
            fake_routes.write_text("{}", encoding="utf-8")

            # When / Then: installer smoke must require the real hook envelope.
            with self.assertRaises(InstallError):
                smoke_hook(fake_hook, fake_routes, "스킬 추천해줘")

    def test_install_smoke_failure_leaves_no_target_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lazy router smoke failure ") as temp_dir:
            # Given: fresh spaced homes with generated or byte-pinned pre-existing routes.
            root = Path(temp_dir)
            for route_mode in ("generated", "existing"):
                with self.subTest(route_mode=route_mode):
                    scenario_root = root / f"{route_mode} scenario"
                    codex_home = scenario_root / "Codex Home"
                    agents_home = scenario_root / "Agents Home"
                    routes_path = codex_home / "lazy-skill-router" / "routes.json"
                    expected_files: tuple[str, ...] = ()
                    existing_routes = b""
                    if route_mode == "existing":
                        routes_path.parent.mkdir(parents=True, exist_ok=True)
                        existing_routes = (ROOT / "routes.default.json").read_bytes()
                        routes_path.write_bytes(existing_routes)
                        expected_files = ("lazy-skill-router/routes.json",)

                    # When: a no-route smoke prompt makes install fail.
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(INSTALL_PATH),
                            "--codex-home",
                            str(codex_home),
                            "--agents-home",
                            str(agents_home),
                            "--smoke-prompt",
                            "hello",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    # Then: no target artifact is added and existing routes stay byte-identical.
                    self.assertEqual(completed.returncode, 1, completed.stdout)
                    self.assertIn("hook smoke test did not return JSON", completed.stderr)
                    target_files = tuple(
                        sorted(
                            path.relative_to(codex_home).as_posix() for path in codex_home.rglob("*") if path.is_file()
                        )
                    )
                    self.assertEqual(target_files, expected_files)
                    if existing_routes:
                        self.assertEqual(routes_path.read_bytes(), existing_routes)
                    self.assertFalse(agents_home.exists())

    def test_valid_custom_narrow_route_default_install_and_doctor_do_not_fail_when_default_prompt_does_not_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="lazy router custom default ") as temp_dir:
            # Given: an existing schema-valid custom config whose only route matches specialtoken.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = codex_home / "lazy-skill-router" / "routes.json"
            write_json_file(routes_path, narrow_custom_routes())

            # When: install and doctor run their implicit default smoke.
            install = run_install(codex_home, agents_home)

            # Then: the default smoke succeeds even though the real custom config does not route that prompt.
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn("keep existing routes", install.stdout)
            self.assertIn("smoke test hook", install.stdout)

            default_hook = subprocess.run(
                [
                    sys.executable,
                    str(codex_home / "hooks" / "lazy_skill_router.py"),
                    "--config",
                    str(routes_path),
                ],
                input=json.dumps({"prompt": "스킬 추천해줘"}, ensure_ascii=False),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(default_hook.returncode, 0, default_hook.stderr)
            self.assertEqual(default_hook.stdout, "")

            doctor = run_doctor(codex_home, agents_home)
            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn("[OK] hook smoke test passed", doctor.stdout)

    def test_explicit_smoke_prompt_hello_against_no_match_config_fails_before_target_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="lazy router explicit custom ") as temp_dir:
            root = Path(temp_dir)

            with self.subTest(route_mode="fresh generated narrow"):
                # Given: no target files and a template that generates only a specialtoken route.
                codex_home = root / "fresh-codex"
                agents_home = root / "fresh-agents"
                template_path = root / "custom-template.json"
                write_json_file(template_path, narrow_custom_template())

                # When: the user explicitly smokes with a prompt the candidate config will not route.
                completed = run_install_with_args(
                    codex_home,
                    agents_home,
                    ["--template", str(template_path), "--smoke-prompt", "hello"],
                )

                # Then: strict smoke fails before any target file or skill is written.
                self.assertEqual(completed.returncode, 1, completed.stdout)
                self.assertIn("hook smoke test did not return JSON", completed.stderr)
                target_files = tuple(
                    sorted(path.relative_to(codex_home).as_posix() for path in codex_home.rglob("*") if path.is_file())
                )
                self.assertEqual(target_files, ())
                self.assertFalse(agents_home.exists())

            with self.subTest(route_mode="existing custom"):
                # Given: an existing custom route file that must not be rewritten on failed explicit smoke.
                codex_home = root / "existing-codex"
                agents_home = root / "existing-agents"
                routes_path = codex_home / "lazy-skill-router" / "routes.json"
                write_json_file(routes_path, narrow_custom_routes())
                before = routes_path.read_bytes()

                # When: explicit hello runs against that real no-match config.
                completed = run_install_with_args(codex_home, agents_home, ["--smoke-prompt", "hello"])

                # Then: the route file stays byte-identical and no later install artifacts are created.
                self.assertEqual(completed.returncode, 1, completed.stdout)
                self.assertIn("hook smoke test did not return JSON", completed.stderr)
                self.assertEqual(routes_path.read_bytes(), before)
                self.assertFalse((codex_home / "hooks" / "lazy_skill_router.py").exists())
                self.assertFalse((codex_home / "hooks.json").exists())
                self.assertFalse((codex_home / "skills" / "personal-skill-router").exists())
                self.assertFalse(agents_home.exists())

            with self.subTest(route_mode="existing custom explicit match"):
                # Given: the same narrow route table.
                codex_home = root / "matching-codex"
                agents_home = root / "matching-agents"
                routes_path = codex_home / "lazy-skill-router" / "routes.json"
                write_json_file(routes_path, narrow_custom_routes())

                # When: the user explicitly smokes with the one prompt the custom config matches.
                completed = run_install_with_args(codex_home, agents_home, ["--smoke-prompt", "specialtoken"])

                # Then: strict explicit smoke passes against the real custom route config.
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("keep existing routes", completed.stdout)
                self.assertTrue((codex_home / "hooks" / "lazy_skill_router.py").is_file())

    def test_doctor_fails_drifted_command_without_executing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: a valid install whose stored command has drifted to marker-writing code.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_install(codex_home, agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)
            marker = root / "marker.txt"
            write_hooks(codex_home, [lazy_router_command(marker)])

            # When: doctor checks the install.
            doctor = run_doctor(codex_home, agents_home)

            # Then: drift is a failure and the stored command was never executed.
            self.assertEqual(doctor.returncode, 1, doctor.stdout)
            self.assertIn("[FAIL] UserPromptSubmit hook registered", doctor.stdout)
            self.assertFalse(marker.exists())

    def test_install_refuses_duplicate_router_entries_before_mutating_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: a hooks.json with multiple lazy-skill-router command entries.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            first_marker = root / "first-marker.txt"
            second_marker = root / "second-marker.txt"
            write_hooks(codex_home, [lazy_router_command(first_marker), lazy_router_command(second_marker)])

            # When: install runs against the duplicate pre-existing state.
            completed = run_install(codex_home, agents_home)

            # Then: it fails before copying hook, skill, routes, or executing stored commands.
            self.assertEqual(completed.returncode, 1, completed.stdout)
            self.assertIn("multiple lazy-skill-router", completed.stderr)
            self.assertFalse((codex_home / "hooks" / "lazy_skill_router.py").exists())
            self.assertFalse((codex_home / "lazy-skill-router" / "routes.json").exists())
            self.assertFalse((codex_home / "skills" / "personal-skill-router").exists())
            self.assertFalse(first_marker.exists())
            self.assertFalse(second_marker.exists())

    def test_doctor_fails_duplicate_router_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: a valid install with an added duplicate lazy-skill-router command entry.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_install(codex_home, agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)
            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            write_hooks(codex_home, [command, command])

            # When: doctor checks registration health.
            doctor = run_doctor(codex_home, agents_home)

            # Then: duplicate router entries are unhealthy.
            self.assertEqual(doctor.returncode, 1, doctor.stdout)
            self.assertIn("[FAIL] UserPromptSubmit hook registered", doctor.stdout)
            self.assertIn("multiple lazy-skill-router", doctor.stdout)

    def test_doctor_smoke_does_not_append_configured_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: an installed router whose route config enables JSONL logging.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            log_path = root / "doctor-log.jsonl"
            install = run_install_with_args(codex_home, agents_home, ["--enable-measurement"])
            self.assertEqual(install.returncode, 0, install.stderr)
            enable_route_logging(codex_home, log_path)

            # When: doctor runs its canonical hook smoke.
            doctor = run_doctor(codex_home, agents_home)

            # Then: doctor remains read-only with respect to configured logging state.
            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn("[OK] hook smoke test passed", doctor.stdout)
            self.assertFalse(log_path.exists())

    def test_installer_can_enable_visible_router_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--show-router-notice",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("enable visible router notice", completed.stdout)
            routes = json.loads((codex_home / "lazy-skill-router" / "routes.json").read_text(encoding="utf-8"))
            self.assertEqual(routes["display"], {"showRouterNotice": True})

            hook = subprocess.run(
                [
                    sys.executable,
                    str(codex_home / "hooks" / "lazy_skill_router.py"),
                    "--config",
                    str(codex_home / "lazy-skill-router" / "routes.json"),
                    "--prompt",
                    "스킬 추천해줘",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertIn("Visible notice requested", hook.stdout)
            self.assertIn("`lazy-skill-router`", hook.stdout)
            self.assertNotIn("lazy-skill-router:", hook.stdout)

    def test_installer_does_not_register_hook_when_template_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            template_path = root / "routes.template.json"
            template_path.write_text(
                json.dumps(
                    {
                        "routes": [
                            {
                                "name": "missing",
                                "primaryCandidates": ["not-installed"],
                                "patterns": ["missing"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--template",
                    str(template_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("generated 0 routes", completed.stderr)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_installer_rejects_malformed_existing_routes_before_copying_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Given: an existing routes.json that cannot be parsed.
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            routes_path = codex_home / "lazy-skill-router" / "routes.json"
            routes_path.parent.mkdir(parents=True, exist_ok=True)
            routes_path.write_text("{bad json\n", encoding="utf-8")

            # When: install runs against the malformed existing config.
            completed = run_install(codex_home, agents_home)

            # Then: it fails before copying hook, skill, or hook registration artifacts.
            self.assertEqual(completed.returncode, 1, completed.stdout)
            self.assertIn("ERROR:", completed.stderr)
            self.assertFalse((codex_home / "hooks" / "lazy_skill_router.py").exists())
            self.assertFalse((codex_home / "skills" / "personal-skill-router").exists())
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_installer_dry_run_prints_planned_hooks_diff_without_writing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_PATH),
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Planned hooks.json diff:", completed.stdout)
            self.assertIn('+    "UserPromptSubmit": [', completed.stdout)
            self.assertIn("lazy_skill_router.py", completed.stdout)
            self.assertIn('+            "command": "python3 ', completed.stdout)
            self.assertFalse((codex_home / "hooks.json").exists())

    def test_doctor_reports_installed_hook_health(self) -> None:
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
            )
            self.assertEqual(install.returncode, 0, install.stderr)

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
            )

            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn("[OK] Codex home found", doctor.stdout)
            self.assertIn("[OK] routes.json validates", doctor.stdout)
            self.assertIn("[OK] UserPromptSubmit hook registered", doctor.stdout)
            self.assertIn("[OK] hook smoke test passed", doctor.stdout)
            self.assertIn("[OK] skill sync checked", doctor.stdout)
            self.assertIn("[OK] skill inventory manifest validates", doctor.stdout)

    def test_doctor_fails_when_inventory_manifest_revision_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = run_install(codex_home, agents_home)
            self.assertEqual(install.returncode, 0, install.stderr)

            manifest_path = codex_home / "lazy-skill-router" / "skills.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skills"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            doctor = run_doctor(codex_home, agents_home)

            self.assertEqual(doctor.returncode, 1)
            self.assertIn("[FAIL] skill inventory manifest validates: inventory_revision_mismatch", doctor.stdout)

    def test_doctor_warns_duplicate_skill_names_with_guidance(self) -> None:
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
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            write_skill(agents_home / "skills" / "personal-skill-router-copy" / "SKILL.md", "personal-skill-router")

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
            )

            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn("[WARN] skill sync checked: 1 duplicate skill name", doctor.stdout)
            self.assertIn("not an install failure", doctor.stdout)
            self.assertIn("examples: personal-skill-router (2 copies:", doctor.stdout)
            self.assertIn("run sync_skills.py --json for full paths", doctor.stdout)

    def test_doctor_fails_when_hook_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"

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
            )

            self.assertEqual(doctor.returncode, 1)
            self.assertIn("[FAIL] Codex home found", doctor.stdout)
            self.assertIn("[FAIL] UserPromptSubmit hook registered", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
