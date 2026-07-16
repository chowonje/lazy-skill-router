from __future__ import annotations

import copy
import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import eval_router_ab
import materialize_router_ab_manifest
from lazy_skill_router_capability_index import (
    CAPABILITY_INDEX_SCHEMA_V1,
    CAPABILITY_INDEX_SCHEMA_V2,
    build_capability_index,
)
from lazy_skill_router_inventory import INVENTORY_SCHEMA, inventory_revision
from release_checksums import digest_file


def skill(name: str, description: str) -> dict[str, object]:
    return {
        "configured_name": name,
        "canonical_id": f"test/skills/{name}",
        "description": description,
        "aliases": [],
        "capabilities": [],
        "phases": [],
        "availability": {"status": "available"},
    }


def write_experiment_inputs(root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    skills = [
        skill("legacy-reviewer", "General legacy workflow for broad review tasks."),
        skill("dedicated-reviewer", "Review pull request regressions and correctness risks."),
        skill("security-threat-model", "Build trust boundaries, assets, abuse paths, and a threat model."),
    ]
    revision = inventory_revision(skills)
    inventory_payload = {
        "schema": INVENTORY_SCHEMA,
        "revision": revision,
        "generated_at": "2026-07-13T00:00:00Z",
        "skills": skills,
    }
    inventory_path = root / "skills.manifest.json"
    inventory_path.write_text(json.dumps(inventory_payload), encoding="utf-8")
    inventory = eval_router_ab.load_inventory_manifest(inventory_path)

    index_payload = build_capability_index(
        inventory,
        generated_at="2026-07-13T00:00:00Z",
        schema=CAPABILITY_INDEX_SCHEMA_V1,
    )
    index_path = root / "capability-index.json"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")

    config: dict[str, object] = {
        "version": 1,
        "minConfidence": 0.5,
        "allowedSkills": ["legacy-reviewer", "security-threat-model"],
        "activation": {"mode": "inject", "autoActivateMinStrength": 0.8},
        "routes": [
            {
                "name": "legacy-review",
                "primary": "legacy-reviewer",
                "patterns": ["review"],
                "reason": "Legacy broad review route.",
            },
            {
                "name": "threat-model",
                "primary": "security-threat-model",
                "patterns": ["threat model"],
                "reason": "Legacy threat-model route.",
            },
        ],
    }
    config_path = root / "routes.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    frozen = {
        "configRevision": eval_router_ab.config_revision(config),
        "inventoryRevision": revision,
        "indexRevision": index_payload["revision"],
        "retrievalAlgorithm": eval_router_ab.RETRIEVAL_ALGORITHM,
        "experimentCodeRevision": eval_router_ab.experiment_code_revision(),
        "maxCandidates": 3,
    }
    return config_path, inventory_path, index_path, frozen


def manifest(frozen: dict[str, object], prompt: str) -> dict[str, object]:
    return {
        "schema": eval_router_ab.EXPERIMENT_MANIFEST_SCHEMA,
        "frozen": frozen,
        "cases": [
            {
                "id": "dedicated-rescue",
                "category": "review-boundary",
                "language": "en",
                "risk": "medium",
                "prompt": prompt,
                "gold": {
                    "primary": "dedicated-reviewer",
                    "acceptableCandidates": ["dedicated-reviewer"],
                    "expectedAbstain": False,
                    "forbiddenCandidates": ["security-threat-model"],
                },
            }
        ],
    }


def perfect_gate_report() -> dict[str, object]:
    return {
        "reportRevision": "sha256:report",
        "runRevision": "sha256:run",
        "protocol": {"rawPromptsEmitted": False},
        "a": {"abstention": {"recall": 1.0}},
        "b": {
            "recallAt3": {"mean": 0.95},
            "candidateTop1Accuracy": {"rate": 0.90},
            "mrr": {"mean": 0.75},
            "precisionAt3": {"mean": 0.30},
            "abstention": {"recall": 1.0},
            "expectedAbstainLexicalNoMatch": {"recall": 0.95},
            "labelledCandidateConflicts": {
                "top1Hits": 0,
                "topKHits": 0,
                "topKAffectedCases": 0,
                "highRiskTopKHits": 0,
                "highRiskTopKAffectedCases": 0,
            },
            "inventoryIneligibleCandidates": {"hits": 0},
            "operationalFailures": 0,
            "latency": {"p95Ms": 20.0},
        },
        "candidateOnlyPairedStatistics": {"pairedNormalApprox95Ci": [0.000001, 0.2]},
        "candidateRecallAt3PairedStatistics": {"pairedNormalApprox95Ci": [0.000001, 0.2]},
        "cases": [{"id": "holdout-1"}],
    }


def write_evidence_artifacts(root: Path) -> eval_router_ab.ExperimentEvidence:
    revisions: list[str] = []
    artifact_paths: list[tuple[str, str]] = []
    for offset, (evidence_type, _) in enumerate(eval_router_ab.EVIDENCE_ARTIFACT_TYPES):
        name = "PRIVATE_ARTIFACT_PATH.json" if offset == 0 else f"artifact-{offset}.json"
        path = root / name
        path.write_text(json.dumps({"type": evidence_type, "sequence": offset}), encoding="utf-8")
        revisions.append("sha256:" + digest_file(path))
        artifact_paths.append((evidence_type, name))
    return eval_router_ab.ExperimentEvidence(
        "independent-holdout",
        "independent-catalog",
        *revisions,
        tuple(artifact_paths),
    )


class StepClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        current = self.value
        self.value += 1_000_000
        return current


class RouterABEvalTest(unittest.TestCase):
    def test_missing_frozen_index_schema_replays_v1(self) -> None:
        parsed = eval_router_ab.parse_manifest(
            manifest(
                {
                    "configRevision": "cfg",
                    "inventoryRevision": "inv",
                    "indexRevision": "idx",
                    "retrievalAlgorithm": "lexical-bm25-char3/v1",
                    "experimentCodeRevision": "code",
                    "maxCandidates": 3,
                },
                "Private replay prompt",
            )
        )

        self.assertEqual(parsed.frozen.index_schema, CAPABILITY_INDEX_SCHEMA_V1)
        self.assertEqual(eval_router_ab.frozen_payload(parsed.frozen)["indexSchema"], CAPABILITY_INDEX_SCHEMA_V1)

    def test_manifest_rejects_unknown_risk_before_safety_slicing(self) -> None:
        raw = manifest(
            {
                "configRevision": "cfg",
                "inventoryRevision": "inv",
                "indexRevision": "idx",
                "retrievalAlgorithm": "lexical-bm25-char3/v1",
                "experimentCodeRevision": "code",
                "maxCandidates": 3,
            },
            "Private risk prompt",
        )
        raw["cases"][0]["risk"] = "critical"  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "risk.*unsupported"):
            eval_router_ab.parse_manifest(raw)

    def test_high_risk_rank_two_forbidden_candidate_blocks_gate(self) -> None:
        case = eval_router_ab.ABCase(
            "high-risk-rank-two",
            "security",
            "en",
            "high",
            "Private high-risk prompt",
            eval_router_ab.GoldLabel("safe", ("safe",), False, ("forbidden",)),
        )
        evaluations = (
            eval_router_ab.CaseEvaluation(
                case,
                eval_router_ab.SystemOutcome(("safe",), False, "activate"),
                eval_router_ab.SystemOutcome(("safe", "forbidden"), False, "matched"),
            ),
        )
        inventory = eval_router_ab.InventorySnapshot(
            "available",
            "inv",
            (skill("safe", "Safe skill"), skill("forbidden", "Forbidden skill")),
        )

        summary = eval_router_ab.system_summary(evaluations, "retrieval", inventory)

        self.assertEqual(summary["labelledCandidateConflicts"]["top1Hits"], 0)
        self.assertEqual(summary["labelledCandidateConflicts"]["highRiskTopKHits"], 1)

    def test_promotion_gate_enforces_full_candidate_safety_policy(self) -> None:
        case = eval_router_ab.ABCase(
            "holdout-1",
            "review",
            "en",
            "high",
            "Independent holdout prompt",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ("forbidden",)),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            experiment = eval_router_ab.ExperimentManifest(
                eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
                (case,),
                "sha256:manifest",
                write_evidence_artifacts(root),
            )
            report = perfect_gate_report()
            report["b"].update(  # type: ignore[union-attr]
                {
                    "candidateTop1Accuracy": {"rate": 0.899999},
                    "mrr": {"mean": 0.749999},
                    "precisionAt3": {"mean": 0.299999},
                    "expectedAbstainLexicalNoMatch": {"recall": 0.949999},
                    "labelledCandidateConflicts": {
                        "top1Hits": 1,
                        "topKHits": 2,
                        "topKAffectedCases": 1,
                        "highRiskTopKHits": 1,
                        "highRiskTopKAffectedCases": 1,
                    },
                }
            )
            report["candidateRecallAt3PairedStatistics"] = {"pairedNormalApprox95Ci": [0.0, 0.2]}

            gate = eval_router_ab.promotion_gate_v1(report, experiment, artifact_root=root)

        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(
            {
                "candidate_top1_below_minimum",
                "candidate_mrr_below_minimum",
                "candidate_precision_at_3_below_minimum",
                "expected_abstain_lexical_no_match_below_minimum",
                "forbidden_top1_candidate",
                "high_risk_forbidden_top3_candidate",
                "candidate_recall_at_3_ci_nonpositive",
            }
            - set(gate["blockers"]),
            set(),
        )
        self.assertNotIn("expected_abstain_no_match_regressed", gate["blockers"])
        self.assertFalse(gate["autoPromote"])
        self.assertEqual(gate["authority"], "none")

    def test_lexical_no_match_is_not_semantic_abstention(self) -> None:
        case = eval_router_ab.ABCase(
            "no-match",
            "none",
            "en",
            "low",
            "Private no-match prompt",
            eval_router_ab.GoldLabel(None, (), True, ()),
        )
        evaluations = (
            eval_router_ab.CaseEvaluation(
                case,
                eval_router_ab.SystemOutcome((), True, "abstain"),
                eval_router_ab.SystemOutcome((), False, "no-match"),
            ),
        )
        inventory = eval_router_ab.InventorySnapshot("available", "inv", ())

        legacy = eval_router_ab.system_summary(evaluations, "legacy", inventory)
        retrieval = eval_router_ab.system_summary(evaluations, "retrieval", inventory)

        self.assertEqual(legacy["abstention"]["recall"], 1.0)
        self.assertEqual(legacy["expectedAbstainLexicalNoMatch"]["recall"], 0.0)
        self.assertEqual(retrieval["abstention"]["recall"], 0.0)
        self.assertEqual(retrieval["expectedAbstainLexicalNoMatch"]["recall"], 1.0)

    def test_paired_recall_ci_measures_recall_uplift_not_top1_uplift(self) -> None:
        cases = tuple(
            eval_router_ab.ABCase(
                f"recall-{offset}",
                "review",
                "en",
                "low",
                f"Private recall prompt {offset}",
                eval_router_ab.GoldLabel("primary", ("primary", "alternate"), False, ()),
            )
            for offset in range(2)
        )
        evaluations = tuple(
            eval_router_ab.CaseEvaluation(
                case,
                eval_router_ab.SystemOutcome(("primary",), False, "activate"),
                eval_router_ab.SystemOutcome(("primary", "alternate"), False, "matched"),
            )
            for case in cases
        )

        statistics = eval_router_ab.paired_recall_at_3_statistics(evaluations)

        self.assertEqual(statistics["pairs"], 2)
        self.assertEqual(statistics["meanUplift"], 0.5)
        self.assertEqual(statistics["pairedNormalApprox95Ci"], [0.5, 0.5])

    def test_experiment_code_revision_covers_transitive_local_runtime_imports(self) -> None:
        self.assertTrue(
            {"lazy_skill_router_common.py", "lazy_skill_router_logging.py"} <= set(eval_router_ab.EXPERIMENT_CODE_FILES)
        )

    def test_pilot_manifest_overlay_is_revision_bound_and_exact(self) -> None:
        base = manifest(
            {
                "configRevision": "cfg",
                "inventoryRevision": "inv",
                "indexRevision": "idx",
                "retrievalAlgorithm": eval_router_ab.RETRIEVAL_ALGORITHM,
                "experimentCodeRevision": "code",
                "maxCandidates": 3,
            },
            "Review pull request regressions",
        )
        base["evidence"] = {
            "schema": eval_router_ab.EXPERIMENT_EVIDENCE_SCHEMA,
            "corpusProvenance": "synthetic-calibration",
            "metadataProvenance": "independent-catalog",
            "independentHoldoutRevision": None,
            "independentAdjudicationRevision": None,
            "ownershipObservationRevision": None,
            "activationObservationRevision": None,
            "outcomeObservationRevision": None,
        }
        overlay = {
            "schema": materialize_router_ab_manifest.OVERLAY_SCHEMA,
            "baseManifestRevision": eval_router_ab.canonical_revision(base),
            "patch": {
                "frozen": {
                    "inventoryRevision": "sha256:" + "1" * 64,
                    "indexRevision": "sha256:" + "2" * 64,
                    "indexSchema": CAPABILITY_INDEX_SCHEMA_V2,
                    "retrievalAlgorithm": "lexical-bm25-char3-anchored/v2",
                    "experimentCodeRevision": "sha256:" + "3" * 64,
                },
                "evidence": {"metadataProvenance": "corpus-informed-calibration"},
            },
        }

        materialized, revision = materialize_router_ab_manifest.materialize_manifest(base, overlay)

        self.assertEqual(materialized["frozen"]["inventoryRevision"], "sha256:" + "1" * 64)
        self.assertEqual(materialized["frozen"]["indexRevision"], "sha256:" + "2" * 64)
        self.assertEqual(materialized["frozen"]["indexSchema"], CAPABILITY_INDEX_SCHEMA_V2)
        self.assertEqual(materialized["frozen"]["retrievalAlgorithm"], "lexical-bm25-char3-anchored/v2")
        self.assertEqual(materialized["frozen"]["experimentCodeRevision"], "sha256:" + "3" * 64)
        self.assertEqual(materialized["evidence"]["metadataProvenance"], "corpus-informed-calibration")
        self.assertEqual(revision, eval_router_ab.canonical_revision(materialized))
        self.assertEqual(base["evidence"]["metadataProvenance"], "independent-catalog")

        overlay["baseManifestRevision"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            materialize_router_ab_manifest.materialize_manifest(base, overlay)

    def test_real_paired_evaluation_finds_rescue_and_redacts_prompt(self) -> None:
        sentinel = "PRIVATE_PROMPT_SENTINEL"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path, inventory_path, index_path, frozen = write_experiment_inputs(root)
            parsed = eval_router_ab.parse_manifest(
                manifest(frozen, f"Review pull request regressions and correctness risks. {sentinel}")
            )
            inputs = eval_router_ab.verify_inputs(
                eval_router_ab.load_config(config_path), inventory_path, index_path, parsed.frozen
            )
            evaluations = eval_router_ab.evaluate_cases(parsed.cases, inputs, clock_ns=StepClock())
            report = eval_router_ab.report_payload(parsed, inputs, evaluations)

        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["a"]["top1Accuracy"]["correct"], 0)
        self.assertEqual(report["b"]["top1Accuracy"]["correct"], 1)
        self.assertEqual(report["b"]["candidateTop1Accuracy"], {"correct": 1, "total": 1, "rate": 1.0})
        self.assertEqual(report["comparison"], {"rescue": 1, "harm": 0, "netWin": 1, "bothCorrect": 0, "bothWrong": 0})
        self.assertEqual(report["b"]["recallAt3"]["mean"], 1.0)
        self.assertEqual(report["a"]["latency"], {"p50Ms": 1.0, "p95Ms": 1.0, "p99Ms": 1.0})
        self.assertFalse(report["protocol"]["rawPromptsEmitted"])
        self.assertTrue(report["protocol"]["variantBProducesCandidatesNotFinalOwnership"])
        self.assertTrue(report["protocol"]["retrievalNoMatchIsNotSemanticAbstention"])
        self.assertEqual(report["promotionGate"]["status"], "blocked")
        self.assertFalse(report["promotionGate"]["autoPromote"])
        self.assertIn("ownership_observation_missing", report["promotionGate"]["blockers"])
        self.assertNotIn(sentinel, encoded)
        self.assertNotIn("Review pull request regressions", encoded)

    def test_v2_frozen_algorithm_is_replayed_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path, inventory_path, index_path, frozen = write_experiment_inputs(root)
            frozen["retrievalAlgorithm"] = "lexical-bm25-char3-anchored/v2"
            parsed = eval_router_ab.parse_manifest(manifest(frozen, "Review pull request regressions"))
            with mock.patch(
                "eval_router_ab.load_capability_index",
                wraps=eval_router_ab.load_capability_index,
            ) as index_loader:
                inputs = eval_router_ab.verify_inputs(
                    eval_router_ab.load_config(config_path), inventory_path, index_path, parsed.frozen
                )
                with mock.patch(
                    "eval_router_ab.retrieve_capabilities",
                    wraps=eval_router_ab.retrieve_capabilities,
                ) as retrieval:
                    eval_router_ab.evaluate_cases(parsed.cases, inputs, clock_ns=StepClock())

        self.assertEqual(inputs.frozen.retrieval_algorithm, "lexical-bm25-char3-anchored/v2")
        self.assertTrue(index_loader.call_args.kwargs["frozen_replay"])
        self.assertEqual(retrieval.call_args.kwargs["algorithm"], "lexical-bm25-char3-anchored/v2")
        self.assertTrue(retrieval.call_args.kwargs["frozen_replay"])

    def test_metrics_cover_harm_forbidden_abstention_and_slices(self) -> None:
        frozen = eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3)
        cases = (
            eval_router_ab.ABCase(
                "harm",
                "security",
                "ko",
                "high",
                "secret prompt one",
                eval_router_ab.GoldLabel("safe", ("safe",), False, ("forbidden",)),
            ),
            eval_router_ab.ABCase(
                "abstain",
                "none",
                "mixed",
                "low",
                "secret prompt two",
                eval_router_ab.GoldLabel(None, (), True, ("forbidden",)),
            ),
        )
        evaluations = (
            eval_router_ab.CaseEvaluation(
                cases[0],
                eval_router_ab.SystemOutcome(("safe",), False, "activate", 2.0),
                eval_router_ab.SystemOutcome(("forbidden", "safe", "not-installed"), False, "matched", 4.0),
            ),
            eval_router_ab.CaseEvaluation(
                cases[1],
                eval_router_ab.SystemOutcome((), True, "abstain", 6.0),
                eval_router_ab.SystemOutcome((), False, "no-match", 8.0),
            ),
        )
        manifest_value = eval_router_ab.ExperimentManifest(frozen, cases, "manifest")
        inputs = eval_router_ab.VerifiedInputs(
            {},
            eval_router_ab.InventorySnapshot(
                "available",
                "inv",
                (skill("safe", "Safe skill"), skill("forbidden", "Conflicting but installed skill")),
            ),
            eval_router_ab.CapabilityIndexSnapshot("available", "idx", "inv", (), {}, 0.0),
            Path("index.json"),
            frozen,
        )

        report = eval_router_ab.report_payload(manifest_value, inputs, evaluations)

        self.assertEqual(report["comparison"], {"rescue": 0, "harm": 1, "netWin": -1, "bothCorrect": 0, "bothWrong": 0})
        self.assertEqual(report["pairedStatistics"]["discordant"], 1)
        self.assertEqual(report["pairedStatistics"]["exactMcNemarTwoSidedP"], 1.0)
        self.assertTrue(report["protocol"]["pairedComparisonsExcludeGoldAbstainCases"])
        self.assertEqual(
            report["candidateOnlyComparison"],
            {"rescue": 0, "harm": 1, "netWin": -1, "bothCorrect": 0, "bothWrong": 0},
        )
        self.assertEqual(
            report["b"]["labelledCandidateConflicts"],
            {
                "top1Hits": 1,
                "topKHits": 1,
                "topKAffectedCases": 1,
                "highRiskTopKHits": 1,
                "highRiskTopKAffectedCases": 1,
            },
        )
        self.assertEqual(report["b"]["inventoryIneligibleCandidates"], {"hits": 1, "affectedCases": 1})
        self.assertEqual(report["a"]["abstention"]["recall"], 1.0)
        self.assertEqual(report["b"]["abstention"]["recall"], 0.0)
        self.assertEqual(report["b"]["expectedAbstainLexicalNoMatch"]["recall"], 1.0)
        self.assertEqual(report["b"]["mrr"]["mean"], 0.5)
        self.assertEqual(set(report["slices"]["language"]), {"ko", "mixed"})
        self.assertEqual(set(report["slices"]["risk"]), {"high", "low"})

    def test_manifest_rejects_inconsistent_gold_and_unknown_fields(self) -> None:
        raw = manifest(
            {
                "configRevision": "cfg",
                "inventoryRevision": "inv",
                "indexRevision": "idx",
                "retrievalAlgorithm": "algo",
                "experimentCodeRevision": "code",
                "maxCandidates": 3,
            },
            "prompt",
        )
        raw["cases"][0]["gold"]["acceptableCandidates"] = ["other"]  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "must include the non-null primary"):
            eval_router_ab.parse_manifest(raw)

        raw = manifest(
            {
                "configRevision": "cfg",
                "inventoryRevision": "inv",
                "indexRevision": "idx",
                "retrievalAlgorithm": "algo",
                "experimentCodeRevision": "code",
                "maxCandidates": 3,
            },
            "prompt",
        )
        raw["cases"][0]["promptHash"] = "not-allowed"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "unknown fields: promptHash"):
            eval_router_ab.parse_manifest(raw)

        raw = manifest(
            {
                "configRevision": "cfg",
                "inventoryRevision": "inv",
                "indexRevision": "idx",
                "retrievalAlgorithm": "algo",
                "experimentCodeRevision": "code",
                "maxCandidates": 3,
            },
            "prompt",
        )
        raw["evidence"] = {
            "schema": eval_router_ab.EXPERIMENT_EVIDENCE_SCHEMA,
            "corpusProvenance": "independent-holdout",
            "metadataProvenance": "independent-catalog",
            "independentHoldoutRevision": None,
            "independentAdjudicationRevision": None,
            "ownershipObservationRevision": None,
            "activationObservationRevision": None,
            "outcomeObservationRevision": None,
            "artifactPaths": {"independentHoldout": "holdout.json"},
        }
        with self.assertRaisesRegex(ValueError, "artifactPaths is missing fields"):
            eval_router_ab.parse_manifest(raw)

    def test_frozen_input_mismatch_fails_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path, inventory_path, index_path, frozen = write_experiment_inputs(root)
            frozen["configRevision"] = "sha256:stale"
            parsed = eval_router_ab.parse_manifest(manifest(frozen, "Review pull request regressions"))

            with self.assertRaisesRegex(ValueError, "frozen input mismatch: configRevision"):
                eval_router_ab.verify_inputs(
                    eval_router_ab.load_config(config_path), inventory_path, index_path, parsed.frozen
                )

    def test_cli_json_report_contains_no_raw_prompt(self) -> None:
        sentinel = "CLI_PRIVATE_PROMPT_SENTINEL"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path, inventory_path, index_path, frozen = write_experiment_inputs(root)
            manifest_path = root / "experiment.json"
            manifest_path.write_text(
                json.dumps(manifest(frozen, f"Review pull request regressions {sentinel}")),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                status = eval_router_ab.main(
                    [
                        str(manifest_path),
                        "--config",
                        str(config_path),
                        "--inventory",
                        str(inventory_path),
                        "--index",
                        str(index_path),
                        "--json",
                    ]
                )
            report_path = root / "report.json"
            output_status = eval_router_ab.main(
                [
                    str(manifest_path),
                    "--config",
                    str(config_path),
                    "--inventory",
                    str(inventory_path),
                    "--index",
                    str(index_path),
                    "--output",
                    str(report_path),
                ]
            )
            written_report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(output_status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema"], eval_router_ab.EXPERIMENT_REPORT_SCHEMA)
        self.assertEqual(written_report["schema"], eval_router_ab.EXPERIMENT_REPORT_SCHEMA)
        self.assertNotIn(sentinel, output.getvalue())
        self.assertNotIn(sentinel, json.dumps(written_report, ensure_ascii=False))

    def test_cli_passes_explicit_artifact_root_without_emitting_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            config_path, inventory_path, index_path, frozen = write_experiment_inputs(root)
            evidence = write_evidence_artifacts(root)
            raw_manifest = manifest(frozen, "Review pull request regressions")
            raw_manifest["evidence"] = {
                **eval_router_ab.evidence_payload(evidence),
                "artifactPaths": dict(evidence.artifact_paths),
            }
            manifest_path = root / "experiment.json"
            manifest_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                status = eval_router_ab.main(
                    [
                        str(manifest_path),
                        "--config",
                        str(config_path),
                        "--inventory",
                        str(inventory_path),
                        "--index",
                        str(index_path),
                        "--artifact-root",
                        str(root),
                        "--json",
                    ]
                )

            report = json.loads(output.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(report["promotionGate"]["evidenceVerification"]["status"], "passed")
        self.assertNotIn("PRIVATE_ARTIFACT_PATH", output.getvalue())

    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(eval_router_ab.percentile([1.0, 3.0], 0.50), 2.0)
        self.assertEqual(eval_router_ab.percentile([1.0, 3.0], 0.95), 2.9)
        self.assertIsNone(eval_router_ab.percentile([], 0.50))

    def test_operational_failure_is_not_rewarded_as_correct_abstention(self) -> None:
        case = eval_router_ab.ABCase(
            "degraded",
            "none",
            "en",
            "low",
            "private prompt",
            eval_router_ab.GoldLabel(None, (), True, ()),
        )
        degraded = eval_router_ab.SystemOutcome((), True, "degraded", operational_failure=True)

        self.assertFalse(eval_router_ab.top1_correct(case, degraded))

    def test_promotion_gate_blocks_self_attested_evidence_without_a_verifier(self) -> None:
        case = eval_router_ab.ABCase(
            "holdout-1",
            "review",
            "en",
            "low",
            "Independent private holdout prompt sentinel",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        evidence = eval_router_ab.ExperimentEvidence(
            "independent-holdout",
            "independent-catalog",
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
        )
        experiment = eval_router_ab.ExperimentManifest(
            eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
            (case,),
            "sha256:manifest",
            evidence,
        )
        report = perfect_gate_report()

        gate = eval_router_ab.promotion_gate_v1(report, experiment)

        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(gate["authority"], "none")
        self.assertFalse(gate["autoPromote"])
        self.assertEqual(
            gate["blockers"],
            ["evidence_artifact_verifier_unavailable", "independent_evidence_unverified"],
        )
        self.assertEqual(gate["evidenceVerification"]["status"], "unavailable")

        invalid_experiment = eval_router_ab.ExperimentManifest(
            experiment.frozen,
            (case,),
            "sha256:manifest",
            eval_router_ab.ExperimentEvidence(
                "independent-holdout",
                "independent-catalog",
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:short",
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
            ),
        )
        invalid_gate = eval_router_ab.promotion_gate_v1(report, invalid_experiment)
        self.assertEqual(invalid_gate["status"], "blocked")
        self.assertEqual(
            invalid_gate["blockers"],
            [
                "evidence_artifact_verifier_unavailable",
                "independent_evidence_unverified",
                "ownership_observation_missing",
            ],
        )

        short_case = eval_router_ab.ABCase(
            "short-leak",
            "privacy",
            "en",
            "high",
            "fix",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        short_manifest = eval_router_ab.ExperimentManifest(
            experiment.frozen,
            (short_case,),
            "sha256:manifest",
            evidence,
        )
        short_leak_report = {**report, "cases": [{"text": "prefix fix suffix"}]}
        verification = eval_router_ab.privacy_verification(short_leak_report, short_manifest)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(verification["minimumPromptLeakScanChars"], 1)

    def test_artifact_identity_does_not_self_attest_independence(self) -> None:
        case = eval_router_ab.ABCase(
            "holdout-1",
            "review",
            "en",
            "low",
            "Independent holdout prompt",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            evidence = write_evidence_artifacts(root)
            experiment = eval_router_ab.ExperimentManifest(
                eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
                (case,),
                "sha256:manifest",
                evidence,
            )

            gate = eval_router_ab.promotion_gate_v1(
                perfect_gate_report(),
                experiment,
                artifact_root=root,
            )
            encoded = json.dumps(gate, ensure_ascii=False)

            self.assertEqual(gate["status"], "blocked")
            self.assertEqual(gate["checks"]["artifactEvidence"], "passed")
            self.assertEqual(gate["checks"]["independentEvidence"], "blocked")
            self.assertEqual(gate["blockers"], ["independent_evidence_unverified"])
            self.assertEqual(gate["evidenceVerification"]["status"], "passed")
            self.assertEqual(len(gate["evidenceVerification"]["verifiedArtifactRevisions"]), 5)
            self.assertFalse(gate["evidenceVerification"]["provesIndependence"])
            self.assertFalse(gate["evidenceVerification"]["provesQuality"])
            self.assertNotIn("PRIVATE_ARTIFACT_PATH", encoded)
            self.assertFalse(gate["autoPromote"])

            no_skill_failure_report = json.loads(json.dumps(perfect_gate_report()))
            no_skill_failure_report["b"]["expectedAbstainLexicalNoMatch"]["recall"] = 0.0
            no_skill_failure = eval_router_ab.promotion_gate_v1(
                no_skill_failure_report,
                experiment,
                artifact_root=root,
            )
            self.assertEqual(no_skill_failure["status"], "blocked")
            self.assertIn(
                "expected_abstain_lexical_no_match_below_minimum",
                no_skill_failure["blockers"],
            )
            self.assertNotIn("expected_abstain_no_match_regressed", no_skill_failure["blockers"])

            (root / "PRIVATE_ARTIFACT_PATH.json").write_text("tampered", encoding="utf-8")
            tampered = eval_router_ab.promotion_gate_v1(
                perfect_gate_report(),
                experiment,
                artifact_root=root,
            )
            self.assertEqual(tampered["status"], "blocked")
            self.assertIn("evidence_artifact_verification_failed", tampered["blockers"])
            self.assertIn(
                {"type": "independentHoldout", "reason": "digest_mismatch"},
                tampered["evidenceVerification"]["failures"],
            )
            self.assertNotIn("PRIVATE_ARTIFACT_PATH", json.dumps(tampered, ensure_ascii=False))

    def test_independently_verified_metrics_only_allow_human_review(self) -> None:
        case = eval_router_ab.ABCase(
            "holdout-1",
            "review",
            "en",
            "low",
            "Independent holdout prompt",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        evidence = eval_router_ab.ExperimentEvidence(
            "independent-holdout",
            "independent-catalog",
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
        )
        experiment = eval_router_ab.ExperimentManifest(
            eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
            (case,),
            "sha256:manifest",
            evidence,
        )
        verified = {
            "status": "passed",
            "scope": "independent-evidence-verification",
            "verifiedArtifactRevisions": [],
            "failures": [],
            "provesIndependence": True,
            "provesQuality": False,
        }

        with mock.patch.object(eval_router_ab, "verify_evidence_artifacts", return_value=verified):
            gate = eval_router_ab.promotion_gate_v1(perfect_gate_report(), experiment)

        self.assertEqual(gate["status"], "eligible-for-human-review")
        self.assertEqual(gate["checks"]["independentEvidence"], "passed")
        self.assertEqual(gate["authority"], "none")
        self.assertFalse(gate["autoPromote"])

    def test_artifact_verifier_rejects_unsafe_reused_and_non_regular_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            evidence = write_evidence_artifacts(root)
            paths = dict(evidence.artifact_paths)

            unsafe_paths = dict(paths)
            unsafe_paths["independentHoldout"] = "../outside.json"
            unsafe = eval_router_ab.verify_evidence_artifacts(
                replace(evidence, artifact_paths=tuple(unsafe_paths.items())),
                root,
            )
            self.assertIn(
                {"type": "independentHoldout", "reason": "path_unsafe"},
                unsafe["failures"],
            )

            reused_paths = dict(paths)
            reused_paths["independentAdjudication"] = "./" + paths["independentHoldout"]
            reused = eval_router_ab.verify_evidence_artifacts(
                replace(evidence, artifact_paths=tuple(reused_paths.items())),
                root,
            )
            self.assertEqual(
                sum(failure["reason"] == "path_reused" for failure in reused["failures"]),
                2,
            )

            hardlink = root / "hardlink.json"
            os.link(root / paths["independentHoldout"], hardlink)
            hardlink_paths = dict(paths)
            hardlink_paths["independentAdjudication"] = hardlink.name
            hardlinked = eval_router_ab.verify_evidence_artifacts(
                replace(evidence, artifact_paths=tuple(hardlink_paths.items())),
                root,
            )
            self.assertEqual(
                sum(failure["reason"] == "path_reused" for failure in hardlinked["failures"]),
                2,
            )

            directory = root / "not-regular"
            directory.mkdir()
            non_regular_paths = dict(paths)
            non_regular_paths["outcomeObservation"] = directory.name
            non_regular = eval_router_ab.verify_evidence_artifacts(
                replace(evidence, artifact_paths=tuple(non_regular_paths.items())),
                root,
            )
            self.assertIn(
                {"type": "outcomeObservation", "reason": "file_not_regular"},
                non_regular["failures"],
            )

            symlink = root / "symlink.json"
            symlink.symlink_to(root / paths["outcomeObservation"])
            symlink_paths = dict(paths)
            symlink_paths["outcomeObservation"] = symlink.name
            symlinked = eval_router_ab.verify_evidence_artifacts(
                replace(evidence, artifact_paths=tuple(symlink_paths.items())),
                root,
            )
            self.assertIn(
                {"type": "outcomeObservation", "reason": "path_unsafe"},
                symlinked["failures"],
            )

            actual_root = root / "actual-root"
            actual_root.mkdir()
            root_symlink = root / "root-symlink"
            root_symlink.symlink_to(actual_root, target_is_directory=True)
            symlinked_root = eval_router_ab.verify_evidence_artifacts(evidence, root_symlink)
            self.assertEqual(symlinked_root["status"], "failed")
            self.assertEqual(
                symlinked_root["failures"],
                [{"type": "bundle", "reason": "artifact_root_invalid"}],
            )

    def test_artifact_verifier_does_not_follow_path_swap_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            evidence = write_evidence_artifacts(root)
            paths = dict(evidence.artifact_paths)
            target = root / paths["independentHoldout"]
            external = root.parent / f"{root.name}-external.json"
            external.write_text("external bytes must never be hashed", encoding="utf-8")
            original_digest = eval_router_ab.digest_file_descriptor
            stable_fingerprint = eval_router_ab.file_read_fingerprint(target.stat())
            swapped = False

            def swap_path_before_hash(file_fd: int) -> str:
                nonlocal swapped
                if not swapped:
                    target.unlink()
                    target.symlink_to(external)
                    swapped = True
                return original_digest(file_fd)

            try:
                with (
                    mock.patch.object(
                        eval_router_ab,
                        "digest_file_descriptor",
                        side_effect=swap_path_before_hash,
                    ),
                    mock.patch.object(
                        eval_router_ab,
                        "file_read_fingerprint",
                        return_value=stable_fingerprint,
                    ),
                ):
                    verification = eval_router_ab.verify_evidence_artifacts(evidence, root)
            finally:
                external.unlink(missing_ok=True)

        self.assertTrue(swapped)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["failures"],
            [{"type": "independentHoldout", "reason": "file_changed_during_read"}],
        )
        self.assertEqual(len(verification["verifiedArtifactRevisions"]), 4)

    def test_artifact_verifier_rejects_same_content_leaf_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            evidence = write_evidence_artifacts(root)
            paths = dict(evidence.artifact_paths)
            target = root / paths["independentHoldout"]
            replacement = root / "replacement.json"
            replacement.write_bytes(target.read_bytes())
            original_digest = eval_router_ab.digest_file_descriptor
            stable_fingerprint = eval_router_ab.file_read_fingerprint(target.stat())
            swapped = False

            def replace_path_before_hash(file_fd: int) -> str:
                nonlocal swapped
                if not swapped:
                    os.replace(replacement, target)
                    swapped = True
                return original_digest(file_fd)

            with (
                mock.patch.object(
                    eval_router_ab,
                    "digest_file_descriptor",
                    side_effect=replace_path_before_hash,
                ),
                mock.patch.object(
                    eval_router_ab,
                    "file_read_fingerprint",
                    return_value=stable_fingerprint,
                ),
            ):
                verification = eval_router_ab.verify_evidence_artifacts(evidence, root)

        self.assertTrue(swapped)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["failures"],
            [{"type": "independentHoldout", "reason": "file_changed_during_read"}],
        )
        self.assertEqual(len(verification["verifiedArtifactRevisions"]), 4)

    def test_artifact_verifier_stops_if_file_grows_past_read_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            evidence = write_evidence_artifacts(root)
            target = root / dict(evidence.artifact_paths)["independentHoldout"]
            original_digest = eval_router_ab.digest_file_descriptor
            expanded = False

            def expand_before_hash(file_fd: int) -> str:
                nonlocal expanded
                if not expanded:
                    with target.open("r+b") as handle:
                        handle.truncate(eval_router_ab.MAX_EVIDENCE_ARTIFACT_BYTES + 1)
                    expanded = True
                return original_digest(file_fd)

            with mock.patch.object(
                eval_router_ab,
                "digest_file_descriptor",
                side_effect=expand_before_hash,
            ):
                verification = eval_router_ab.verify_evidence_artifacts(evidence, root)

        self.assertTrue(expanded)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["failures"],
            [{"type": "independentHoldout", "reason": "file_too_large"}],
        )
        self.assertEqual(len(verification["verifiedArtifactRevisions"]), 4)

    def test_promotion_gate_rejects_non_finite_or_non_count_metrics(self) -> None:
        case = eval_router_ab.ABCase(
            "holdout-1",
            "review",
            "en",
            "low",
            "Independent holdout prompt",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        mutations = (
            (("b", "recallAt3", "mean"), math.nan, "candidate_recall_missing"),
            (("b", "candidateTop1Accuracy", "rate"), math.nan, "candidate_top1_missing"),
            (("b", "mrr", "mean"), math.nan, "candidate_mrr_missing"),
            (("b", "precisionAt3", "mean"), math.nan, "candidate_precision_at_3_missing"),
            (
                ("b", "expectedAbstainLexicalNoMatch", "recall"),
                math.nan,
                "expected_abstain_lexical_no_match_missing",
            ),
            (("b", "labelledCandidateConflicts", "top1Hits"), math.nan, "forbidden_top1_count_missing"),
            (
                ("b", "labelledCandidateConflicts", "highRiskTopKHits"),
                math.nan,
                "high_risk_forbidden_top3_count_missing",
            ),
            (
                ("candidateRecallAt3PairedStatistics", "pairedNormalApprox95Ci", 0),
                math.nan,
                "candidate_recall_at_3_ci_missing",
            ),
            (("b", "latency", "p95Ms"), math.nan, "latency_budget_missing"),
            (("b", "inventoryIneligibleCandidates", "hits"), math.nan, "inventory_eligibility_missing"),
            (("b", "operationalFailures"), math.nan, "operational_failure_count_missing"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            experiment = eval_router_ab.ExperimentManifest(
                eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
                (case,),
                "sha256:manifest",
                write_evidence_artifacts(root),
            )
            for path, value, expected_blocker in mutations:
                with self.subTest(path=path):
                    report = copy.deepcopy(perfect_gate_report())
                    target: object = report
                    for key in path[:-1]:
                        target = target[key]  # type: ignore[index]
                    target[path[-1]] = value  # type: ignore[index]

                    gate = eval_router_ab.promotion_gate_v1(report, experiment, artifact_root=root)

                    self.assertEqual(gate["status"], "blocked")
                    self.assertIn(expected_blocker, gate["blockers"])

    def test_promotion_gate_blocks_metric_boundaries_privacy_and_calibration(self) -> None:
        case = eval_router_ab.ABCase(
            "calibration-1",
            "review",
            "en",
            "low",
            "Private calibration prompt sentinel value",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        experiment = eval_router_ab.ExperimentManifest(
            eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
            (case,),
            "sha256:manifest",
            eval_router_ab.ExperimentEvidence(
                "independent-holdout",
                "corpus-informed-calibration",
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
            ),
        )
        report = {
            "reportRevision": "sha256:report",
            "runRevision": "sha256:run",
            "protocol": {"rawPromptsEmitted": False},
            "a": {"abstention": {"recall": 0.95}},
            "b": {
                "recallAt3": {"mean": 0.949999},
                "candidateTop1Accuracy": {"rate": 0.899999},
                "mrr": {"mean": 0.749999},
                "precisionAt3": {"mean": 0.299999},
                "abstention": {"recall": 0.949999},
                "expectedAbstainLexicalNoMatch": {"recall": 0.949999},
                "labelledCandidateConflicts": {
                    "top1Hits": 1,
                    "topKHits": 2,
                    "topKAffectedCases": 1,
                    "highRiskTopKHits": 1,
                    "highRiskTopKAffectedCases": 1,
                },
                "inventoryIneligibleCandidates": {"hits": 1},
                "operationalFailures": 1,
                "latency": {"p95Ms": 20.0001},
            },
            "candidateOnlyPairedStatistics": {"pairedNormalApprox95Ci": [0.0, 0.2]},
            "candidateRecallAt3PairedStatistics": {"pairedNormalApprox95Ci": [0.0, 0.2]},
            "cases": [{"id": "calibration-1", "prompt": case.prompt}],
        }

        gate = eval_router_ab.promotion_gate_v1(report, experiment)

        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(
            set(gate["blockers"]),
            {
                "evidence_artifact_verifier_unavailable",
                "independent_evidence_unverified",
                "metadata_corpus_informed",
                "privacy_verification_failed",
                "candidate_recall_below_minimum",
                "candidate_top1_below_minimum",
                "candidate_mrr_below_minimum",
                "candidate_precision_at_3_below_minimum",
                "expected_abstain_lexical_no_match_below_minimum",
                "forbidden_top1_candidate",
                "high_risk_forbidden_top3_candidate",
                "inventory_ineligible_candidate",
                "operational_failure",
                "candidate_recall_at_3_ci_nonpositive",
                "latency_budget_exceeded",
            },
        )

    def test_decision_revisions_are_stable_across_benchmark_noise(self) -> None:
        case = eval_router_ab.ABCase(
            "stable-1",
            "review",
            "en",
            "low",
            "Stable revision privacy sentinel",
            eval_router_ab.GoldLabel("reviewer", ("reviewer",), False, ()),
        )
        experiment = eval_router_ab.ExperimentManifest(
            eval_router_ab.FrozenInputs("cfg", "inv", "idx", "algo", "code", 3),
            (case,),
            "sha256:manifest",
            eval_router_ab.ExperimentEvidence(
                "synthetic-calibration",
                "active-catalog",
                None,
                None,
                None,
                None,
                None,
            ),
        )

        def report(latency: float, platform: str) -> dict[str, object]:
            value = {
                "environment": {"platform": platform},
                "protocol": {"rawPromptsEmitted": False},
                "a": {"latency": {"p95Ms": latency / 2}, "abstention": {"recall": 1.0}},
                "b": {
                    "recallAt3": {"mean": 0.9},
                    "abstention": {"recall": 1.0},
                    "inventoryIneligibleCandidates": {"hits": 0},
                    "operationalFailures": 0,
                    "latency": {"p95Ms": latency},
                },
                "candidateOnlyPairedStatistics": {"pairedNormalApprox95Ci": [0.1, 0.2]},
                "cases": [{"id": case.case_id, "a": {"latencyMs": latency / 2}, "b": {"latencyMs": latency}}],
            }
            value["reportRevision"] = eval_router_ab.canonical_revision(eval_router_ab.stable_evaluation_payload(value))
            value["runRevision"] = eval_router_ab.canonical_revision(value)
            return value

        first = report(10.0, "first")
        second = report(12.0, "second")
        first_gate = eval_router_ab.promotion_gate_v1(first, experiment)
        second_gate = eval_router_ab.promotion_gate_v1(second, experiment)

        self.assertEqual(first["reportRevision"], second["reportRevision"])
        self.assertNotEqual(first["runRevision"], second["runRevision"])
        self.assertEqual(first_gate["gateRevision"], second_gate["gateRevision"])
        self.assertNotEqual(first_gate["runRevision"], second_gate["runRevision"])


if __name__ == "__main__":
    unittest.main()
