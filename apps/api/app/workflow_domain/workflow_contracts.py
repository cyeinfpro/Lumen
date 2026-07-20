"""Small shared contracts for workflow routing and durable dispatch."""

from __future__ import annotations

from typing import Any


class _PublishBundle:
    """Durable assistant-task payload prepared before outbox publication."""

    def __init__(
        self,
        *,
        assistant_msg_id: str,
        message_ids: list[str],
        outbox_payloads: list[dict[str, Any]],
        outbox_rows: list[Any],
    ) -> None:
        self.assistant_msg_id = assistant_msg_id
        self.message_ids = message_ids
        self.outbox_payloads = outbox_payloads
        self.outbox_rows = outbox_rows


__all__ = ["_PublishBundle"]
