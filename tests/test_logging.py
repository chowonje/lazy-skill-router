from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from lazy_skill_router_logging import ROUTING_OBSERVATION_SCHEMA, log_decision, prompt_hash


@dataclass(frozen=True)
class RouteFixture:
    name: str = "pdf"
    primary: str = "pdf"


@dataclass(frozen=True)
class MatchFixture:
    route: RouteFixture = RouteFixture()
    confidence: float = 0.65
    score: float = 0.65
    matched_signals: tuple[str, ...] = ("PDF signal",)


def read_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class LoggingTest(unittest.TestCase):
    def test_logging_hashes_prompt_and_never_writes_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            prompt = "private prompt PDF"
            log_decision(prompt, MatchFixture(), {"logging": {"enabled": True, "path": str(path)}})
            records = read_records(path)

        self.assertEqual(records[0]["promptHash"], prompt_hash(prompt))
        self.assertNotIn("routingObservation", records[0])
        self.assertNotIn(prompt, json.dumps(records, ensure_ascii=False))

    def test_routing_observation_is_bounded_redacted_and_has_no_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            prompt = "PRIVATE ROUTING OBSERVATION PROMPT"
            log_decision(
                prompt,
                MatchFixture(),
                {"logging": {"enabled": True, "path": str(path)}},
                activation_disposition="propose",
                capability_index_revision="sha256:index",
                capability_candidate_skill_ids=("pdf", "code-review", "ponytail", "ignored"),
                capability_candidate_observations=(
                    {"skillId": "pdf", "evidenceIds": tuple(f"metadata.evidence-{i}" for i in range(10))},
                    {"skillId": "code-review", "evidenceIds": ("metadata.description.word",)},
                    {"skillId": "ponytail", "evidenceIds": ("configured_name.lexical",)},
                    {"skillId": "ignored", "evidenceIds": ("metadata.word",)},
                ),
                capability_retrieval_latency_ms=1.25,
                capability_retrieval_status="matched",
                retrieval_top1="pdf",
            )
            record = read_records(path)[0]

        observation = record["routingObservation"]
        self.assertEqual(record["schema"], "lazy-skill-router.measurement-event/v1")
        self.assertEqual(observation["schema"], ROUTING_OBSERVATION_SCHEMA)
        self.assertEqual(len(observation["retrieval"]["candidates"]), 3)
        self.assertEqual(len(observation["retrieval"]["candidates"][0]["evidenceIds"]), 8)
        self.assertEqual(observation["ownership"]["status"], "unobserved")
        self.assertEqual(observation["activation"]["source"], "legacy-route-plus-activation-ir")
        self.assertEqual(observation["stop"]["action"], "observe-only")
        self.assertFalse(observation["stop"]["affectsLegacySelection"])
        self.assertFalse(observation["semantics"]["automaticPromotion"])
        self.assertNotIn(prompt, json.dumps(record, ensure_ascii=False))

    def test_routing_observation_distinguishes_lexical_no_match_from_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            config = {"logging": {"enabled": True, "path": str(path)}}
            log_decision("no match prompt", None, config, capability_retrieval_status="no-match")
            log_decision("failure prompt", None, config, capability_retrieval_status="degraded")
            log_decision("off failure prompt", None, config, mode="off", capability_retrieval_status="degraded")
            no_match, degraded, off_degraded = read_records(path)

        self.assertEqual(no_match["routingObservation"]["stop"]["action"], "observe-only")
        self.assertEqual(
            no_match["routingObservation"]["stop"]["reasonCode"],
            "lexical_no_match_not_semantic_abstain",
        )
        self.assertFalse(no_match["routingObservation"]["semantics"]["semanticAbstentionObserved"])
        self.assertEqual(degraded["routingObservation"]["stop"]["action"], "fallback-legacy")
        self.assertFalse(degraded["routingObservation"]["stop"]["affectsLegacySelection"])
        self.assertEqual(off_degraded["routingObservation"]["stop"]["action"], "stop-shadow")
        self.assertEqual(off_degraded["routingObservation"]["activation"]["source"], "unobserved")

    def test_logging_keeps_only_configured_max_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            config = {"logging": {"enabled": True, "path": str(path), "maxEntries": 2, "retentionDays": 30}}
            for prompt in ("one", "two", "three"):
                log_decision(prompt, MatchFixture(), config)
            records = read_records(path)

        self.assertEqual(len(records), 2)
        self.assertEqual([record["promptHash"] for record in records], [prompt_hash("two"), prompt_hash("three")])

    def test_logging_drops_records_older_than_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            path.write_text(
                json.dumps({"time": "2000-01-01T00:00:00+00:00", "promptHash": "old"}) + "\n",
                encoding="utf-8",
            )
            config = {"logging": {"enabled": True, "path": str(path), "maxEntries": 10, "retentionDays": 1}}
            log_decision("new", MatchFixture(), config)
            records = read_records(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["promptHash"], prompt_hash("new"))

    def test_logging_preserves_unknown_event_schema_on_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "router.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "schema": "lazy-skill-router.measurement-event/v999",
                        "eventType": "decision",
                        "time": "2999-01-01T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {"logging": {"enabled": True, "path": str(path), "maxEntries": 10, "retentionDays": 30}}
            log_decision("new", MatchFixture(), config)
            records = read_records(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["schema"], "lazy-skill-router.measurement-event/v999")
        self.assertEqual(records[1]["promptHash"], prompt_hash("new"))


if __name__ == "__main__":
    unittest.main()
