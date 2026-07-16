from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import lazy_skill_router_common as common_module

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "eval_portable_beta.py"
CHECKED_IN_MANIFEST = ROOT / "eval" / "portable_beta_manifest.json"
HISTORICAL_MANIFEST = ROOT / "eval" / "portable_beta_manifest_2026-07-13.json"
CHECKED_IN_REPORT = ROOT / "docs" / "evaluation" / "portable-beta-report-2026-07-13.json"


def load_module():
    spec = importlib.util.spec_from_file_location("eval_portable_beta", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load eval_portable_beta module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_portable_beta"] = module
    spec.loader.exec_module(module)
    return module


eval_portable_beta = load_module()


def fixture_payload(*, wrong_owner: bool = False) -> dict[str, object]:
    return {
        "schema": "lazy-skill-router.portable-catalog-authoring/v1",
        "authoring": {
            "authorId": "unit-author",
            "scorerAccess": False,
            "existingCorpusAccess": False,
            "purpose": "one-shot-opt-in-beta-holdout",
        },
        "catalogs": [
            {
                "id": "unit-catalog",
                "description": "Small unit-test catalog.",
                "skills": [
                    {
                        "name": "diagram-maker",
                        "description": "Create architecture diagrams and dependency maps.",
                        "aliases": ["diagram builder"],
                        "capabilities": ["architecture diagram"],
                        "phases": ["design"],
                    },
                    {
                        "name": "table-editor",
                        "description": "Edit tables, rows, and columns in structured documents.",
                        "aliases": [],
                        "capabilities": ["table editing"],
                        "phases": ["editing"],
                    },
                ],
                "cases": [
                    {
                        "id": "unit-positive",
                        "prompt": "Create an architecture diagram and dependency map.",
                        "language": "en",
                        "category": "diagram",
                        "expectedSkills": ["table-editor" if wrong_owner else "diagram-maker"],
                        "expectedNoMatch": False,
                    },
                    {
                        "id": "unit-no-skill",
                        "prompt": "Schedule a dental appointment for tomorrow morning.",
                        "language": "en",
                        "category": "no-skill",
                        "expectedSkills": [],
                        "expectedNoMatch": True,
                    },
                ],
            }
        ],
    }


def write_suite(root: Path, *, wrong_owner: bool = False, wrong_digest: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    fixture_path = root / "author.json"
    fixture_path.write_text(
        json.dumps(fixture_payload(wrong_owner=wrong_owner), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    if wrong_digest:
        digest = "0" * 64
    manifest = {
        "schema": "lazy-skill-router.portable-beta-manifest/v1",
        "suiteId": "unit-suite",
        "createdAt": "2026-07-13",
        "algorithm": "lexical-bm25-char3-anchored/v2",
        "retrievalImplementationRevision": eval_portable_beta.RETRIEVAL_IMPLEMENTATION_REVISION,
        "maxCandidates": 3,
        "fixtureFiles": [
            {
                "path": "author.json",
                "sha256": f"sha256:{digest}",
                "authorId": "unit-author",
            }
        ],
        "scenarios": [
            {
                "id": "unit-scenario",
                "catalogs": ["unit-catalog"],
                "caseCatalogs": ["unit-catalog"],
            }
        ],
        "gate": {
            "minAuthors": 1,
            "minCatalogs": 1,
            "minCases": 2,
            "minPositiveCases": 1,
            "minNoSkillCases": 1,
            "minPositiveRecallAt3": 1.0,
            "minPositiveTop1": 1.0,
            "minNoSkillNoMatch": 1.0,
            "minCatalogRecallAt3": 1.0,
            "minCatalogNoSkillNoMatch": 1.0,
            "minScenarioRecallAt3": 1.0,
            "minScenarioNoSkillNoMatch": 1.0,
            "minLanguageRecallAt3": 1.0,
            "maxDegradedCases": 0,
            "maxIneligibleCandidates": 0,
            "maxP95LatencyMs": 20.0,
        },
        "semantics": {
            "scope": "explicit-cli-preview-only",
            "oneShot": True,
            "scorerFrozenBeforeEvaluation": True,
            "externalUserValidationSubstitute": False,
            "hookActivationAuthorized": False,
            "automaticRelease": False,
            "rawPromptsEmitted": False,
        },
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def refresh_fixture_digest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture_path = manifest_path.parent / manifest["fixtureFiles"][0]["path"]
    digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    manifest["fixtureFiles"][0]["sha256"] = f"sha256:{digest}"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def upgrade_manifest_to_v2(manifest_path: Path, evaluation_revision: str) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "schema": "lazy-skill-router.portable-beta-manifest/v2",
            "evaluationImplementationRevision": evaluation_revision,
            "indexSchema": "lazy-skill-router.capability-index/v2",
            "featureExtractor": "lexical-word-char3/v1",
            "evidenceRole": "self-attested-internal-release-regression",
        }
    )
    manifest["semantics"]["oneShot"] = False
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


class PortableBetaEvalTest(unittest.TestCase):
    def test_report_is_prompt_redacted_and_passes_predeclared_unit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            suite = eval_portable_beta.load_suite(manifest_path)
            report = eval_portable_beta.evaluate_suite(suite)

        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["schema"], "lazy-skill-router.portable-beta-report/v1")
        self.assertEqual(report["gate"]["status"], "eligible-for-opt-in-beta-review")
        self.assertNotIn("Create an architecture diagram", encoded)
        self.assertNotIn("Schedule a dental appointment", encoded)
        self.assertNotIn(temp_dir, encoded)
        self.assertFalse(report["semantics"]["externalUserValidationSubstitute"])
        self.assertFalse(report["semantics"]["hookActivationAuthorized"])

    def test_gate_blocks_when_positive_owner_floor_is_missed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            suite = eval_portable_beta.load_suite(write_suite(Path(temp_dir), wrong_owner=True))
            report = eval_portable_beta.evaluate_suite(suite)

        self.assertEqual(report["gate"]["status"], "blocked")
        self.assertIn("positive_recall_at_3_below_minimum", report["gate"]["blockers"])

    def test_fixture_digest_mismatch_is_rejected_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir), wrong_digest=True)
            with self.assertRaisesRegex(ValueError, "fixture digest mismatch"):
                eval_portable_beta.load_suite(manifest_path)

    def test_fixture_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_suite(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["fixtureFiles"][0]["path"] = "../outside.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture path"):
                eval_portable_beta.load_suite(manifest_path)

    def test_json_snapshot_rejects_leaf_swap_between_stat_and_open(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            document = root / "document.json"
            displaced = root / "document.original.json"
            outside = root / "outside.json"
            document.write_text('{"source":"original"}\n', encoding="utf-8")
            outside.write_text('{"source":"outside-private-sentinel"}\n', encoding="utf-8")
            original_open = common_module.os.open
            swapped = False

            def swap_leaf(path, flags, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and path == document.name
                    and kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    document.rename(displaced)
                    os.symlink(outside, document)
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(common_module.os, "open", side_effect=swap_leaf),
                self.assertRaisesRegex(ValueError, "unsafe|unreadable|regular|changed"),
            ):
                eval_portable_beta.read_json_object(document, "test document")

            self.assertTrue(swapped)
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"source":"outside-private-sentinel"}\n')

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
        "POSIX FIFO support required",
    )
    def test_json_snapshot_fifo_swap_is_opened_nonblocking_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            document = root / "document.json"
            displaced = root / "document.original.json"
            outside = root / "outside.json"
            document.write_text('{"source":"original"}\n', encoding="utf-8")
            outside.write_text('{"source":"outside-private-sentinel"}\n', encoding="utf-8")
            original_open = common_module.os.open
            swapped = False

            def swap_leaf_to_fifo(path, flags, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and path == document.name
                    and kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    document.rename(displaced)
                    os.mkfifo(document)
                    swapped = True
                    self.assertTrue(flags & os.O_NONBLOCK, "confined leaf open must not block on a raced FIFO")
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(common_module.os, "open", side_effect=swap_leaf_to_fifo),
                self.assertRaisesRegex(ValueError, "unsafe|unreadable|regular|changed"),
            ):
                eval_portable_beta.read_json_object(document, "test document")

            self.assertTrue(swapped)
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"source":"outside-private-sentinel"}\n')

    def test_json_snapshot_stays_on_validated_parent_when_path_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            parent = root / "parent"
            parent.mkdir()
            document = parent / "document.json"
            document.write_text('{"source":"original"}\n', encoding="utf-8")
            displaced = root / "validated-parent"
            original_open_parent = common_module._open_confined_parent
            swapped = False

            def swap_parent(*args, **kwargs):
                nonlocal swapped
                result = original_open_parent(*args, **kwargs)
                if not swapped:
                    parent.rename(displaced)
                    parent.mkdir()
                    (parent / document.name).write_text(
                        '{"source":"replacement-private-sentinel"}\n',
                        encoding="utf-8",
                    )
                    swapped = True
                return result

            with mock.patch.object(common_module, "_open_confined_parent", side_effect=swap_parent):
                parsed, _ = eval_portable_beta.read_json_object(document, "test document")

            self.assertTrue(swapped)
            self.assertEqual(parsed, {"source": "original"})
            self.assertEqual(
                (parent / document.name).read_text(encoding="utf-8"),
                '{"source":"replacement-private-sentinel"}\n',
            )

    def test_json_snapshot_rejects_oversized_replacement_after_size_check(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            document = root / "document.json"
            displaced = root / "document.original.json"
            document.write_text('{"source":"original"}\n', encoding="utf-8")
            original_open = common_module.os.open
            swapped = False

            def replace_with_oversized_file(path, flags, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and path == document.name
                    and kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                ):
                    document.rename(displaced)
                    document.write_bytes(b"{" + b"x" * (eval_portable_beta.MAX_JSON_BYTES + 1) + b"}")
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(common_module.os, "open", side_effect=replace_with_oversized_file),
                self.assertRaisesRegex(ValueError, "size|unsafe|unreadable|changed"),
            ):
                eval_portable_beta.read_json_object(document, "test document")

            self.assertTrue(swapped)

    def test_nonfinite_latency_thresholds_are_structural_errors(self) -> None:
        for label, value in (
            ("NaN", float("nan")),
            ("Infinity", float("inf")),
            ("1e309", float("1e309")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                manifest_path = write_suite(Path(temp_dir))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["gate"]["maxP95LatencyMs"] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "finite|constant|latency"):
                    eval_portable_beta.load_suite(manifest_path)

    def test_output_leaf_swap_never_writes_external_target(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_suite(root / "suite")
            output_root = root / "output"
            output_root.mkdir()
            output = output_root / "report.json"
            output.write_text("existing report\n", encoding="utf-8")
            outside = root / "outside-sentinel.json"
            outside.write_text("outside sentinel\n", encoding="utf-8")
            original_write = common_module.confined_atomic_write_bytes
            swapped = False

            def swap_leaf(path, content, managed_root, expected):
                nonlocal swapped
                output.unlink()
                os.symlink(outside, output)
                swapped = True
                return original_write(path, content, managed_root, expected)

            with (
                mock.patch.object(common_module, "confined_atomic_write_bytes", side_effect=swap_leaf),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = eval_portable_beta.main([str(manifest_path), "--output", str(output)])

            self.assertTrue(swapped)
            self.assertEqual(status, 2)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside sentinel\n")

    def test_output_parent_swap_gets_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_suite(root / "suite")
            output_root = root / "output"
            output_root.mkdir()
            output = output_root / "report.json"
            displaced = root / "validated-output"
            sentinel = output_root / "sentinel.txt"
            original_stage = common_module.confined_stage_bytes
            swapped = False

            def swap_parent(*args, **kwargs):
                nonlocal swapped
                staged = original_stage(*args, **kwargs)
                output_root.rename(displaced)
                output_root.mkdir()
                sentinel.write_text("replacement sentinel\n", encoding="utf-8")
                swapped = True
                return staged

            with (
                mock.patch.object(common_module, "confined_stage_bytes", side_effect=swap_parent),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = eval_portable_beta.main([str(manifest_path), "--output", str(output)])

            self.assertTrue(swapped)
            self.assertEqual(status, 2)
            self.assertEqual({path.name for path in output_root.iterdir()}, {"sentinel.txt"})
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "replacement sentinel\n")

    def test_unknown_expected_skill_is_rejected_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_suite(root)
            fixture_path = root / "author.json"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture["catalogs"][0]["cases"][0]["expectedSkills"] = ["unknown-skill"]
            fixture_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
            refresh_fixture_digest(manifest_path)

            with self.assertRaisesRegex(ValueError, "references unknown skills"):
                eval_portable_beta.load_suite(manifest_path)

    def test_retrieval_implementation_revision_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["retrievalImplementationRevision"] = "sha256:" + "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "retrieval implementation revision mismatch"):
                eval_portable_beta.load_suite(manifest_path)

    def test_v1_manifest_uses_frozen_v1_index_and_non_degraded_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["algorithm"] = "lexical-bm25-char3/v1"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            suite = eval_portable_beta.load_suite(manifest_path)
            with mock.patch.object(
                eval_portable_beta,
                "retrieve_capabilities",
                wraps=eval_portable_beta.retrieve_capabilities,
            ) as retrieve:
                report = eval_portable_beta.evaluate_suite(suite)

        self.assertEqual(suite.index_schema, "lazy-skill-router.capability-index/v1")
        self.assertEqual(suite.feature_extractor, "lexical-word-char3/v1")
        self.assertTrue(all(scenario["indexSchema"] == suite.index_schema for scenario in report["scenarios"]))
        self.assertEqual(report["metrics"]["degradedCases"], 0)
        self.assertTrue(all(case["status"] != "degraded" for case in report["cases"]))
        self.assertTrue(retrieve.call_args_list)
        self.assertTrue(all(call.kwargs["frozen_replay"] is True for call in retrieve.call_args_list))
        self.assertTrue(all(call.kwargs["algorithm"] == suite.algorithm for call in retrieve.call_args_list))

    def test_manifest_rejects_more_than_64_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            template = manifest["scenarios"][0]
            manifest["scenarios"] = [
                {**template, "id": f"scenario-{index}"} for index in range(eval_portable_beta.MAX_SCENARIOS + 1)
            ]
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "scenario.*limit"):
                eval_portable_beta.load_suite(manifest_path)

    def test_manifest_rejects_more_than_16384_total_evaluations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_suite(root)
            fixture_path = root / "author.json"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            cases = fixture["catalogs"][0]["cases"]
            positive = cases[0]
            case_count = eval_portable_beta.MAX_TOTAL_EVALUATIONS // eval_portable_beta.MAX_SCENARIOS + 1
            cases.extend({**positive, "id": f"unit-positive-{index}"} for index in range(case_count - len(cases)))
            fixture_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
            refresh_fixture_digest(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            template = manifest["scenarios"][0]
            manifest["scenarios"] = [
                {**template, "id": f"scenario-{index}"} for index in range(eval_portable_beta.MAX_SCENARIOS)
            ]
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "evaluation.*limit"):
                eval_portable_beta.load_suite(manifest_path)

    def test_evaluator_revision_drift_after_load_is_a_structural_error(self) -> None:
        first_revision = "sha256:" + "1" * 64
        second_revision = "sha256:" + "2" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            upgrade_manifest_to_v2(manifest_path, first_revision)

            with (
                mock.patch.object(
                    eval_portable_beta,
                    "evaluation_implementation_revision",
                    side_effect=(first_revision, second_revision),
                ),
                self.assertRaisesRegex(ValueError, "evaluation implementation.*changed"),
            ):
                suite = eval_portable_beta.load_suite(manifest_path)
                eval_portable_beta.evaluate_suite(suite)

    def test_stable_report_revision_excludes_latency_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = eval_portable_beta.evaluate_suite(eval_portable_beta.load_suite(write_suite(Path(temp_dir))))
        stable_revision = eval_portable_beta.canonical_revision(eval_portable_beta.stable_report_payload(report))
        report["generatedAt"] = "2099-01-01T00:00:00Z"
        report["environment"] = {"python": "different", "platform": "different"}
        report["metrics"]["latency"] = {"p50Ms": 999.0, "p95Ms": 999.0, "p99Ms": 999.0}

        self.assertEqual(
            stable_revision,
            eval_portable_beta.canonical_revision(eval_portable_beta.stable_report_payload(report)),
        )

    def test_worst_scenario_blocks_even_when_aggregate_metrics_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            suite = eval_portable_beta.load_suite(write_suite(Path(temp_dir)))
            report = eval_portable_beta.evaluate_suite(suite)
        report["slices"]["scenario"]["unit-scenario"]["positiveRecallAt3"]["rate"] = 0.0

        gate = eval_portable_beta.gate_payload(suite, report)

        self.assertEqual(gate["status"], "blocked")
        self.assertIn("scenario_recall_at_3_below_minimum", gate["blockers"])

    def test_cli_exit_codes_distinguish_pass_block_and_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            passing = write_suite(root / "pass")
            blocked = write_suite(root / "blocked", wrong_owner=True)
            invalid = write_suite(root / "invalid", wrong_digest=True)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                pass_code = eval_portable_beta.main([str(passing), "--json"])
                blocked_code = eval_portable_beta.main([str(blocked), "--json"])
                invalid_code = eval_portable_beta.main([str(invalid), "--json"])

        self.assertEqual(pass_code, 0)
        self.assertEqual(blocked_code, 1)
        self.assertEqual(invalid_code, 2)

    def test_cli_sanitizes_unexpected_internal_error_as_exit_two(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_suite(Path(temp_dir))
            with (
                mock.patch.object(eval_portable_beta, "evaluate_suite", side_effect=RuntimeError("PRIVATE SENTINEL")),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = eval_portable_beta.main([str(manifest_path), "--json"])

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unexpected evaluator failure", stderr.getvalue())
        self.assertNotIn("PRIVATE SENTINEL", stderr.getvalue())

    def test_current_release_regression_is_frozen_balanced_and_internal(self) -> None:
        suite = eval_portable_beta.load_suite(CHECKED_IN_MANIFEST)

        self.assertEqual(len(suite.author_ids), 2)
        self.assertEqual(len(suite.catalogs), 4)
        self.assertEqual(len(suite.cases), 64)
        self.assertEqual(len(suite.scenarios), 7)
        self.assertEqual(sum(not case.expected_no_match for case in suite.cases), 48)
        self.assertEqual(sum(case.expected_no_match for case in suite.cases), 16)
        self.assertTrue(suite.semantics["scorerFrozenBeforeEvaluation"])
        self.assertFalse(suite.semantics["oneShot"])
        self.assertFalse(suite.semantics["externalUserValidationSubstitute"])
        self.assertEqual(suite.evidence_role, eval_portable_beta.CURRENT_EVIDENCE_ROLE)
        self.assertEqual(
            suite.evaluation_implementation_revision,
            eval_portable_beta.evaluation_implementation_revision(),
        )
        self.assertEqual(suite.index_schema, eval_portable_beta.CAPABILITY_INDEX_SCHEMA)
        self.assertEqual(suite.feature_extractor, eval_portable_beta.FEATURE_EXTRACTOR_V1)

    def test_historical_blocked_report_and_manifest_remain_exact_and_prompt_redacted(self) -> None:
        historical_manifest_revision = eval_portable_beta.bytes_revision(HISTORICAL_MANIFEST.read_bytes())
        encoded = CHECKED_IN_REPORT.read_text(encoding="utf-8")
        report = json.loads(encoded)
        stable_revision = eval_portable_beta.canonical_revision(eval_portable_beta.stable_report_payload(report))
        gate = dict(report["gate"])
        gate_revision = gate.pop("gateRevision")

        self.assertEqual(
            historical_manifest_revision, "sha256:5f87e73f04040fb4bf9f918d09f785147b62d4995838c07e18c39d7b9fed5057"
        )
        self.assertEqual(report["manifestRevision"], historical_manifest_revision)
        self.assertEqual(
            eval_portable_beta.bytes_revision(CHECKED_IN_REPORT.read_bytes()),
            "sha256:5cede049ecfb56cb302fb2a435393b79baae574c3d5f01ab10a16437453c116d",
        )
        self.assertEqual(report["reportRevision"], stable_revision)
        self.assertEqual(gate_revision, eval_portable_beta.canonical_revision(gate))
        self.assertEqual(report["gate"]["status"], "blocked")
        self.assertEqual(
            report["gate"]["blockers"],
            ["positive_recall_at_3_below_minimum", "language_recall_at_3_below_minimum"],
        )
        self.assertNotIn('"prompt"', encoded)
        self.assertNotIn("/Users/", encoded)

    def test_current_release_regression_exits_one_and_writes_inside_repo_boundary(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            output = Path(temp_dir) / "PORTABLE_BETA_REPORT.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = eval_portable_beta.main([str(CHECKED_IN_MANIFEST), "--output", str(output)])

            self.assertEqual(status, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["gate"]["status"], "blocked")
            self.assertEqual(report["evidenceRole"], eval_portable_beta.CURRENT_EVIDENCE_ROLE)
            self.assertNotIn('"prompt"', output.read_text(encoding="utf-8"))

    def test_output_outside_current_working_tree_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir=ROOT) as suite_dir,
            tempfile.TemporaryDirectory(dir=ROOT.parent) as output_dir,
        ):
            manifest_path = write_suite(Path(suite_dir))
            output = Path(output_dir) / "report.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = eval_portable_beta.main([str(manifest_path), "--output", str(output)])

            self.assertEqual(status, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
