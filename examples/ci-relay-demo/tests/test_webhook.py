import unittest

from ci_relay.webhook import parse_event


class WebhookTests(unittest.TestCase):
    def test_parses_valid_event(self) -> None:
        event = parse_event(
            {
                "repository": "acme/widget-service",
                "run_id": "run-1042",
                "status": "failed",
                "artifact": {"name": "logs/build.txt", "content": "failed\n"},
            }
        )

        self.assertEqual(event.run_id, "run-1042")
        self.assertEqual(event.artifact_name, "logs/build.txt")

    def test_rejects_run_id_that_can_become_a_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "run_id"):
            parse_event(
                {
                    "repository": "acme/widget-service",
                    "run_id": "../outside",
                    "status": "failed",
                    "artifact": {"name": "build.txt", "content": "failed\n"},
                }
            )

    def test_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported status"):
            parse_event(
                {
                    "repository": "acme/widget-service",
                    "run_id": "run-1042",
                    "status": "mystery",
                    "artifact": {"name": "build.txt", "content": "failed\n"},
                }
            )


if __name__ == "__main__":
    unittest.main()
