"""Deterministic bounded query planning and public-query privacy filtering."""

from __future__ import annotations

from collections.abc import Sequence
import re

from friday.research.models import ResearchPlan


_ACTION = re.compile(
    r"\b(?:research|look\s+up|search(?:\s+for)?|find|compare|investigate|check|"
    r"tell\s+me\s+about|what(?:\s+is|'s)|who\s+is|where\s+is|when\s+is)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"\b(?:compare|comparison|versus|vs|differences between)\b", re.IGNORECASE)
_TRAILING_PRIVATE_CONTEXT = re.compile(
    r"\b(?:using|based\s+on|given|according\s+to|while\s+considering|"
    r"in\s+my|in\s+our|for\s+my|for\s+our|from\s+my|from\s+our|with\s+my|"
    r"with\s+our|see\s+attached)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_CONTEXT = re.compile(
    r"\b(?:my|our|attached|confidential|sensitive|private\s+(?:contract|file|document|notes?|data)|"
    r"password|passcode|api[ _-]?key|access\s+token|credentials?|secret|bank\s+statement|"
    r"tax\s+return|national\s+id|ssn|account\s+number|personal\s+data)\b",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(r"\b(?:password|passcode|api[ _-]?key|token|secret)\s*[:=]\s*\S+", re.I)
_PERSONAL_IDENTIFIER = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)\+?\d[\d() .-]{7,}\d(?!\d)",
    re.I,
)
_URL = re.compile(r"https?://\S+", re.I)
_GENERIC_START = re.compile(r"^(?:please\s+)?(?:research|look\s+up|search(?:\s+for)?|find|compare|investigate|check|tell\s+me\s+about|what(?:\s+is|'s)|who\s+is|where\s+is|when\s+is)\s+", re.I)


class ResearchQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResearchPlanner:
    def plan(
        self,
        question: str,
        *,
        privacy: str = "public",
        freshness: str = "current",
        max_sources: int = 8,
        timeout_budget_seconds: float = 45.0,
        preferred_domains: Sequence[str] = (),
        excluded_domains: Sequence[str] = (),
        persist: bool = False,
    ) -> ResearchPlan:
        query = self.public_query(question, privacy=privacy)
        if query is None:
            raise ResearchQueryError("PRIVATE_QUERY_BLOCKED", "No safe public search query could be separated from the private request.")
        clean = self._minimal(query)
        if not clean:
            raise ResearchQueryError("EMPTY_QUERY", "No usable public search query remains after sanitization.")
        # Query planning is deliberately deterministic and starts with one broad query.
        return ResearchPlan(
            question=clean,
            search_queries=(clean,),
            max_sources=max_sources,
            timeout_budget_seconds=timeout_budget_seconds,
            freshness=freshness,
            preferred_domains=tuple(preferred_domains),
            excluded_domains=tuple(excluded_domains),
            minimum_independent_sources=2 if _COMPARISON.search(question) else 1,
            privacy=privacy,
            persist=persist,
            sanitized=clean.casefold() != self._minimal(question).casefold(),
        )

    @staticmethod
    def public_query(question: str, *, privacy: str = "public") -> str | None:
        """Return a public-only topic or None; caller history and local sources are never read."""
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ResearchQueryError("INVALID_QUERY", "Research question must contain 1 to 2000 characters.")
        if privacy not in {"public", "private", "local_only"}:
            raise ResearchQueryError("INVALID_PRIVACY", "Privacy must be public, private, or local_only.")
        if privacy == "local_only":
            return None
        text = question.strip()
        action = _ACTION.search(text)
        if action:
            text = text[action.end():]
        # Context before the research verb is omitted; private context after it is cut off.
        text = _TRAILING_PRIVATE_CONTEXT.sub("", text)
        sensitive = _SENSITIVE_CONTEXT.search(text)
        if sensitive:
            if privacy == "private":
                return None
            text = text[:sensitive.start()]
        personal = _PERSONAL_IDENTIFIER.search(text)
        if personal:
            if privacy == "private":
                return None
            text = text[:personal.start()]
        text = _URL.sub(" ", text)
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" \t,;:.!?()[]{}\"'")
        if _SECRET_ASSIGNMENT.search(text):
            return None
        if privacy == "private" and _SENSITIVE_CONTEXT.search(text):
            return None
        if not text or len(text) > 500:
            return None
        return text

    @staticmethod
    def _minimal(query: str) -> str:
        text = _GENERIC_START.sub("", query.strip())
        text = re.sub(r"\b(?:please|for me|about this|on this topic)\b", " ", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" \t,;:.!?()[]{}\"'")
        return text[:500]
