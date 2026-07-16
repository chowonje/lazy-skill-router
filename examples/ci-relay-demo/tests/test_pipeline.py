import json
import tempfile
import unittest
from pathlib import Path

from ci_relay.service import process_event


class PipelineTests(unittest.TestCase):
    def test_processes_event_end_to_end(self) -> None:
        payload = {
            "repository": "acme/widget-service",
            "run_id": "run-1042",
            "status": "failed",
            "artifact": {"name": "logs/build.txt", "content": "failed\n"},
        }
        messages: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            result = process_event(payload, Path(directory), messages.append)

            self.assertEqual(result.artifact.read_text(encoding="utf-8"), "failed\n")
            run_record = json.loads(result.run_record.read_text(encoding="utf-8"))
            self.assertEqual(run_record["status"], "failed")
            self.assertNotIn("artifact_content", run_record)

        self.assertEqual(messages, ["acme/widget-service run run-1042: failed"])


if __name__ == "__main__":
    unittest.main()
