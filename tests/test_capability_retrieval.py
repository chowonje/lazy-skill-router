from __future__ import annotations

import copy
import io
import json
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lazy_skill_router import main as hook_main
from lazy_skill_router_capability_index import (
    CAPABILITY_INDEX_SCHEMA_V1,
    FEATURE_EXTRACTOR_V1,
    build_capability_index,
    build_capability_index_v1,
)
from lazy_skill_router_inventory import InventorySnapshot
from lazy_skill_router_retrieval import (
    PRODUCT_PREVIEW_ALGORITHM,
    RETRIEVAL_ALGORITHM_V1,
    RETRIEVAL_ALGORITHM_V2,
    RETRIEVAL_IMPLEMENTATION_FILES,
    retrieval_implementation_revision,
    retrieve_capabilities,
)

ROOT = Path(__file__).resolve().parents[1]


def skill(
    name: str,
    description: str,
    *,
    aliases: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, object]:
    return {
        "configured_name": name,
        "canonical_id": f"user/codex/skills/{name}",
        "description": description,
        "aliases": aliases or [],
        "capabilities": capabilities or [],
        "phases": [],
        "availability": {"status": "available"},
    }


def inventory(revision: str = "sha256:inventory-v1") -> InventorySnapshot:
    return InventorySnapshot(
        "available",
        revision,
        (
            skill("code-review", "Review code changes, pull requests, and regressions."),
            skill("github:gh-address-comments", "Address pull request review comments and feedback."),
            skill("security-threat-model", "Build a repository grounded security threat model."),
            skill("pdf", "Read and review PDF documents and rendered page layout."),
            skill("ponytail", "Choose the smallest implementation that works."),
        ),
    )


def write_fixture(root: Path, source: InventorySnapshot) -> tuple[dict[str, object], Path]:
    routes_path = root / "routes.json"
    index_path = root / "capability-index.json"
    config: dict[str, object] = {
        "_loaded_from": str(routes_path),
        "capabilityRetrieval": {"mode": "shadow", "maxCandidates": 3},
        "routes": [],
    }
    index_path.write_text(
        json.dumps(
            build_capability_index(source, generated_at="2026-07-12T00:00:00Z"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config, index_path


class CapabilityRetrievalTest(unittest.TestCase):
    def test_normal_hook_output_is_identical_when_shadow_measurement_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            index_path = root / "capability-index.json"
            index_path.write_text(
                json.dumps(
                    build_capability_index(source, generated_at="2026-07-12T00:00:00Z"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            base_config = {
                "_loaded_from": str(root / "routes.json"),
                "allowedSkills": ["ponytail"],
                "minConfidence": 0.5,
                "activation": {"mode": "inject", "autoActivateMinStrength": 0.8},
                "routes": [
                    {
                        "name": "ponytail",
                        "primary": "ponytail",
                        "supporting": [],
                        "verification": "",
                        "patterns": ["ponytail", "smallest"],
                        "reason": "Use the requested minimal implementation skill.",
                    }
                ],
            }
            prompt = "Use ponytail to implement the smallest working change."
            baseline = io.StringIO()
            with (
                mock.patch("lazy_skill_router.load_config", return_value=base_config),
                mock.patch("lazy_skill_router.inventory_for_config", return_value=source),
                redirect_stdout(baseline),
            ):
                baseline_status = hook_main(["--prompt", prompt])

            shadow_config = copy.deepcopy(base_config)
            shadow_config["capabilityRetrieval"] = {"mode": "shadow", "maxCandidates": 3}
            shadow_config["logging"] = {
                "enabled": True,
                "path": str(root / "measurements.jsonl"),
            }
            shadow = io.StringIO()
            with (
                mock.patch("lazy_skill_router.load_config", return_value=shadow_config),
                mock.patch("lazy_skill_router.inventory_for_config", return_value=source),
                redirect_stdout(shadow),
            ):
                shadow_status = hook_main(["--prompt", prompt])
            measurement_event = json.loads((root / "measurements.jsonl").read_text(encoding="utf-8"))

            failed_shadow = io.StringIO()
            with (
                mock.patch("lazy_skill_router.load_config", return_value=shadow_config),
                mock.patch("lazy_skill_router.inventory_for_config", return_value=source),
                mock.patch(
                    "lazy_skill_router_retrieval.retrieve_capabilities",
                    side_effect=RuntimeError("injected retrieval failure"),
                ),
                redirect_stdout(failed_shadow),
            ):
                failed_shadow_status = hook_main(["--prompt", prompt])
            measurement_events = [
                json.loads(line) for line in (root / "measurements.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            off_config = copy.deepcopy(shadow_config)
            off_config["activation"]["mode"] = "off"
            off_config["logging"]["path"] = str(root / "off-measurements.jsonl")
            off_output = io.StringIO()
            with (
                mock.patch("lazy_skill_router.load_config", return_value=off_config),
                mock.patch("lazy_skill_router.inventory_for_config", return_value=source),
                redirect_stdout(off_output),
            ):
                off_status = hook_main(["--prompt", prompt])
            off_event = json.loads((root / "off-measurements.jsonl").read_text(encoding="utf-8"))

        self.assertEqual(baseline_status, 0)
        self.assertEqual(shadow_status, 0)
        self.assertEqual(failed_shadow_status, 0)
        self.assertEqual(off_status, 0)
        self.assertTrue(baseline.getvalue())
        self.assertEqual(shadow.getvalue(), baseline.getvalue())
        self.assertEqual(failed_shadow.getvalue(), baseline.getvalue())
        self.assertEqual(off_output.getvalue(), "")
        self.assertEqual(off_event["decisionStatus"], "off")
        self.assertEqual(off_event["retrievalStatus"], "matched")
        self.assertEqual(off_event["routingObservation"]["activation"]["source"], "unobserved")
        self.assertEqual(measurement_event["retrievalStatus"], "matched")
        self.assertEqual(measurement_event["retrievalAlgorithm"], PRODUCT_PREVIEW_ALGORITHM)
        self.assertRegex(measurement_event["retrievalImplementationRevision"], r"^sha256:[0-9a-f]{64}$")
        self.assertIn("ponytail", measurement_event["capabilityCandidateSkillIds"])
        self.assertEqual(measurement_events[0]["routingObservation"]["stop"]["action"], "observe-only")
        self.assertIn(
            "configured_name.lexical",
            measurement_events[0]["routingObservation"]["retrieval"]["candidates"][0]["evidenceIds"],
        )
        self.assertEqual(measurement_events[1]["retrievalStatus"], "degraded")
        self.assertEqual(measurement_events[1]["routingObservation"]["stop"]["action"], "fallback-legacy")
        self.assertNotIn(prompt, json.dumps(measurement_events, ensure_ascii=False))

    def test_shadow_result_is_bounded_redacted_and_cannot_change_legacy_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            config, _ = write_fixture(root, source)
            before_config = copy.deepcopy(config)
            before_inventory = copy.deepcopy(source)
            prompt = "Address GitHub PR review comments. PRIVATE_PROMPT_SENTINEL"

            result = retrieve_capabilities(
                prompt,
                config,
                source,
                legacy_route="legacy-review",
                legacy_primary="code-review",
            )

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["status"], "matched")
        self.assertLessEqual(len(result["candidates"]), 3)
        self.assertEqual(result["legacy"]["routeId"], "legacy-review")
        self.assertEqual(result["legacy"]["primaryConfiguredName"], "code-review")
        self.assertFalse(result["semantics"]["affectsLegacySelection"])
        self.assertFalse(result["semantics"]["affectsActivation"])
        self.assertEqual(result["semantics"]["noMatchScope"], "lexical-retrieval-only")
        self.assertFalse(result["semantics"]["ownsSemanticAbstention"])
        self.assertTrue(result["semantics"]["requiresHostOwnershipDecision"])
        self.assertFalse(result["semantics"]["executionRequested"])
        self.assertEqual(config, before_config)
        self.assertEqual(source, before_inventory)
        self.assertNotIn(prompt, encoded)
        self.assertNotIn("PRIVATE_PROMPT_SENTINEL", encoded)
        for entry in source.skills:
            self.assertNotIn(str(entry["description"]), encoded)

    def test_frozen_v1_index_and_algorithm_require_explicit_replay_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            config, index_path = write_fixture(root, source)
            index_path.write_text(
                json.dumps(
                    build_capability_index_v1(source, generated_at="2026-07-12T00:00:00Z"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            product = retrieve_capabilities(
                "Review this pull request.",
                config,
                source,
                algorithm=RETRIEVAL_ALGORITHM_V1,
            )
            result = retrieve_capabilities(
                "Review this pull request.",
                config,
                source,
                algorithm=RETRIEVAL_ALGORITHM_V1,
                frozen_replay=True,
            )

        self.assertEqual(product["status"], "degraded")
        self.assertEqual(product["reasonCodes"], ["retrieval_algorithm_v1_replay_only"])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["algorithm"], RETRIEVAL_ALGORITHM_V1)
        self.assertEqual(result["indexSchema"], CAPABILITY_INDEX_SCHEMA_V1)
        self.assertEqual(result["featureExtractor"], FEATURE_EXTRACTOR_V1)

    def test_retrieval_implementation_revision_binds_both_scorer_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in RETRIEVAL_IMPLEMENTATION_FILES:
                (root / name).write_text(name, encoding="utf-8")
            initial = retrieval_implementation_revision(root)
            (root / "lazy_skill_router_retrieval.py").write_text("retrieval changed", encoding="utf-8")
            retrieval_changed = retrieval_implementation_revision(root)
            (root / "lazy_skill_router_capability_index.py").write_text("index changed", encoding="utf-8")
            index_changed = retrieval_implementation_revision(root)

        self.assertRegex(initial or "", r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(initial, retrieval_changed)
        self.assertNotEqual(retrieval_changed, index_changed)
        self.assertIsNone(retrieval_implementation_revision(Path("/definitely/missing")))

    def test_standalone_shadow_diagnostic_pins_product_preview_algorithm(self) -> None:
        source = inventory()
        config = {"routes": []}
        payload = {
            "status": "no-match",
            "candidates": [],
            "reasonCodes": [],
        }

        with (
            mock.patch("lazy_skill_router.load_config", return_value=config),
            mock.patch("lazy_skill_router.inventory_for_config", return_value=source),
            mock.patch("lazy_skill_router_retrieval.retrieve_capabilities", return_value=payload) as retrieve,
            redirect_stdout(io.StringIO()),
        ):
            status = hook_main(["--capability-shadow-json", "--prompt", "review code"])

        self.assertEqual(status, 0)
        self.assertEqual(retrieve.call_args.kwargs["algorithm"], PRODUCT_PREVIEW_ALGORITHM)

    def test_off_mode_skips_retrieval_without_loading_an_index(self) -> None:
        source = inventory()
        config = {
            "_loaded_from": "/definitely/missing/routes.json",
            "capabilityRetrieval": {"mode": "off", "maxCandidates": 3},
        }

        result = retrieve_capabilities("review a pull request", config, source)

        self.assertEqual(result["mode"], "off")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reasonCodes"], ["retrieval_disabled"])

    def test_non_string_algorithm_degrades_fail_open(self) -> None:
        source = inventory()
        config = {
            "_loaded_from": "/definitely/missing/routes.json",
            "capabilityRetrieval": {
                "mode": "shadow",
                "maxCandidates": 3,
                "algorithm": [],
            },
        }

        result = retrieve_capabilities("review a pull request", config, source)

        self.assertEqual(result["mode"], "off")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reasonCodes"], ["retrieval_algorithm_unsupported"])

    def test_missing_or_invalid_index_degrades_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            config = {
                "_loaded_from": str(root / "routes.json"),
                "capabilityRetrieval": {"mode": "shadow", "maxCandidates": 3},
            }

            missing = retrieve_capabilities("review code", config, source)
            (root / "capability-index.json").write_text("{invalid", encoding="utf-8")
            invalid = retrieve_capabilities("review code", config, source)

        self.assertEqual(missing["status"], "degraded")
        self.assertEqual(missing["candidates"], [])
        self.assertEqual(missing["reasonCodes"], ["capability_index_missing"])
        self.assertEqual(invalid["status"], "degraded")
        self.assertEqual(invalid["candidates"], [])
        self.assertEqual(invalid["reasonCodes"], ["capability_index_unreadable"])

    def test_prompt_over_shared_limit_abstains_before_loading_index(self) -> None:
        source = inventory()
        config = {
            "_loaded_from": "/definitely/missing/routes.json",
            "capabilityRetrieval": {"mode": "shadow", "maxCandidates": 3},
        }

        with mock.patch("lazy_skill_router_retrieval.load_capability_index") as load_index:
            result = retrieve_capabilities("x" * 4097, config, source)

        load_index.assert_not_called()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reasonCodes"], ["prompt_too_long"])
        self.assertEqual(result["algorithm"], PRODUCT_PREVIEW_ALGORITHM)

    def test_stale_index_degrades_without_returning_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            built_from = inventory("sha256:inventory-old")
            config, _ = write_fixture(root, built_from)

            result = retrieve_capabilities(
                "build a security threat model",
                config,
                inventory("sha256:inventory-new"),
            )

        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reasonCodes"], ["capability_index_stale"])
        self.assertEqual(result["candidates"], [])

    def test_max_candidates_is_hard_capped_at_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            config, _ = write_fixture(root, source)

            result = retrieve_capabilities(
                "review code pull request feedback security model pdf minimal implementation",
                config,
                source,
            )

        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual([candidate["rank"] for candidate in result["candidates"]], [1, 2, 3])

    def test_explicit_skill_name_at_end_of_long_prompt_remains_top1(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = inventory()
            config, _ = write_fixture(root, source)
            context = " ".join(f"context{index}" for index in range(300))

            result = retrieve_capabilities(
                f"{context} Use ponytail for the smallest implementation.",
                config,
                source,
            )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "ponytail")

    def test_bilingual_alias_metadata_supports_korean_retrieval_without_runtime_translation(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:bilingual-inventory",
            (
                skill(
                    "security-threat-model",
                    "Build a repository grounded threat model.",
                    aliases=["보안 위협 모델", "위협 모델링"],
                ),
                skill("code-review", "Review source code changes."),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            result = retrieve_capabilities("이 저장소의 보안 위협 모델을 작성해줘", config, source)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "security-threat-model")
        self.assertIn("metadata.alias.word", result["candidates"][0]["evidenceIds"])

    def test_v2_latin_stopword_and_char3_only_overlap_returns_no_match(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:v2-latin-filter-inventory",
            (
                skill("percentage-helper", "What is a percentage calculation?"),
                skill("context-helper", "This is contextual reference material."),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            v1 = retrieve_capabilities(
                "What is 17 percent of 480?",
                config,
                source,
                algorithm=RETRIEVAL_ALGORITHM_V1,
                frozen_replay=True,
            )
            config["capabilityRetrieval"] = {
                "mode": "shadow",
                "maxCandidates": 3,
                "algorithm": RETRIEVAL_ALGORITHM_V2,
            }
            v2 = retrieve_capabilities("What is 17 percent of 480?", config, source)

        self.assertEqual(v1["status"], "matched")
        self.assertEqual(v2["algorithm"], RETRIEVAL_ALGORITHM_V2)
        self.assertEqual(v2["status"], "no-match")
        self.assertEqual(v2["candidates"], [])

    def test_v2_sentence_punctuation_cannot_bypass_stopword_filter(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:v2-punctuated-stopword-inventory",
            (skill("generic-helper", "Do this."),),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            config["capabilityRetrieval"] = {
                "mode": "shadow",
                "maxCandidates": 3,
                "algorithm": RETRIEVAL_ALGORITHM_V2,
            }
            punctuated = retrieve_capabilities("Explain this.", config, source)
            plain = retrieve_capabilities("Explain this", config, source)

        self.assertEqual(punctuated["status"], "no-match")
        self.assertEqual(plain["status"], "no-match")
        self.assertEqual(punctuated["candidates"], plain["candidates"])

    def test_v2_hangul_char3_only_match_remains_retrievable(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:v2-hangul-inventory",
            (
                skill("security-threat-model", "저장소 위협모델링"),
                skill("code-review", "코드 변경 검토"),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            config["capabilityRetrieval"] = {
                "mode": "shadow",
                "maxCandidates": 3,
                "algorithm": RETRIEVAL_ALGORITHM_V2,
            }
            prompt = "저장소의 위협모델을 작성해줘"
            result = retrieve_capabilities(prompt, config, source)
            decomposed = retrieve_capabilities(unicodedata.normalize("NFD", prompt), config, source)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "security-threat-model")
        self.assertIn("metadata.char3", result["candidates"][0]["evidenceIds"])
        self.assertNotIn("metadata.word", result["candidates"][0]["evidenceIds"])
        self.assertEqual(decomposed["status"], "matched")
        self.assertEqual(decomposed["candidates"][0]["skillRef"]["configuredName"], "security-threat-model")

    def test_v2_explicit_configured_name_remains_top1(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:v2-explicit-name-inventory",
            (
                skill("ponytail", "Minimal engineering guardrail."),
                skill("code-review", "Review source changes."),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            config["capabilityRetrieval"] = {
                "mode": "shadow",
                "maxCandidates": 3,
                "algorithm": RETRIEVAL_ALGORITHM_V2,
            }
            result = retrieve_capabilities("Use ponytail.", config, source)

        self.assertEqual(result["candidates"][0]["skillRef"]["configuredName"], "ponytail")
        self.assertIn("configured_name.lexical", result["candidates"][0]["evidenceIds"])

    def test_word_evidence_distinguishes_description_alias_and_capability_sources(self) -> None:
        source = InventorySnapshot(
            "available",
            "sha256:provenance-inventory",
            (
                skill(
                    "code-review",
                    "Review regressions in source changes.",
                    aliases=["코드 변경 검토"],
                    capabilities=["pull request correctness"],
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = write_fixture(Path(temp_dir), source)
            description = retrieve_capabilities("review regressions", config, source)
            alias = retrieve_capabilities("코드 변경 검토", config, source)
            capability = retrieve_capabilities("pull request correctness", config, source)

        self.assertIn("metadata.description.word", description["candidates"][0]["evidenceIds"])
        self.assertIn("metadata.alias.word", alias["candidates"][0]["evidenceIds"])
        self.assertIn("metadata.capability.word", capability["candidates"][0]["evidenceIds"])


if __name__ == "__main__":
    unittest.main()
