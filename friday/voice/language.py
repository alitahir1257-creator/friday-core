"""Local, bounded language handling for display and speech text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata


class SpeechLanguage(str, Enum):
    ENGLISH = "english"
    URDU = "urdu"
    ROMAN_URDU = "roman_urdu"
    MIXED = "mixed"


_URDU_RE = re.compile(r"[\u0600-\u06ff]")
_ROMAN_MARKERS = frozenset({
    "abhi", "aur", "batao", "hai", "hain", "ho", "hoon", "karo", "kro", "kholo",
    "mera", "meri", "mujhe", "mujhy", "ne", "nahi", "theek", "tum", "ya", "gai",
})
_TECHNICAL_TERMS = frozenset({
    "api", "chrome", "code", "github", "git", "python", "repo", "vs", "vscode",
    "build", "check", "codex", "commit", "complete", "deployment", "error", "errors",
    "friday", "open", "project", "run", "summary", "test", "tests",
})
_FALLBACK_URDU = {
    "ali": "علی", "aur": "اور", "batao": "بتاؤ", "gai": "گئی", "hai": "ہے",
    "hain": "ہیں", "ho": "ہو", "kro": "کرو", "karo": "کرو", "kholo": "کھولو",
    "ko": "کو", "mera": "میرا", "meri": "میری", "mujhe": "مجھے", "mujhy": "مجھے",
    "ne": "نے", "project": "project", "thora": "تھوڑا", "tum": "تم", "ya": "یا",
    "abhi": "ابھی", "complete": "complete", "successful": "successful",
}


@dataclass(frozen=True)
class LanguageText:
    display_text: str
    speech_text: str
    language: SpeechLanguage


def normalize_urdu(text: str) -> str:
    """Normalize script locally without changing meaning or transliterating English."""
    value = unicodedata.normalize("NFKC", str(text))
    return value.translate(str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ہ"}))


def detect_language(text: str) -> SpeechLanguage:
    value = str(text)
    if _URDU_RE.search(value):
        return SpeechLanguage.MIXED if _contains_latin_words(value) else SpeechLanguage.URDU
    words = {word.casefold() for word in re.findall(r"[A-Za-z]+", value)}
    roman = bool(words & _ROMAN_MARKERS)
    technical = bool(words & _TECHNICAL_TERMS)
    if roman and technical:
        return SpeechLanguage.MIXED
    return SpeechLanguage.ROMAN_URDU if roman else SpeechLanguage.ENGLISH


def detect_mixed_language(text: str) -> bool:
    return detect_language(text) is SpeechLanguage.MIXED


def roman_to_urdu(text: str) -> str:
    """Prefer the optional local urdunlp package, with a small no-download fallback."""
    try:
        from urdunlp import transliterate_to_urdu
        return str(transliterate_to_urdu(str(text), keep_english=True))
    except ImportError:
        def replace(match: re.Match[str]) -> str:
            word = match.group(0)
            return _FALLBACK_URDU.get(word.casefold(), word)
        return re.sub(r"[A-Za-z]+", replace, str(text))


def urdu_to_roman(text: str) -> str:
    try:
        from urdunlp import transliterate_to_roman
        return str(transliterate_to_roman(normalize_urdu(text)))
    except ImportError:
        return normalize_urdu(text)


def normalize_for_brain(text: str, *, preferred_language: str = "auto") -> LanguageText:
    language = detect_language(text)
    display = normalize_urdu(text) if language in {SpeechLanguage.URDU, SpeechLanguage.MIXED} else str(text).strip()
    if preferred_language == "roman_urdu" and language is SpeechLanguage.URDU:
        display = urdu_to_roman(display)
    return LanguageText(display, speech_text_for(display, language=language), language)


def speech_text_for(text: str, *, language: SpeechLanguage | None = None) -> str:
    detected = language or detect_language(text)
    if detected in {SpeechLanguage.ROMAN_URDU, SpeechLanguage.MIXED}:
        return roman_to_urdu(text)
    return normalize_urdu(text) if detected is SpeechLanguage.URDU else str(text).strip()


def mixed_speech_segments(text: str) -> tuple[tuple[SpeechLanguage, str], ...]:
    """Split mixed speech into Urdu and protected English technical runs.

    The visible response is never changed.  Roman Urdu is converted only in the
    Urdu runs, leaving product and technical names for the English Piper voice.
    """
    value = normalize_urdu(str(text))
    pieces = re.split(r"(\b[A-Za-z][A-Za-z0-9_-]*\b)", value)
    runs: list[tuple[SpeechLanguage, str]] = []
    pending: list[str] = []

    def add(language: SpeechLanguage, content: str) -> None:
        clean = content.strip()
        if not clean:
            return
        if runs and runs[-1][0] is language:
            runs[-1] = (language, f"{runs[-1][1]} {clean}".strip())
        else:
            runs.append((language, clean))

    for piece in pieces:
        if not piece:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", piece) and piece.casefold() in _TECHNICAL_TERMS:
            if pending:
                add(SpeechLanguage.URDU, roman_to_urdu("".join(pending)))
                pending.clear()
            add(SpeechLanguage.ENGLISH, piece)
        else:
            pending.append(piece)
    if pending:
        add(SpeechLanguage.URDU, roman_to_urdu("".join(pending)))
    return tuple(runs)


def _contains_latin_words(value: str) -> bool:
    return bool(re.search(r"\b[A-Za-z][A-Za-z0-9_-]*\b", value))


__all__ = [
    "LanguageText", "SpeechLanguage", "detect_language", "detect_mixed_language", "normalize_for_brain",
    "mixed_speech_segments", "normalize_urdu", "roman_to_urdu", "speech_text_for", "urdu_to_roman",
]
