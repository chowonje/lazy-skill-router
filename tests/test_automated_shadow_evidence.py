from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazy_skill_router import automated_reference_measurement, deterministic_explicit_skill_references
from lazy_skill_router_inventory import InventorySnapshot
from lazy_skill_router_logging import (
    AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA,
    MEASUREMENT_EVENT_SCHEMA,
    automated_objective_signal_v1,
    log_decision,
    routing_observation_v1,
)
from measurement import build_automated_shadow_evidence

ROOT = Path(__file__).resolve().parents[1]
CLI_MODULE = "lazy_skill_router_cli.cli"


def revision(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def inventory() -> InventorySnapshot:
    skills = (
        {
            "configured_name": "ponytail",
            "canonical_id": "local/ponytail",
            "availability": {"status": "available"},
        },
        {
            "configured_name": "github:github",
            "canonical_id": "plugin/github",
            "availability": {"status": "available"},
        },
        {
            "configured_name": "code-review",
            "canonical_id": "local/code-review",
            "availability": {"status": "available"},
        },
        {
            "configured_name": "foo.bar",
            "canonical_id": "local/foo.bar",
            "availability": {"status": "available"},
        },
    )
    return InventorySnapshot("available", "sha256:inventory", skills)


def shadow_event(
    offset: int,
    *,
    expected: tuple[str, ...] = ("ponytail",),
    candidates: tuple[str, ...] = ("ponytail", "code-review"),
    signal: dict[str, object] | None = None,
    retrieval_revision: str = revision("index"),
    retrieval_algorithm: str = "lexical-bm25-char3/v1",
    retrieval_implementation_revision: str = revision("retrieval-implementation"),
    config_revision: str = revision("config"),
) -> dict[str, object]:
    candidate_observations = tuple({"skillId": name, "evidenceIds": ()} for name in candidates)
    observation = routing_observation_v1(
        retrieval_status="matched",
        retrieval_revision=retrieval_revision,
        candidate_observations=candidate_observations,
        retrieval_latency_ms=1.0,
        retrieval_reason_codes=(),
        legacy_primary="code-review",
        activation_disposition="propose",
        injected=False,
        legacy_selection_observed=True,
    )
    return {
        "schema": MEASUREMENT_EVENT_SCHEMA,
        "eventType": "decision",
        "promptHash": f"{offset:016x}"[-16:],
        "policyVersion": "route-v1:1",
        "configRevision": config_revision,
        "catalogRevision": revision("catalog"),
        "runtimeRevision": revision("runtime"),
        "retrievalStatus": "matched",
        "retrievalAlgorithm": retrieval_algorithm,
        "retrievalImplementationRevision": retrieval_implementation_revision,
        "routingObservation": observation,
        "automatedObjectiveSignal": signal or automated_objective_signal_v1(expected),
    }


class AutomatedShadowEvidenceTest(unittest.TestCase):
    def test_explicit_reference_parser_is_narrow_and_inventory_bound(self) -> None:
        snapshot = inventory()

        self.assertEqual(deterministic_explicit_skill_references("Use $ponytail now", snapshot), ("ponytail",))
        self.assertEqual(
            deterministic_explicit_skill_references("github:github 스킬을 사용해줘", snapshot),
            ("github:github",),
        )
        self.assertEqual(
            deterministic_explicit_skill_references("Use skill `code-review` please", snapshot),
            ("code-review",),
        )
        self.assertEqual(deterministic_explicit_skill_references("Use a ponytail approach", snapshot), ())
        self.assertEqual(deterministic_explicit_skill_references("Use $missing-skill", snapshot), ())
        self.assertEqual(
            deterministic_explicit_skill_references("Do not use $ponytail; use $code-review", snapshot),
            ("code-review",),
        )
        self.assertEqual(deterministic_explicit_skill_references("Without ponytail skill", snapshot), ())
        self.assertEqual(deterministic_explicit_skill_references("Is the $ponytail skill installed?", snapshot), ())
        self.assertEqual(deterministic_explicit_skill_references("Should I use $ponytail?", snapshot), ())
        self.assertEqual(deterministic_explicit_skill_references("How do I use $ponytail?", snapshot), ())
        self.assertEqual(deterministic_explicit_skill_references("ponytail 스킬을 써도 돼?", snapshot), ())
        self.assertEqual(
            deterministic_explicit_skill_references("$ponytail is broken, use $code-review instead", snapshot),
            ("code-review",),
        )
        self.assertEqual(
            deterministic_explicit_skill_references("Use $ponytail, $code-review", snapshot),
            ("ponytail", "code-review"),
        )
        self.assertEqual(
            deterministic_explicit_skill_references("Use $ponytail; then $code-review", snapshot),
            ("ponytail", "code-review"),
        )
        self.assertEqual(
            deterministic_explicit_skill_references("Use $ponytail, do not use $code-review", snapshot),
            ("ponytail",),
        )
        self.assertEqual(deterministic_explicit_skill_references("Use $ponytail.", snapshot), ("ponytail",))
        self.assertEqual(
            deterministic_explicit_skill_references("Use skill code-review.", snapshot),
            ("code-review",),
        )
        self.assertEqual(deterministic_explicit_skill_references("Use $foo.bar now.", snapshot), ("foo.bar",))
        self.assertEqual(deterministic_explicit_skill_references("Use skill foo.bar now.", snapshot), ("foo.bar",))
        self.assertEqual(
            deterministic_explicit_skill_references("Use $ponytail " * 5_000, snapshot),
            ("ponytail",),
        )

    def test_logging_adds_redacted_objective_signal_without_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            prompt = "PRIVATE use $ponytail"
            log_decision(
                prompt,
                None,
                {"logging": {"enabled": True, "path": str(path)}},
                capability_index_revision="sha256:index",
                capability_candidate_skill_ids=("ponytail",),
                capability_candidate_observations=({"skillId": "ponytail", "evidenceIds": ()},),
                capability_retrieval_latency_ms=1.0,
                capability_retrieval_status="matched",
                automated_expected_skill_ids=("ponytail",),
            )
            record = json.loads(path.read_text(encoding="utf-8"))

        signal = record["automatedObjectiveSignal"]
        self.assertEqual(signal["schema"], AUTOMATED_OBJECTIVE_SIGNAL_SCHEMA)
        self.assertEqual(signal["kind"], "explicit-skill-reference")
        self.assertEqual(signal["expectedSkillIds"], ["ponytail"])
        self.assertEqual(signal["parserRevision"], "deterministic-explicit-skill-reference/v2")
        self.assertFalse(signal["rawPromptStored"])
        self.assertNotIn(prompt, json.dumps(record, ensure_ascii=False))
        self.assertFalse(record["routingObservation"]["semantics"]["automaticPromotion"])

    def test_parser_cost_is_only_added_for_usable_shadow_results(self) -> None:
        snapshot = inventory()
        expected, latency = automated_reference_measurement("Use $ponytail", snapshot, None, None)
        self.assertEqual(expected, ())
        self.assertIsNone(latency)

        expected, latency = automated_reference_measurement(
            "Use $ponytail",
            snapshot,
            {"status": "matched"},
            2.0,
        )
        self.assertEqual(expected, ("ponytail",))
        self.assertGreaterEqual(latency or 0, 2.0)

    def test_collection_gate_can_be_ready_but_promotion_stays_blocked(self) -> None:
        evidence = build_automated_shadow_evidence([shadow_event(offset) for offset in range(100)])

        self.assertEqual(evidence["collectionStatus"], "ready-for-automated-shadow-review")
        self.assertEqual(evidence["promotionStatus"], "blocked")
        self.assertEqual(evidence["authority"], "none")
        self.assertFalse(evidence["autoPromote"])
        self.assertFalse(evidence["provesIndependence"])
        self.assertFalse(evidence["provesQuality"])
        self.assertEqual(evidence["observed"]["uniqueExplicitReferenceCases"], 100)
        self.assertEqual(evidence["observed"]["explicitReferenceRecallAt3"], 1.0)
        self.assertEqual(evidence["observed"]["explicitReferenceTop1Accuracy"], 1.0)
        self.assertEqual(evidence["observed"]["retrievalRevisions"], [revision("index")])
        self.assertEqual(
            evidence["observed"]["retrievalContexts"],
            [
                {
                    "algorithm": "lexical-bm25-char3/v1",
                    "implementationRevision": revision("retrieval-implementation"),
                    "indexRevision": revision("index"),
                }
            ],
        )
        self.assertEqual(evidence["observed"]["decisionContextCount"], 1)
        self.assertEqual(evidence["collectionBlockers"], [])
        self.assertIn("explicit_reference_scope_only", evidence["promotionBlockers"])
        self.assertNotIn("promptHash", json.dumps(evidence))

    def test_no_data_is_stable_and_missed_reference_fails_collection(self) -> None:
        empty = build_automated_shadow_evidence([])
        legacy_only = build_automated_shadow_evidence(
            [{"schema": MEASUREMENT_EVENT_SCHEMA, "eventType": "decision", "promptHash": "0" * 16}]
        )
        missed = build_automated_shadow_evidence(
            [shadow_event(offset, candidates=("code-review",)) for offset in range(100)]
        )

        self.assertEqual(empty, legacy_only)
        self.assertEqual(empty["collectionStatus"], "no-data")
        self.assertIn("no_current_routing_observations", empty["collectionBlockers"])
        self.assertEqual(missed["collectionStatus"], "blocked")
        self.assertEqual(missed["observed"]["explicitReferenceRecallAt3"], 0.0)
        self.assertIn("explicit_reference_recall_below_minimum", missed["collectionBlockers"])

        invalid_only = build_automated_shadow_evidence(
            [
                {
                    "schema": MEASUREMENT_EVENT_SCHEMA,
                    "eventType": "decision",
                    "retrievalStatus": "matched",
                    "routingObservation": {"schema": "lazy-skill-router.routing-observation/v1"},
                }
            ]
        )
        self.assertEqual(invalid_only["collectionStatus"], "blocked")
        self.assertIn("invalid_routing_observations", invalid_only["collectionBlockers"])

    def test_revision_and_context_mixing_fail_closed_and_change_artifact_revision(self) -> None:
        index_a = build_automated_shadow_evidence([shadow_event(offset) for offset in range(100)])
        index_b = build_automated_shadow_evidence(
            [shadow_event(offset, retrieval_revision=revision("index-b")) for offset in range(100)]
        )
        mixed_index = build_automated_shadow_evidence(
            [
                shadow_event(offset, retrieval_revision=revision("index-a" if offset < 50 else "index-b"))
                for offset in range(100)
            ]
        )
        mixed_context = build_automated_shadow_evidence(
            [
                shadow_event(offset, config_revision=revision("config-a" if offset < 50 else "config-b"))
                for offset in range(100)
            ]
        )
        mixed_retrieval_context = build_automated_shadow_evidence(
            [
                shadow_event(
                    offset,
                    retrieval_algorithm=("lexical-bm25-char3/v1" if offset < 50 else "lexical-bm25-char3-anchored/v2"),
                )
                for offset in range(100)
            ]
        )

        self.assertNotEqual(index_a["revision"], index_b["revision"])
        self.assertEqual(mixed_index["collectionStatus"], "blocked")
        self.assertIn("mixed_retrieval_revisions", mixed_index["collectionBlockers"])
        self.assertEqual(mixed_context["collectionStatus"], "blocked")
        self.assertIn("mixed_decision_contexts", mixed_context["collectionBlockers"])
        self.assertEqual(mixed_retrieval_context["collectionStatus"], "blocked")
        self.assertIn("mixed_retrieval_contexts", mixed_retrieval_context["collectionBlockers"])

    def test_missing_retrieval_implementation_context_fails_collection(self) -> None:
        event = shadow_event(1)
        event.pop("retrievalImplementationRevision")
        invalid_algorithm = shadow_event(2)
        invalid_algorithm["retrievalAlgorithm"] = []

        evidence = build_automated_shadow_evidence([event])
        invalid_algorithm_evidence = build_automated_shadow_evidence([invalid_algorithm])

        self.assertEqual(evidence["collectionStatus"], "blocked")
        self.assertIn("retrieval_context_missing_or_invalid", evidence["collectionBlockers"])
        self.assertEqual(evidence["observed"]["invalidRetrievalContexts"], 1)
        self.assertEqual(invalid_algorithm_evidence["collectionStatus"], "blocked")
        self.assertIn(
            "retrieval_context_missing_or_invalid",
            invalid_algorithm_evidence["collectionBlockers"],
        )

    def test_conflicts_are_not_double_counted_as_duplicates(self) -> None:
        first = shadow_event(1, candidates=("ponytail",))
        conflicting = shadow_event(1, candidates=("code-review",))
        evidence = build_automated_shadow_evidence([first, conflicting])

        self.assertEqual(evidence["observed"]["conflictingExplicitReferenceCases"], 1)
        self.assertEqual(evidence["observed"]["duplicateExplicitReferenceCases"], 0)

    def test_context_values_are_validated_and_never_emitted_raw(self) -> None:
        event = shadow_event(1)
        event["policyVersion"] = "PRIVATE PROMPT /Users/alice/secret"
        event["runtimeRevision"] = "C:/Users/alice/private"
        evidence = build_automated_shadow_evidence([event])
        encoded = json.dumps(evidence)

        self.assertEqual(evidence["collectionStatus"], "blocked")
        self.assertIn("decision_context_missing_or_invalid", evidence["collectionBlockers"])
        self.assertNotIn("PRIVATE PROMPT", encoded)
        self.assertNotIn("Users/alice", encoded)

    def test_cli_emits_path_redacted_no_data_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "private-events.jsonl"
            log_path.write_text("", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", CLI_MODULE, "shadow-evidence", "--log", str(log_path), "--json"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["collectionStatus"], "no-data")
        self.assertEqual(payload["promotionStatus"], "blocked")
        self.assertNotIn("private-events", completed.stdout)


if __name__ == "__main__":
    unittest.main()
