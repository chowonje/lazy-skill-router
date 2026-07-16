from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eval_external_user_holdout as holdout
from lazy_skill_router_common import ConfinedPathIdentity


def revision(character: str) -> str:
    return "sha256:" + character * 64


def study_row() -> dict[str, object]:
    return {
        "schema": holdout.ROW_SCHEMA,
        "recordType": "study",
        "studyId": "study-0123456789abcdef",
        "protocolRevision": revision("a"),
        "frozen": {
            "configRevision": revision("1"),
            "inventoryRevision": revision("2"),
            "indexRevision": revision("3"),
            "indexSchema": "lazy-skill-router.capability-index/v2",
            "retrievalAlgorithm": "lexical-bm25-char3-anchored/v2",
            "experimentCodeRevision": revision("4"),
            "maxCandidates": 3,
        },
        "metrics": list(holdout.METRIC_NAMES),
        "precommitRequired": True,
        "rawPromptStored": False,
        "retuningAllowed": False,
        "authority": "none",
        "autoPromote": False,
    }


def router_result(
    *,
    status: str = "ok",
    disposition: str = "skill",
    skill: str | None = "skill-1111111111111111",
) -> dict[str, object]:
    return {
        "runStatus": status,
        "routerDisposition": disposition,
        "recommendedSkillToken": skill,
    }


def observation_input(
    *,
    fit: str = "appropriate",
    elapsed: int | None = 1200,
    authority: str = "recommendation-only",
) -> dict[str, object]:
    return {
        "fitVerdict": fit,
        "timeToCorrectStartMs": elapsed,
        "authorityAnswer": authority,
    }


class ExternalUserHoldoutTest(unittest.TestCase):
    def test_collect_fsyncs_expectation_before_router_and_keeps_prompt_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            prompt_sentinel = "PRIVATE prompt /Users/example/project"
            fsynced = False
            real_fsync = os.fsync

            def tracked_fsync(descriptor: int) -> None:
                nonlocal fsynced
                fsynced = True
                real_fsync(descriptor)

            def route_after_precommit() -> dict[str, object]:
                self.assertTrue(fsynced)
                rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["recordType"] for row in rows], ["study", "expectation"])
                return router_result()

            fsynced = False
            with mock.patch.object(holdout.os, "fsync", side_effect=tracked_fsync):
                expectation, observation = holdout.collect_case(
                    journal,
                    participant_id="participant-0123456789abcdef",
                    case_id="case-0123456789abcdef",
                    expected_disposition="skill",
                    expected_skill_token="skill-1111111111111111",
                    router_callback=route_after_precommit,
                    observation_input=lambda _result: observation_input(),
                )

            self.assertEqual(observation["expectationRevision"], holdout.canonical_revision(expectation))
            self.assertNotIn(prompt_sentinel, journal.read_text(encoding="utf-8"))
            validated = holdout.load_holdout(journal, require_complete=False)
            self.assertEqual(len(validated["observations"]), 1)

    def test_collect_does_not_reveal_router_when_expectation_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            router_callback = mock.Mock(return_value=router_result())

            with (
                mock.patch.object(holdout.os, "fsync", side_effect=OSError("injected fsync failure")),
                self.assertRaises(OSError),
            ):
                holdout.collect_case(
                    journal,
                    participant_id="participant-0123456789abcdef",
                    case_id="case-0123456789abcdef",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=router_callback,
                    observation_input=lambda _result: observation_input(),
                )

            router_callback.assert_not_called()

    def test_append_rejects_hardlinked_journal_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            journal = root / "holdout.jsonl"
            alias = root / "alias.jsonl"
            holdout.initialize_study(journal, study_row())
            os.link(journal, alias)
            before = journal.read_bytes()
            router_callback = mock.Mock(return_value=router_result())

            with self.assertRaises(ValueError):
                holdout.collect_case(
                    journal,
                    participant_id="participant-0123456789abcdef",
                    case_id="case-0123456789abcdef",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=router_callback,
                    observation_input=lambda _result: observation_input(),
                )

            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(alias.read_bytes(), before)
            router_callback.assert_not_called()

    def test_append_rejects_non_private_existing_mode_without_chmod_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            journal.chmod(0o644)
            before = journal.read_bytes()
            router_callback = mock.Mock(return_value=router_result())

            with self.assertRaises(ValueError):
                holdout.collect_case(
                    journal,
                    participant_id="participant-0123456789abcdef",
                    case_id="case-0123456789abcdef",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=router_callback,
                    observation_input=lambda _result: observation_input(),
                )

            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(journal.stat().st_mode & 0o777, 0o644)
            router_callback.assert_not_called()

    def test_parent_swap_does_not_append_to_the_outside_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            safe = root / "safe"
            held = root / "held"
            outside = root / "outside"
            safe.mkdir()
            outside.mkdir()
            journal = safe / "holdout.jsonl"
            outside_journal = outside / journal.name
            holdout.initialize_study(journal, study_row())
            outside_journal.write_text("outside sentinel\n", encoding="utf-8")
            router_callback = mock.Mock(return_value=router_result())
            real_open_parent = holdout._open_confined_parent
            swapped = False

            def swap_after_verified_parent(*args: object, **kwargs: object) -> object:
                nonlocal swapped
                result = real_open_parent(*args, **kwargs)
                if not swapped:
                    safe.rename(held)
                    os.symlink(outside, safe)
                    swapped = True
                return result

            try:
                with (
                    mock.patch.object(holdout, "_open_confined_parent", side_effect=swap_after_verified_parent),
                    self.assertRaises(ValueError),
                ):
                    holdout.collect_case(
                        journal,
                        participant_id="participant-0123456789abcdef",
                        case_id="case-0123456789abcdef",
                        expected_disposition="abstain",
                        expected_skill_token=None,
                        router_callback=router_callback,
                        observation_input=lambda _result: observation_input(),
                    )
            finally:
                if safe.is_symlink():
                    safe.unlink()
                if held.exists():
                    held.rename(safe)

            self.assertEqual(outside_journal.read_text(encoding="utf-8"), "outside sentinel\n")
            router_callback.assert_not_called()

    def test_final_completion_requires_three_to_five_unique_participants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())

            def add_case(index: int) -> None:
                holdout.collect_case(
                    journal,
                    participant_id=f"participant-{index:016x}",
                    case_id=f"case-{index:016x}",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=lambda: router_result(disposition="abstain", skill=None),
                    observation_input=lambda _result: observation_input(),
                )

            for index in (1, 2):
                add_case(index)
                with self.assertRaises(ValueError):
                    holdout.load_holdout(journal, require_complete=True)
                self.assertEqual(
                    holdout.build_report(holdout.load_holdout(journal, require_complete=False))["collectionStatus"],
                    "incomplete",
                )

            add_case(3)
            self.assertEqual(
                holdout.build_report(holdout.load_holdout(journal, require_complete=True))["collectionStatus"],
                "complete",
            )
            with mock.patch("sys.stdout"):
                self.assertEqual(holdout.main(["validate", str(journal)]), 0)

            for index in (4, 5):
                add_case(index)
            self.assertEqual(
                holdout.build_report(holdout.load_holdout(journal, require_complete=True))["collectionStatus"],
                "complete",
            )

            add_case(6)
            with self.assertRaises(ValueError):
                holdout.load_holdout(journal, require_complete=True)
            self.assertEqual(
                holdout.build_report(holdout.load_holdout(journal, require_complete=False))["collectionStatus"],
                "incomplete",
            )
            with mock.patch("sys.stdout"):
                self.assertEqual(holdout.main(["validate", str(journal)]), 1)

    def test_report_has_exactly_three_metrics_and_preserves_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            holdout.collect_case(
                journal,
                participant_id="participant-0123456789abcdef",
                case_id="case-0123456789abcdef",
                expected_disposition="skill",
                expected_skill_token="skill-1111111111111111",
                router_callback=router_result,
                observation_input=lambda _result: observation_input(),
            )
            holdout.collect_case(
                journal,
                participant_id="participant-2222222222222222",
                case_id="case-1111111111111111",
                expected_disposition="abstain",
                expected_skill_token=None,
                router_callback=lambda: router_result(disposition="abstain", skill=None),
                observation_input=lambda _result: observation_input(
                    fit="not-appropriate",
                    elapsed=None,
                    authority="authorizes-or-executes",
                ),
            )
            holdout.collect_case(
                journal,
                participant_id="participant-1111111111111111",
                case_id="case-2222222222222222",
                expected_disposition="skill",
                expected_skill_token="skill-2222222222222222",
                router_callback=lambda: router_result(
                    status="operational-failure", disposition="unavailable", skill=None
                ),
                observation_input=lambda _result: observation_input(
                    fit="not-observable",
                    elapsed=None,
                    authority="unsure",
                ),
            )

            report = holdout.build_report(holdout.load_holdout(journal, require_complete=True))

            self.assertEqual(set(report["metrics"]), set(holdout.REPORT_METRIC_FIELDS))
            self.assertEqual(report["observed"]["participants"], 3)
            self.assertEqual(report["observed"]["cases"], 3)
            self.assertEqual(report["observed"]["operationalFailures"], 1)
            self.assertEqual(report["metrics"]["recommendationAppropriateness"]["eligible"], 2)
            self.assertEqual(report["metrics"]["recommendationAppropriateness"]["appropriate"], 1)
            self.assertEqual(report["metrics"]["timeToCorrectStartMs"]["notStarted"], 1)
            self.assertEqual(report["metrics"]["authorityDistinctionUnderstanding"]["understood"], 1)
            boundary = report["evidenceBoundary"]
            self.assertEqual(boundary["promotionStatus"], "blocked")
            self.assertEqual(boundary["authority"], "none")
            self.assertFalse(boundary["autoPromote"])
            self.assertFalse(boundary["retuningAllowed"])
            self.assertFalse(boundary["provesIndependence"])
            self.assertFalse(boundary["provesQuality"])

            encoded = json.dumps(report, ensure_ascii=False)
            for private_value in (
                "participant-0123456789abcdef",
                "case-0123456789abcdef",
                "skill-1111111111111111",
            ):
                self.assertNotIn(private_value, encoded)

    def test_strict_json_rejects_prompt_paths_unknown_fields_and_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, encoded in {
                "prompt": json.dumps({**study_row(), "prompt": "private"}),
                "path": json.dumps({**study_row(), "cwd": "/Users/private/project"}),
                "duplicate": '{"schema":"one","schema":"two"}',
                "blank": json.dumps(study_row()) + "\n\n",
            }.items():
                with self.subTest(name=name):
                    path = root / f"{name}.jsonl"
                    path.write_text(encoded + ("" if encoded.endswith("\n") else "\n"), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        holdout.load_holdout(path, require_complete=False)

    def test_pairing_rejects_observation_before_expectation_and_digest_mismatch(self) -> None:
        header = study_row()
        plan_revision = holdout.canonical_revision(header)
        expectation = {
            "schema": holdout.ROW_SCHEMA,
            "recordType": "expectation",
            "studyId": header["studyId"],
            "planRevision": plan_revision,
            "participantId": "participant-0123456789abcdef",
            "caseId": "case-0123456789abcdef",
            "expectedDisposition": "abstain",
            "expectedSkillToken": None,
            "rawPromptStored": False,
        }
        observation = {
            "schema": holdout.ROW_SCHEMA,
            "recordType": "observation",
            "studyId": header["studyId"],
            "planRevision": plan_revision,
            "participantId": expectation["participantId"],
            "caseId": expectation["caseId"],
            "expectationRevision": revision("f"),
            **router_result(disposition="abstain", skill=None),
            **observation_input(),
            "rawPromptStored": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdout.jsonl"
            for name, rows in {
                "wrong-order": [header, observation, expectation],
                "bad-digest": [header, expectation, observation],
                "duplicate": [header, expectation, expectation],
            }.items():
                with self.subTest(name=name):
                    path.write_text(
                        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        holdout.load_holdout(path, require_complete=False)

    def test_incomplete_pair_is_reported_but_final_validation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            with self.assertRaises(RuntimeError):
                holdout.collect_case(
                    journal,
                    participant_id="participant-0123456789abcdef",
                    case_id="case-0123456789abcdef",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=lambda: (_ for _ in ()).throw(RuntimeError("router failed")),
                    observation_input=lambda _result: observation_input(),
                )

            validated = holdout.load_holdout(journal, require_complete=False)
            self.assertEqual(validated["incompleteCases"], 1)
            self.assertEqual(holdout.build_report(validated)["collectionStatus"], "incomplete")
            with self.assertRaises(ValueError):
                holdout.load_holdout(journal, require_complete=True)

    def test_conditional_fields_and_finite_duration_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            cases = (
                ("bool-duration", router_result(), observation_input(elapsed=True)),
                ("negative-duration", router_result(), observation_input(elapsed=-1)),
                (
                    "failure-with-fit",
                    router_result(status="operational-failure", disposition="unavailable", skill=None),
                    observation_input(fit="appropriate", elapsed=None),
                ),
                ("abstain-with-skill", router_result(disposition="abstain"), observation_input()),
            )
            for index, (name, route_value, input_value) in enumerate(cases):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    holdout.collect_case(
                        journal,
                        participant_id="participant-0123456789abcdef",
                        case_id=f"case-{index:016x}",
                        expected_disposition="abstain",
                        expected_skill_token=None,
                        router_callback=lambda value=route_value: value,
                        observation_input=lambda _result, value=input_value: value,
                    )

    def test_safe_snapshot_rejects_symlink_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.jsonl"
            target.write_text(json.dumps(study_row()) + "\n", encoding="utf-8")
            linked = root / "linked.jsonl"
            os.symlink(target, linked)
            with self.assertRaises(ValueError):
                holdout.load_holdout(linked, require_complete=False)

            identity = ConfinedPathIdentity("available", "file", size=holdout.MAX_ARTIFACT_BYTES + 1)
            with mock.patch.object(holdout, "confined_read_regular_snapshot", return_value=(None, identity)):
                with self.assertRaises(ValueError):
                    holdout.load_holdout(target, require_complete=False)

    def test_report_revision_is_deterministic_and_source_revision_binds_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            first = holdout.load_holdout(journal, require_complete=False)
            first_report = holdout.build_report(first)
            self.assertEqual(first_report, holdout.build_report(first))

            second_journal = Path(temp_dir) / "second.jsonl"
            changed_study = {**study_row(), "protocolRevision": revision("b")}
            holdout.initialize_study(second_journal, changed_study)
            second = holdout.load_holdout(second_journal, require_complete=False)
            second_report = holdout.build_report(second)

            self.assertNotEqual(first["sourceRevision"], second["sourceRevision"])
            self.assertNotEqual(first_report["reportRevision"], second_report["reportRevision"])

    def test_cli_distinguishes_complete_incomplete_and_invalid_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = Path(temp_dir) / "holdout.jsonl"
            holdout.initialize_study(journal, study_row())
            with mock.patch("sys.stdout"):
                self.assertEqual(holdout.main(["validate", str(journal)]), 1)

            holdout.collect_case(
                journal,
                participant_id="participant-0123456789abcdef",
                case_id="case-0123456789abcdef",
                expected_disposition="abstain",
                expected_skill_token=None,
                router_callback=lambda: router_result(disposition="abstain", skill=None),
                observation_input=lambda _result: observation_input(),
            )
            for index in (1, 2):
                holdout.collect_case(
                    journal,
                    participant_id=f"participant-{index:016x}",
                    case_id=f"case-{index:016x}",
                    expected_disposition="abstain",
                    expected_skill_token=None,
                    router_callback=lambda: router_result(disposition="abstain", skill=None),
                    observation_input=lambda _result: observation_input(),
                )
            with mock.patch("sys.stdout"):
                self.assertEqual(holdout.main(["validate", str(journal)]), 0)
                self.assertEqual(holdout.main(["report", str(journal)]), 0)

            invalid = Path(temp_dir) / "invalid.jsonl"
            invalid.write_text('{"prompt":"private"}\n', encoding="utf-8")
            with mock.patch("sys.stderr"):
                self.assertEqual(holdout.main(["validate", str(invalid)]), 2)


if __name__ == "__main__":
    unittest.main()
