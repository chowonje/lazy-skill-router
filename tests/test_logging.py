from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from lazy_skill_router_logging import log_decision, prompt_hash


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
        self.assertNotIn(prompt, json.dumps(records, ensure_ascii=False))

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
