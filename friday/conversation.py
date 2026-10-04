"""Bounded in-memory conversation history."""

from __future__ import annotations


class ConversationHistory:
    def __init__(self, max_turns: int = 6) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least one.")
        self._max_messages = max_turns * 2
        self._messages: list[dict[str, str]] = []

    def messages(self) -> list[dict[str, str]]:
        """Return a copy of the recent user/assistant messages."""
        return [message.copy() for message in self._messages]

    def add_exchange(self, user_message: str, assistant_message: str) -> None:
        self._messages.extend(
            (
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            )
        )
        self._messages = self._messages[-self._max_messages :]
