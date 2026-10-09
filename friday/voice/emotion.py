"""Backend-neutral emotional intent and delivery direction for speech."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from friday.voice.language import SpeechLanguage
from friday.voice.style import SpeechStyle as ProsodyStyle


class TrueEmotion(str, Enum):
    """Semantic emotion requested by FRIDAY, independent of TTS rendering."""

    NEUTRAL = "neutral"
    HAPPY = "happy"
    AMUSED = "amused"
    EXCITED = "excited"
    ANGRY = "angry"
    SAD = "sad"
    FEAR = "fear"
    CONCERNED = "concerned"
    EMPATHETIC = "empathetic"
    SERIOUS = "serious"
    CALM = "calm"


class VocalEvent(str, Enum):
    """Optional vocal actions, kept separate from emotion and prosody."""

    LAUGH = "laugh"
    SIGH = "sigh"
    GASP = "gasp"
    BREATH = "breath"
    WHISPER = "whisper"
    SHOUT = "shout"


@dataclass(frozen=True)
class DeliverySpecification:
    emotion: TrueEmotion
    instruction: str


@dataclass(frozen=True)
class BackendCapabilities:
    supports_true_emotion: bool = False
    supports_prosody: bool = False
    supports_vocal_events: bool = False
    supports_prompted_style: bool = False


@dataclass(frozen=True)
class EmotionSynthesisMetadata:
    """Capability-resolved request metadata, not proof that audio was generated."""

    requested_emotion: TrueEmotion
    applied_emotion: TrueEmotion | None
    true_emotion_applied: bool
    prosody_applied: ProsodyStyle | None
    backend: str
    fallback_reason: str | None
    vocal_events: tuple[VocalEvent, ...]


class EmotionalTTSBackend(Protocol):
    """Minimal contract for a future backend with verified emotion controls."""

    def supports_emotion(self, emotion: TrueEmotion) -> bool: ...

    def synthesize(
        self,
        *,
        text: str,
        emotion: TrueEmotion,
        voice_profile: str,
        prosody: ProsodyStyle | None,
        vocal_events: tuple[VocalEvent, ...],
    ) -> object: ...


PIPER_CAPABILITIES = BackendCapabilities(
    supports_true_emotion=False,
    supports_prosody=True,
    supports_vocal_events=False,
    supports_prompted_style=False,
)

_DIRECTIONS = {
    TrueEmotion.NEUTRAL: "Speak naturally, with balanced, clear delivery.",
    TrueEmotion.HAPPY: "Speak warmly, genuinely pleased, with brighter delivery.",
    TrueEmotion.AMUSED: "Speak lightly and playfully, with natural amusement.",
    TrueEmotion.EXCITED: "Speak energetically and enthusiastically, animated but intelligible.",
    TrueEmotion.ANGRY: "Speak with restrained anger, firm delivery, and controlled intensity.",
    TrueEmotion.SAD: "Speak softly and subdued, with emotional weight.",
    TrueEmotion.FEAR: "Speak cautiously, with tense, slightly trembling delivery.",
    TrueEmotion.CONCERNED: "Speak carefully, seriously, and supportively.",
    TrueEmotion.EMPATHETIC: "Speak gently, patiently, and reassuringly.",
    TrueEmotion.SERIOUS: "Speak with focus, control, and authority.",
    TrueEmotion.CALM: "Speak steadily, in a relaxed, reassuring manner.",
}

_PIPER_PROSODY = {
    TrueEmotion.NEUTRAL: ProsodyStyle.NEUTRAL,
    TrueEmotion.HAPPY: ProsodyStyle.POSITIVE,
    TrueEmotion.AMUSED: ProsodyStyle.POSITIVE,
    TrueEmotion.EXCITED: ProsodyStyle.EXCITED,
    TrueEmotion.ANGRY: ProsodyStyle.SERIOUS,
    TrueEmotion.SAD: ProsodyStyle.CALM,
    TrueEmotion.FEAR: ProsodyStyle.CONCERNED,
    TrueEmotion.CONCERNED: ProsodyStyle.CONCERNED,
    TrueEmotion.EMPATHETIC: ProsodyStyle.EMPATHETIC,
    TrueEmotion.SERIOUS: ProsodyStyle.SERIOUS,
    TrueEmotion.CALM: ProsodyStyle.CALM,
}

_LANGUAGE_NAMES = {
    SpeechLanguage.ENGLISH: "English",
    SpeechLanguage.URDU: "Urdu",
    SpeechLanguage.ROMAN_URDU: "Roman Urdu",
    SpeechLanguage.MIXED: "mixed Urdu and English",
}
_LANGUAGE_NAMES_BY_VALUE = {language.value: name for language, name in _LANGUAGE_NAMES.items()}


class EmotionDirector:
    """Map semantic intent to stable backend-neutral directions and fallbacks."""

    def direct(self, emotion: TrueEmotion) -> DeliverySpecification:
        requested = TrueEmotion(emotion)
        return DeliverySpecification(requested, _DIRECTIONS[requested])

    def piper_fallback(self, emotion: TrueEmotion) -> ProsodyStyle:
        return _PIPER_PROSODY[TrueEmotion(emotion)]

    def synthesis_metadata(
        self,
        emotion: TrueEmotion,
        *,
        backend: str,
        capabilities: BackendCapabilities | None,
        vocal_events: tuple[VocalEvent, ...] = (),
        supports_emotion: bool | None = None,
    ) -> EmotionSynthesisMetadata:
        requested = TrueEmotion(emotion)
        events = tuple(VocalEvent(event) for event in vocal_events)
        backend_name = str(backend).strip().upper()
        true_emotion_applied = bool(
            backend_name != "PIPER"
            and capabilities
            and capabilities.supports_true_emotion
            and supports_emotion is not False
        )
        prosody = (
            self.piper_fallback(requested)
            if capabilities is not None and capabilities.supports_prosody
            else None
        )
        return EmotionSynthesisMetadata(
            requested_emotion=requested,
            applied_emotion=requested if true_emotion_applied else None,
            true_emotion_applied=true_emotion_applied,
            prosody_applied=prosody,
            backend=backend_name,
            fallback_reason=(
                None if true_emotion_applied
                else "backend_capabilities_unavailable" if capabilities is None
                else "backend_does_not_support_true_emotion"
            ),
            vocal_events=events,
        )


def build_style_instruction(
    emotion: TrueEmotion,
    language: str | SpeechLanguage,
    context: str | None = None,
) -> str:
    """Build a concise delivery direction, separate from the spoken text."""
    instruction = EmotionDirector().direct(emotion).instruction
    label = _LANGUAGE_NAMES.get(language) if isinstance(language, SpeechLanguage) else None
    if label is None:
        label = " ".join(str(language).split())
        label = _LANGUAGE_NAMES_BY_VALUE.get(label.casefold(), label)
    if not label:
        raise ValueError("A speech language is required.")
    if label.casefold() != "english":
        instruction += f" Keep the spoken language as {label}."
    if context and context.strip():
        instruction += f" Context: {context.strip()}"
    return instruction


__all__ = [
    "BackendCapabilities", "DeliverySpecification", "EmotionDirector",
    "EmotionSynthesisMetadata", "EmotionalTTSBackend", "PIPER_CAPABILITIES",
    "ProsodyStyle", "TrueEmotion", "VocalEvent", "build_style_instruction",
]
