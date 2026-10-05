"""Shared deterministic scoring and privacy gates for retrieval sources."""

import hashlib
import re

from friday.memory.policy import normalize_text

PRIVACY = {"public": 0, "private": 1, "local_only": 2}
TOKEN = re.compile(r"[\w-]+", re.UNICODE)


def read_allowed(privacy: str, privacy_level: str, route_backend: str | None) -> bool:
    if privacy_level not in PRIVACY:
        raise ValueError("Unsupported retrieval privacy.")
    return privacy in PRIVACY and (
        privacy == "public" or (privacy_level != "public" and route_backend == "local")
    )


def relevance(query: str, content: str, title: str = "") -> float:
    terms = set(TOKEN.findall(normalize_text(query)))
    if not terms:
        return 0.0
    text = normalize_text(content + " " + title)
    overlap = len(terms & set(TOKEN.findall(text))) / len(terms)
    phrase = normalize_text(query) in text
    return min(1.0, overlap * 0.9 + float(phrase) * 0.1)


def snippet(content: str, query: str, max_chars: int = 320) -> str:
    if len(content.strip()) <= max_chars:
        return content.strip()
    terms = set(TOKEN.findall(normalize_text(query)))
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    best = max(lines, key=lambda line: len(terms & set(TOKEN.findall(normalize_text(line)))), default="")
    return best[:max_chars]


def fingerprint(content: str) -> str:
    # Exact normalized content only; no semantic similarity or embeddings.
    body = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", content)
    normalized = normalize_text(body) or normalize_text(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
