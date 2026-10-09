"""Bounded speech presentation selected locally after policy and content decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from friday.voice.language import SpeechLanguage


class SpeechStyle(str, Enum):
    NEUTRAL = "neutral"
    CALM = "calm"
    CONFIDENT = "confident"
    POSITIVE = "positive"
    EXCITED = "excited"
    SERIOUS = "serious"
    CONCERNED = "concerned"
    EMPATHETIC = "empathetic"
    URGENT = "urgent"


@dataclass(frozen=True)
class StyleParameters:
    pace: float
    intensity: float
    sentence_silence: float
    instruction: str


@dataclass(frozen=True)
class SpeechRenderRequest:
    text: str
    language: SpeechLanguage
    voice_profile: str
    style: SpeechStyle
    pace: float
    intensity: float
    sentence_silence: float
    instruction: str
    quality_preference: str
    speech_text: str


_PARAMETERS = {
    SpeechStyle.NEUTRAL: StyleParameters(1.0, 0.4, 0.04, "professional, natural, controlled"),
    SpeechStyle.CALM: StyleParameters(0.94, 0.25, 0.08, "calm, measured, gentle"),
    SpeechStyle.CONFIDENT: StyleParameters(1.015, 0.55, 0.035, "clear, confident, natural"),
    SpeechStyle.POSITIVE: StyleParameters(1.035, 0.65, 0.045, "pleased, warm, slightly energetic"),
    SpeechStyle.EXCITED: StyleParameters(1.08, 0.75, 0.02, "pleasantly energetic, clear, natural"),
    SpeechStyle.SERIOUS: StyleParameters(0.92, 0.45, 0.10, "serious, controlled, slightly slower"),
    SpeechStyle.CONCERNED: StyleParameters(0.90, 0.4, 0.12, "concerned, gentle, careful"),
    SpeechStyle.EMPATHETIC: StyleParameters(0.96, 0.45, 0.14, "warm, reassuring, soft"),
    SpeechStyle.URGENT: StyleParameters(1.10, 0.65, 0.01, "urgent, focused, faster, clear"),
}
_OVERRIDES = (
    (SpeechStyle.NEUTRAL, re.compile(r"\bneutral(?:ly)?\b", re.I)),
    (SpeechStyle.CALM, re.compile(r"\b(calm(?:ly)?|thora\s+calmly)\b", re.I)),
    (SpeechStyle.POSITIVE, re.compile(r"\bpositive\b", re.I)),
    (SpeechStyle.EXCITED, re.compile(r"\b(excited|exciting)\b", re.I)),
    (SpeechStyle.CONFIDENT, re.compile(r"\bconfident(?:ly)?\b", re.I)),
    (SpeechStyle.SERIOUS, re.compile(r"\b(serious|seriously)\b", re.I)),
    (SpeechStyle.CONCERNED, re.compile(r"\bconcerned\b", re.I)),
    (SpeechStyle.EMPATHETIC, re.compile(r"\bempathetic(?:ally)?\b", re.I)),
    (SpeechStyle.URGENT, re.compile(r"\burgent(?:ly)?\b", re.I)),
)


class SpeechStyleEngine:
    """Maps content to one fixed style set; it cannot affect policy or facts."""

    def select(self, response_text: str, *, user_text: str = "", override: SpeechStyle | None = None) -> SpeechStyle:
        for style, pattern in _OVERRIDES:
            if pattern.search(user_text):
                return style
        if override is not None:
            return override
        text = f"{user_text} {response_text}".casefold()
        if any(word in text for word in ("urgent", "immediately", "right away", "فوری")):
            return SpeechStyle.URGENT
        if any(word in text for word in ("failed", "failure", "error", "errors")):
            return SpeechStyle.SERIOUS
        if any(word in text for word in ("problem", "issue", "risk")):
            return SpeechStyle.CONCERNED
        if any(word in text for word in ("warning", "caution", "approval", "cannot", "must")):
            return SpeechStyle.SERIOUS
        if any(word in text for word in ("sorry", "i understand", "i'm here", "i am here")):
            return SpeechStyle.EMPATHETIC
        if any(word in text for word in ("successful", "complete", "completed", "done")):
            return SpeechStyle.POSITIVE
        return SpeechStyle.NEUTRAL

    def request(self, text: str, *, language: SpeechLanguage, voice_profile: str,
                quality_preference: str, user_text: str = "",
                override: SpeechStyle | None = None) -> SpeechRenderRequest:
        style = self.select(text, user_text=user_text, override=override)
        parameters = _PARAMETERS[style]
        from friday.voice.language import speech_text_for
        return SpeechRenderRequest(
            text=str(text), language=language, voice_profile=voice_profile, style=style,
            pace=parameters.pace, intensity=parameters.intensity,
            sentence_silence=parameters.sentence_silence, instruction=parameters.instruction,
            quality_preference=quality_preference, speech_text=speech_text_for(text, language=language),
        )


__all__ = ["SpeechRenderRequest", "SpeechStyle", "SpeechStyleEngine", "StyleParameters"]
