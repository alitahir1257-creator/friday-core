"""Small data contracts for source-backed research."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Event
from typing import Any, Mapping
import uuid


MAX_SEARCH_QUERIES = 4
MAX_RESEARCH_SOURCES = 8
MAX_FETCHES_PER_SOURCE = 1
MAX_BROWSER_FALLBACKS = 2


class ResearchStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SEARCH_CONFIGURATION_REQUIRED = "SEARCH_CONFIGURATION_REQUIRED"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SEARCH_CHALLENGE = "SEARCH_CHALLENGE"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
    NO_RESULTS = "NO_RESULTS"


class ClaimKind(str, Enum):
    SOURCE_DERIVED_FACT = "SOURCE_DERIVED_FACT"
    MODEL_SYNTHESIS = "MODEL_SYNTHESIS"
    UNSUPPORTED_UNVERIFIED = "UNSUPPORTED_UNVERIFIED"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    rank: int
    published_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchPlan:
    question: str
    search_queries: tuple[str, ...]
    max_search_queries: int = MAX_SEARCH_QUERIES
    max_sources: int = MAX_RESEARCH_SOURCES
    max_fetches_per_source: int = MAX_FETCHES_PER_SOURCE
    max_browser_fallbacks: int = MAX_BROWSER_FALLBACKS
    timeout_budget_seconds: float = 45.0
    freshness: str = "current"
    preferred_domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    minimum_independent_sources: int = 1
    privacy: str = "public"
    persist: bool = False
    sanitized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip() or len(self.question) > 2000:
            raise ValueError("Research question must contain 1 to 2000 characters.")
        minimum_queries = 0 if self.privacy in {"private", "local_only"} else 1
        if not minimum_queries <= len(self.search_queries) <= MAX_SEARCH_QUERIES:
            raise ValueError("Research plans require 1 to 4 search queries, except local-only plans.")
        if (isinstance(self.max_search_queries, bool)
                or not minimum_queries <= self.max_search_queries <= MAX_SEARCH_QUERIES):
            raise ValueError("max_search_queries must be from 1 to 4, except local-only plans.")
        if any(not isinstance(query, str) or not query.strip() or len(query) > 500 for query in self.search_queries):
            raise ValueError("Research search queries must contain 1 to 500 characters.")
        if isinstance(self.max_sources, bool) or not 1 <= self.max_sources <= MAX_RESEARCH_SOURCES:
            raise ValueError("max_sources must be from 1 to 8.")
        if isinstance(self.minimum_independent_sources, bool) or self.minimum_independent_sources not in {1, 2}:
            raise ValueError("minimum_independent_sources must be 1 or 2.")
        if self.max_fetches_per_source != MAX_FETCHES_PER_SOURCE:
            raise ValueError("Each selected source can be fetched at most once.")
        if not 0 <= self.max_browser_fallbacks <= MAX_BROWSER_FALLBACKS:
            raise ValueError("max_browser_fallbacks must be from 0 to 2.")
        if not isinstance(self.timeout_budget_seconds, (int, float)) or not 1 <= self.timeout_budget_seconds <= 180:
            raise ValueError("timeout_budget_seconds must be from 1 to 180.")
        if self.freshness not in {"current", "recent", "timeless"}:
            raise ValueError("freshness must be current, recent, or timeless.")
        if self.privacy not in {"public", "private", "local_only"}:
            raise ValueError("privacy must be public, private, or local_only.")


@dataclass(frozen=True)
class ResearchSource:
    id: str
    requested_url: str
    final_url: str
    canonical_url: str
    title: str
    domain: str
    author: str | None
    published_at: str | None
    retrieved_at: str
    text: str
    headings: tuple[str, ...]
    content_hash: str
    source_type: str
    reliability_metadata: Mapping[str, Any] = field(default_factory=dict)
    citation_id: str = ""
    accessible: bool = True
    error_code: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class ResearchClaim:
    text: str
    kind: ClaimKind
    citation_ids: tuple[str, ...] = ()
    evidence_quotes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchEvent:
    event: str
    run_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    index: int | None = None
    total: int | None = None
    status: str | None = None
    duration_ms: float | None = None


@dataclass
class ResearchRun:
    plan: ResearchPlan
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: ResearchStatus = ResearchStatus.INSUFFICIENT_EVIDENCE
    sources: list[ResearchSource] = field(default_factory=list)
    claims: list[ResearchClaim] = field(default_factory=list)
    events: list[ResearchEvent] = field(default_factory=list)
    answer: str = ""
    error_code: str | None = None
    citations: dict[str, dict[str, str]] = field(default_factory=dict)
    _cancel_event: Event = field(default_factory=Event, repr=False)

    def cancel(self) -> None:
        self._cancel_event.set()

    def cancelled(self) -> bool:
        return self._cancel_event.is_set()


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str | None
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class ExtractedPage:
    final_url: str
    canonical_url: str
    title: str
    headings: tuple[str, ...]
    author: str | None
    published_at: str | None
    text: str
    content_hash: str
