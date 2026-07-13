from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router_logging import MEASUREMENT_EVENT_SCHEMA, ROUTING_OBSERVATION_SCHEMA
from measurement import build_measurement_report
from validate_routes import validate_config

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "lazy_skill_router.py"
CLI_MODULE = "lazy_skill_router_cli.cli"
DEFAULT_CONFIG = ROOT / "routes.default.json"


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def measurement_event(event_type: str, **values: object) -> dict[str, object]:
    return {"schema": MEASUREMENT_EVENT_SCHEMA, "eventType": event_type, **values}


class MeasurementTest(unittest.TestCase):
    def test_report_aggregates_only_current_routing_observations(self) -> None:
        def observation(status: str, action: str, *, schema: str = ROUTING_OBSERVATION_SCHEMA) -> dict[str, object]:
            reason = {
                ("matched", "observe-only"): "ownership_unobserved",
                ("degraded", "fallback-legacy"): "retrieval_unusable",
            }[(status, action)]
            return {
                "schema": schema,
                "lane": "capability-retrieval",
                "mode": "shadow",
                "retrieval": {
                    "revision": None,
                    "status": status,
                    "candidates": ([{"skillId": "reviewer", "evidenceIds": []}] if status == "matched" else []),
                    "latencyMs": None,
                    "reasonCodes": [],
                },
                "ownership": {
                    "status": "unobserved",
                    "primarySkillId": None,
                    "reasonCode": "host_ownership_observation_unavailable",
                },
                "activation": {
                    "source": "legacy-route-plus-activation-ir",
                    "disposition": "propose",
                    "legacyPrimarySkillId": "reviewer",
                    "injected": False,
                },
                "stop": {"action": action, "reasonCode": reason, "affectsLegacySelection": False},
                "semantics": {
                    "rawPromptStored": False,
                    "semanticAbstentionObserved": False,
                    "disagreementIsFallbackEvidence": False,
                    "automaticPromotion": False,
                },
            }

        report = build_measurement_report(
            [
                measurement_event("decision", routingObservation=observation("matched", "observe-only")),
                measurement_event("decision", routingObservation=observation("degraded", "fallback-legacy")),
                measurement_event(
                    "decision",
                    routingObservation=observation(
                        "matched", "observe-only", schema="lazy-skill-router.routing-observation/v999"
                    ),
                ),
                measurement_event(
                    "decision",
                    routingObservation={"schema": ROUTING_OBSERVATION_SCHEMA},
                ),
                measurement_event(
                    "decision",
                    routingObservation={
                        **observation("matched", "observe-only"),
                        "activation": {
                            "source": "legacy-route-plus-activation-ir",
                            "disposition": "abstain",
                            "legacyPrimarySkillId": "reviewer",
                            "injected": True,
                        },
                    },
                ),
                measurement_event(
                    "decision",
                    routingObservation={**observation("matched", "observe-only"), "lane": "wrong-lane"},
                ),
                measurement_event(
                    "decision",
                    routingObservation={
                        **observation("matched", "observe-only"),
                        "retrieval": {
                            **observation("matched", "observe-only")["retrieval"],
                            "candidates": [{"skillId": None, "evidenceIds": [None]}],
                            "reasonCodes": [None],
                        },
                    },
                ),
                measurement_event(
                    "decision",
                    routingObservation={
                        **observation("matched", "observe-only"),
                        "retrieval": {
                            **observation("matched", "observe-only")["retrieval"],
                            "candidates": [{"skillId": "reviewer", "evidenceIds": [{}]}],
                        },
                    },
                ),
                measurement_event(
                    "decision",
                    routingObservation={
                        **observation("matched", "observe-only"),
                        "retrieval": {
                            **observation("matched", "observe-only")["retrieval"],
                            "latencyMs": 10**5000,
                        },
                    },
                ),
                measurement_event("decision"),
            ]
        )

        self.assertEqual(report["events"], 10)
        self.assertEqual(report["ignoredEvents"], 0)
        self.assertEqual(report["routingObservations"]["total"], 2)
        self.assertEqual(report["routingObservations"]["invalid"], 6)
        self.assertEqual(report["routingObservations"]["byOwnershipStatus"], {"unobserved": 2})
        self.assertEqual(
            report["routingObservations"]["byStopAction"],
            {"fallback-legacy": 1, "observe-only": 1},
        )
        self.assertEqual(report["routingObservations"]["legacySelectionAffected"], 0)
        self.assertIn("invalid-routing-observations", report["warnings"])

    def test_activation_dispositions_are_accumulated_separately_from_delivery(self) -> None:
        report = build_measurement_report(
            [
                measurement_event("decision", decisionStatus="matched", activationDisposition="activate"),
                measurement_event("decision", decisionStatus="matched", activationDisposition="propose"),
                measurement_event("decision", decisionStatus="no-match", activationDisposition="abstain"),
            ]
        )

        decisions = report["decisions"]
        self.assertEqual(decisions["activated"], 1)
        self.assertEqual(decisions["proposed"], 1)
        self.assertEqual(decisions["activationAbstained"], 1)
        self.assertEqual(decisions["activationDecisionCoverage"], 1.0)
        self.assertEqual(decisions["activationRate"], 0.3333)

    def test_shadow_only_decision_counts_as_an_abstention(self) -> None:
        report = build_measurement_report(
            [
                measurement_event(
                    "decision",
                    decisionStatus="shadow-match",
                    mode="inject",
                    injected=False,
                )
            ]
        )

        self.assertEqual(report["decisions"]["matched"], 0)
        self.assertEqual(report["decisions"]["noMatch"], 0)
        self.assertEqual(report["decisions"]["shadowOnly"], 1)
        self.assertEqual(report["decisions"]["shadowed"], 1)
        self.assertEqual(report["decisions"]["abstentionRate"], 1.0)

    def test_policy_feedback_is_accumulated_by_verdict_and_route(self) -> None:
        report = build_measurement_report(
            [
                measurement_event("policy-feedback", route="pdf", verdict="helpful"),
                measurement_event("policy-feedback", route="pdf", verdict="irrelevant"),
                measurement_event("policy-feedback", route="code", verdict="helpful"),
            ]
        )

        self.assertEqual(report["policyFeedback"]["total"], 3)
        self.assertEqual(report["policyFeedback"]["byVerdict"], {"helpful": 2, "irrelevant": 1})
        self.assertEqual(report["policyFeedback"]["byRoute"], {"code": 1, "pdf": 2})

    def test_completion_correlation_requires_same_session_and_turn(self) -> None:
        report = build_measurement_report(
            [
                measurement_event(
                    "decision",
                    sessionHash="session-a",
                    turnHash="turn-1",
                    mode="shadow",
                    decisionStatus="matched",
                    injected=False,
                ),
                measurement_event("completion", sessionHash="session-b", turnHash="turn-1"),
            ]
        )

        self.assertEqual(report["completions"]["decisionTurns"], 1)
        self.assertEqual(report["completions"]["uniqueTurns"], 1)
        self.assertEqual(report["completions"]["correlatedTurns"], 0)
        self.assertEqual(report["completions"]["completionRate"], 0.0)
        self.assertFalse(report["comparability"]["outcomeAggregateComparable"])

    def test_outcome_report_deduplicates_labels_and_excludes_conflicts(self) -> None:
        context = {"policyVersion": "policy-1", "configRevision": "config-1"}
        report = build_measurement_report(
            [
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-a",
                    replicate=1,
                    arm="native",
                    status="fail",
                    source="objective",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-a",
                    replicate=1,
                    arm="native",
                    status="fail",
                    source="human",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-a",
                    replicate=1,
                    arm="inject",
                    status="pass",
                    source="objective",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-b",
                    replicate=1,
                    arm="native",
                    status="fail",
                    source="objective",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-b",
                    replicate=1,
                    arm="native",
                    status="pass",
                    source="human",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-b",
                    replicate=1,
                    arm="inject",
                    status="pass",
                    source="objective",
                ),
                measurement_event(
                    "outcome",
                    **context,
                    caseHash="case-c",
                    replicate=1,
                    arm="unexpected",
                    status="pass",
                    source="objective",
                ),
            ]
        )

        self.assertEqual(report["outcomes"]["total"], 7)
        self.assertEqual(report["outcomes"]["usable"], 3)
        self.assertEqual(report["outcomes"]["duplicates"], 1)
        self.assertEqual(report["outcomes"]["conflicts"], 1)
        self.assertEqual(report["outcomes"]["conflictingEvents"], 2)
        self.assertEqual(report["outcomes"]["invalid"], 1)
        self.assertEqual(report["outcomes"]["byArm"]["native"]["failed"], 1)
        self.assertEqual(report["outcomes"]["byArm"]["inject"]["passed"], 2)
        self.assertEqual(report["pairedNativeInject"]["pairs"], 1)
        self.assertEqual(report["pairedNativeInject"]["rescues"], 1)
        self.assertIn("duplicate-outcomes", report["warnings"])
        self.assertIn("conflicting-outcomes", report["warnings"])
        self.assertIn("invalid-outcomes", report["warnings"])

    def test_report_marks_mixed_revisions_and_does_not_pair_across_them(self) -> None:
        report = build_measurement_report(
            [
                measurement_event(
                    "decision",
                    policyVersion="policy-1",
                    configRevision="config-a",
                    runtimeRevision="runtime-a",
                    mode="shadow",
                    decisionStatus="matched",
                    injected=False,
                ),
                measurement_event(
                    "decision",
                    policyVersion="policy-1",
                    configRevision="config-b",
                    runtimeRevision="runtime-b",
                    mode="inject",
                    decisionStatus="matched",
                    injected=True,
                ),
                measurement_event(
                    "outcome",
                    policyVersion="policy-1",
                    configRevision="config-a",
                    caseHash="case-a",
                    replicate=1,
                    arm="native",
                    status="fail",
                    source="objective",
                ),
                measurement_event(
                    "outcome",
                    policyVersion="policy-1",
                    configRevision="config-b",
                    caseHash="case-a",
                    replicate=1,
                    arm="inject",
                    status="pass",
                    source="objective",
                ),
            ]
        )

        self.assertTrue(report["comparability"]["mixedDecisionContexts"])
        self.assertTrue(report["comparability"]["mixedOutcomeContexts"])
        self.assertFalse(report["comparability"]["outcomeAggregateComparable"])
        self.assertEqual(len(report["comparability"]["decisionContexts"]), 2)
        self.assertEqual(len(report["comparability"]["outcomeContexts"]), 2)
        self.assertEqual(report["pairedNativeInject"]["pairs"], 0)
        self.assertIn("mixed-decision-contexts", report["warnings"])
        self.assertIn("mixed-outcome-contexts", report["warnings"])

    def test_report_ignores_unknown_event_schema(self) -> None:
        report = build_measurement_report(
            [
                measurement_event(
                    "decision",
                    mode="shadow",
                    decisionStatus="matched",
                    injected=False,
                ),
                {
                    "schema": "lazy-skill-router.measurement-event/v999",
                    "eventType": "decision",
                    "mode": "inject",
                    "decisionStatus": "matched",
                    "injected": True,
                },
            ]
        )

        self.assertEqual(report["observedEvents"], 2)
        self.assertEqual(report["events"], 1)
        self.assertEqual(report["ignoredEvents"], 1)
        self.assertEqual(report["decisions"]["total"], 1)
        self.assertFalse(report["comparability"]["outcomeAggregateComparable"])
        self.assertIn("ignored-events", report["warnings"])

    def test_unversioned_outcome_is_not_claimed_comparable(self) -> None:
        report = build_measurement_report(
            [
                measurement_event(
                    "outcome",
                    caseHash="case-a",
                    replicate=1,
                    arm="native",
                    status="pass",
                    source="objective",
                )
            ]
        )

        self.assertTrue(report["comparability"]["unversionedOutcomes"])
        self.assertFalse(report["comparability"]["outcomeAggregateComparable"])
        self.assertIn("unversioned-outcomes", report["warnings"])

    def test_turn_based_outcome_requires_session_and_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "measurement.jsonl"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "outcome",
                    "--log",
                    str(log_path),
                    "--turn-id",
                    "turn-only",
                    "--arm",
                    "native",
                    "--status",
                    "pass",
                    "--source",
                    "objective",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            valid = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "outcome",
                    "--log",
                    str(log_path),
                    "--session-id",
                    "session-a",
                    "--turn-id",
                    "turn-a",
                    "--arm",
                    "native",
                    "--status",
                    "pass",
                    "--source",
                    "objective",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            events = read_events(log_path)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("--case-id or both --session-id and --turn-id are required", completed.stderr)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIsInstance(events[0]["sessionHash"], str)
        self.assertIsInstance(events[0]["turnHash"], str)

    def test_off_mode_records_no_route_selection_or_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "measurement.jsonl"
            config_path = root / "routes.json"
            config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            config["activation"] = {"mode": "off"}
            config["logging"] = {"enabled": True, "path": str(log_path)}
            config_path.write_text(json.dumps(config), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(HOOK_PATH), "--config", str(config_path)],
                input=json.dumps({"turn_id": "off-turn", "prompt": "PDF 만들어줘"}),
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            event = read_events(log_path)[0]

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(event["mode"], "off")
        self.assertEqual(event["decisionStatus"], "off")
        self.assertFalse(event["shouldInject"])
        self.assertFalse(event["injected"])
        self.assertIsNone(event["route"])
        self.assertEqual(event["candidateRouteIds"], [])

    def test_shadow_and_stop_hooks_accumulate_pseudonymous_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "measurement.jsonl"
            config_path = root / "routes.json"
            config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            config["activation"] = {"mode": "shadow"}
            config["logging"] = {
                "enabled": True,
                "path": str(log_path),
                "maxEntries": 100,
                "retentionDays": 30,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            prompt = "private PDF request"
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "raw-session-id",
                "turn_id": "raw-turn-id",
                "prompt": prompt,
            }

            decision = subprocess.run(
                [sys.executable, str(HOOK_PATH), "--config", str(config_path)],
                input=json.dumps(event),
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            completion = subprocess.run(
                [sys.executable, str(HOOK_PATH), "--hook-event", "stop", "--config", str(config_path)],
                input=json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "raw-session-id",
                        "turn_id": "raw-turn-id",
                        "stop_hook_active": False,
                        "last_assistant_message": "private assistant response",
                    }
                ),
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            events = read_events(log_path)
            serialized = json.dumps(events, ensure_ascii=False)

        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(decision.stdout, "")
        self.assertEqual(completion.returncode, 0, completion.stderr)
        self.assertEqual(json.loads(completion.stdout), {})
        self.assertEqual([event["eventType"] for event in events], ["decision", "completion"])
        self.assertEqual(events[0]["schema"], "lazy-skill-router.measurement-event/v1")
        self.assertEqual(events[0]["mode"], "shadow")
        self.assertEqual(events[0]["decisionStatus"], "matched")
        self.assertEqual(events[0]["activationDisposition"], "propose")
        self.assertEqual(events[0]["activationReason"], "weak_evidence")
        self.assertFalse(events[0]["shouldActivate"])
        self.assertFalse(events[0]["injected"])
        self.assertEqual(events[0]["route"], "pdf")
        self.assertEqual(events[0]["turnHash"], events[1]["turnHash"])
        self.assertNotIn(prompt, serialized)
        self.assertNotIn("raw-session-id", serialized)
        self.assertNotIn("raw-turn-id", serialized)
        self.assertNotIn("private assistant response", serialized)

    def test_outcome_cli_accumulates_native_inject_pair_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "measurement.jsonl"
            commands = (
                (
                    "outcome",
                    "--config",
                    str(DEFAULT_CONFIG),
                    "--log",
                    str(log_path),
                    "--case-id",
                    "private-case-name",
                    "--replicate",
                    "1",
                    "--arm",
                    "native",
                    "--status",
                    "fail",
                    "--source",
                    "objective",
                ),
                (
                    "outcome",
                    "--config",
                    str(DEFAULT_CONFIG),
                    "--log",
                    str(log_path),
                    "--case-id",
                    "private-case-name",
                    "--replicate",
                    "1",
                    "--arm",
                    "inject",
                    "--status",
                    "pass",
                    "--source",
                    "objective",
                ),
            )
            for command in commands:
                completed = subprocess.run(
                    [sys.executable, "-m", CLI_MODULE, *command],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            report = subprocess.run(
                [sys.executable, "-m", CLI_MODULE, "report", "--log", str(log_path), "--json"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            text_report = subprocess.run(
                [sys.executable, "-m", CLI_MODULE, "report", "--log", str(log_path)],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            payload = json.loads(report.stdout)
            events = read_events(log_path)
            serialized = log_path.read_text(encoding="utf-8")

        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(payload["schema"], "lazy-skill-router.measurement-report/v1")
        self.assertEqual(payload["outcomes"]["byArm"]["native"]["failed"], 1)
        self.assertEqual(payload["outcomes"]["byArm"]["inject"]["passed"], 1)
        self.assertEqual(payload["pairedNativeInject"]["pairs"], 1)
        self.assertEqual(payload["pairedNativeInject"]["rescues"], 1)
        self.assertEqual(payload["pairedNativeInject"]["harms"], 0)
        self.assertEqual(payload["pairedNativeInject"]["netWin"], 1)
        self.assertTrue(payload["comparability"]["outcomeAggregateComparable"])
        self.assertFalse(payload["comparability"]["unversionedOutcomes"])
        self.assertEqual({event["policyVersion"] for event in events}, {"2026-07-11.1"})
        self.assertEqual(len({event["configRevision"] for event in events}), 1)
        self.assertEqual(text_report.returncode, 0, text_report.stderr)
        self.assertIn("Decision latency ms:", text_report.stdout)
        self.assertIn("- inject: pass 1, fail 0, unknown 0 (success rate 1.0)", text_report.stdout)
        self.assertIn("- native: pass 0, fail 1, unknown 0 (success rate 0.0)", text_report.stdout)
        self.assertNotIn("private-case-name", serialized)

    def test_validator_rejects_unknown_activation_mode(self) -> None:
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        config["activation"] = {"mode": "randomly-inject"}

        findings = validate_config(config)

        self.assertIn("activation.mode must be one of: inject, off, shadow", [item.message for item in findings])

    def test_install_flags_enable_automatic_shadow_measurement_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "install",
                    "--codex-home",
                    str(codex_home),
                    "--agents-home",
                    str(agents_home),
                    "--enable-measurement",
                    "--activation-mode",
                    "shadow",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            route_path = codex_home / "lazy-skill-router" / "routes.json"
            config = json.loads(route_path.read_text(encoding="utf-8"))
            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            prompt_command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
            stop_command = hooks["Stop"][0]["hooks"][0]["command"]

            decision = subprocess.run(
                shlex.split(prompt_command),
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "installed-session",
                        "turn_id": "installed-turn",
                        "prompt": "PDF 만들어줘",
                    }
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            completion = subprocess.run(
                shlex.split(stop_command),
                input=json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "installed-session",
                        "turn_id": "installed-turn",
                        "stop_hook_active": False,
                    }
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            report = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "report",
                    "--config",
                    str(route_path),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            report_payload = json.loads(report.stdout)
            doctor = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    CLI_MODULE,
                    "doctor",
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
            uninstall = subprocess.run(
                [sys.executable, "-m", CLI_MODULE, "uninstall", "--codex-home", str(codex_home)],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

        self.assertEqual(config["activation"]["mode"], "shadow")
        self.assertTrue(config["logging"]["enabled"])
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(decision.stdout, "")
        self.assertEqual(completion.returncode, 0, completion.stderr)
        self.assertEqual(json.loads(completion.stdout), {})
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(report_payload["decisions"]["shadowed"], 1)
        self.assertEqual(report_payload["decisions"]["injectionRate"], 0.0)
        self.assertEqual(report_payload["decisions"]["latencyMs"]["count"], 1)
        self.assertEqual(report_payload["completions"]["correlatedTurns"], 1)
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn("[OK] Stop hook registered", doctor.stdout)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("remove 2 hook entry", uninstall.stdout)

    def test_disabling_measurement_removes_stop_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            agents_home = root / "agents"
            common = [
                sys.executable,
                "-m",
                CLI_MODULE,
                "install",
                "--codex-home",
                str(codex_home),
                "--agents-home",
                str(agents_home),
            ]
            enabled = subprocess.run(
                [*common, "--enable-measurement", "--activation-mode", "shadow"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            disabled = subprocess.run(
                [*common, "--disable-measurement"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            routes = json.loads((codex_home / "lazy-skill-router" / "routes.json").read_text(encoding="utf-8"))

        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertFalse(routes["logging"]["enabled"])
        self.assertEqual(hooks.get("Stop"), [{"hooks": []}])
        self.assertIn("removed Stop hook entry", disabled.stdout)


if __name__ == "__main__":
    unittest.main()
