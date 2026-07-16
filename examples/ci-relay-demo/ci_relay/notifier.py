"""Dependency-injected local notification boundary."""

from typing import Callable

from .webhook import CiEvent

Sender = Callable[[str], None]


def build_message(event: CiEvent) -> str:
    return f"{event.repository} run {event.run_id}: {event.status}"


def send_notification(message: str, sender: Sender) -> None:
    sender(message)
