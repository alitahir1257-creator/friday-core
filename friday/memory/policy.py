"""Deterministic memory candidate and secret-handling policy."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from friday.memory.models import MemoryKind, MemoryWriteDecision


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bsk-(?:(?:proj|live|test|svcacct)-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?is)-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    re.compile(
        r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*\S+"
    ),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|"
        r"refresh[_ -]?token|client[_ -]?secret|private[_ -]?key|token)\b"
        r"\s*[:=]\s*[\"']?[^\s\"',;]{8,}"
    ),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://"
        r"[^:/\s]+:[^@\s]+@"
    ),
)

_SMALLTALK = re.compile(
    r"^\s*(?:hi|hello|hey|salam|thanks|thank you|good morning|good afternoon|"
    r"good evening)[!. ,?]*\s*$",
    re.IGNORECASE,
)
_SPECULATION = re.compile(
    r"\b(?:maybe|might|probably|possibly|i think|i guess|it seems|uncertain)\b",
    re.IGNORECASE,
)
_DURABLE_CUE = re.compile(
    r"\b(?:remember|save this|save that|don't forget|we decided|decision|prefer|"
    r"preference|always|never|project|architecture|constraint|checkpoint|uses|"
    r"branch|policy|completed|finished)\b",
    re.IGNORECASE,
)


def contains_possible_secret(value: Any) -> bool:
    """Conservatively detect credentials without returning or logging their values."""
    if isinstance(value, str):
        return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)
    if isinstance(value, Mapping):
        secret_key = re.compile(
            r"(?i)(?:^|[_-])(?:api[_-]?key|password|passwd|secret|access[_-]?token|"
            r"refresh[_-]?token|authorization|cookie|private[_-]?key|client[_-]?secret|token)(?:$|[_-])"
        )
        return any(
            (isinstance(key, str) and secret_key.search(key) is not None)
            or contains_possible_secret(key)
            or contains_possible_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return any(contains_possible_secret(item) for item in value)
    return False


def normalize_text(value: str) -> str:
    """Normalize whitespace and Unicode for deterministic deduplication/search."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def normalize_namespace(namespace: str | None = None) -> str:
    if namespace is None:
        return "global"
    if not isinstance(namespace, str):
        raise ValueError("namespace must be text.")
    normalized = unicodedata.normalize("NFKC", namespace).strip().casefold()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("namespace must contain 1 to 128 printable characters.")
    return normalized


def bounded_score(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number from 0.0 to 1.0.")
    try:
        score = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a number from 0.0 to 1.0.") from error
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError(f"{field_name} must be a number from 0.0 to 1.0.")
    return score


def validate_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a JSON object.")
    copied = dict(metadata)
    try:
        json.dumps(copied, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be a JSON object.") from error
    return copied


class MemoryWritePolicy:
    """Rule-based gate; it never asks a model what should be remembered."""

    def evaluate(
        self,
        content: str,
        *,
        kind: MemoryKind,
        source: str,
        namespace: str,
        confidence: float,
        metadata: Mapping[str, Any],
        explicit: bool,
    ) -> MemoryWriteDecision:
        if contains_possible_secret((content, source, namespace, metadata)):
            return MemoryWriteDecision(False, "possible_secret")
        if not normalize_text(content):
            return MemoryWriteDecision(False, "empty_content")
        if _SMALLTALK.fullmatch(content):
            return MemoryWriteDecision(False, "transient_smalltalk")
        if _SPECULATION.search(content):
            return MemoryWriteDecision(False, "speculative_claim")
        if not explicit and not _DURABLE_CUE.search(content):
            return MemoryWriteDecision(False, "not_durable_candidate")
        if not explicit and confidence < 0.6:
            return MemoryWriteDecision(False, "low_confidence_candidate")
        if kind == MemoryKind.EPISODE and not explicit and not re.search(
            r"\b(?:completed|finished|checkpoint|milestone)\b", content, re.IGNORECASE
        ):
            return MemoryWriteDecision(False, "not_meaningful_episode")
        return MemoryWriteDecision(True, "accepted")
