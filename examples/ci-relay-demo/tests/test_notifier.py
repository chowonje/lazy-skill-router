import unittest

from ci_relay.notifier import send_notification


class NotifierTests(unittest.TestCase):
    def test_sends_message_once(self) -> None:
        messages: list[str] = []

        send_notification("build failed", messages.append)

        self.assertEqual(messages, ["build failed"])

    def test_sender_error_is_visible(self) -> None:
        def fail(_message: str) -> None:
            raise TimeoutError("temporary failure")

        with self.assertRaises(TimeoutError):
            send_notification("build failed", fail)


if __name__ == "__main__":
    unittest.main()
