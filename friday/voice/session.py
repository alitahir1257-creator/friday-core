"""Deterministic session phrase handling and conservative self-voice checks."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from friday.voice.models import ConversationSession, VoiceMode, VoiceState


def normalize_transcript(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text).replace("\u2019", "'")).casefold().strip()
    words = []
    current = []
    for char in normalized:
        if char.isalnum() or char == "'":
            current.append(char)
        elif current:
            words.append("".join(current))
            current.clear()
    if current:
        words.append("".join(current))
    return " ".join(words)


def is_end_phrase(text: str, assistant_name: str = "Friday") -> bool:
    normalized = normalize_transcript(text)
    phrases = {
        "stop listening",
        "that's all",
        "you can stop",
        normalize_transcript(f"goodbye {assistant_name}"),
    }
    return normalized in phrases


def strip_wake_prefix(text: str, wake_phrase: str) -> str:
    """Remove only a leading wake phrase when activation contains a command."""
    value = str(text).strip()
    phrase = re.escape(" ".join(wake_phrase.split()))
    match = re.match(rf"^\s*{phrase}(?=$|[\s,.:;!?-])\s*[,.:;!?-]*\s*", value, re.IGNORECASE)
    return value[match.end():].strip() if match else value


def is_tts_echo(transcript: str, current_tts_text: str) -> bool:
    """Suppress long, near-exact playback echoes; keep short interruptions."""
    candidate = normalize_transcript(transcript)
    reference = normalize_transcript(current_tts_text)
    if len(candidate) < 12 or len(reference) < 12:
        return False
    if candidate == reference:
        return True
    return SequenceMatcher(a=candidate, b=reference, autojunk=False).ratio() >= 0.97


class SessionState:
    """Own the active, bounded session and explicit state transitions."""

    def __init__(self) -> None:
        self.current: ConversationSession | None = None
        self.idle_state = VoiceState.IDLE

    @property
    def state(self) -> VoiceState:
        return self.current.state if self.current is not None else self.idle_state

    def begin(self, mode: VoiceMode, timeout_seconds: float, privacy: str) -> ConversationSession:
        session = ConversationSession(mode=mode, timeout_seconds=timeout_seconds, privacy=privacy)
        session.state = VoiceState.LISTENING
        self.current = session
        self.idle_state = VoiceState.IDLE
        return session

    def transition(self, state: VoiceState) -> None:
        if self.current is not None:
            self.current.state = state
            self.current.speaking = state == VoiceState.SPEAKING
        else:
            self.idle_state = state

    def end(self) -> ConversationSession | None:
        session, self.current = self.current, None
        self.idle_state = VoiceState.IDLE
        return session


__all__ = [
    "SessionState", "is_end_phrase", "is_tts_echo", "normalize_transcript",
    "strip_wake_prefix",
]
