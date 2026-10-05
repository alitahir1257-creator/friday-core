"""Deterministic relevance, provenance signals, and duplicate removal."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit
import re

from friday.research.models import ResearchSource, SearchResult
from friday.research.urls import domain_of, normalize_url


_WORD = re.compile(r"[a-z0-9]{2,}", re.I)


def rank_search_results(
    query: str,
    results: Iterable[SearchResult],
    *,
    limit: int,
    preferred_domains: tuple[str, ...] = (),
    excluded_domains: tuple[str, ...] = (),
    freshness: str = "current",
    fixture_hosts: frozenset[str] = frozenset(),
) -> list[SearchResult]:
    terms = set(_WORD.findall(query.casefold()))
    scored: list[tuple[float, float, int, SearchResult]] = []
    seen: set[str] = set()
    for item in results:
        try:
            url = normalize_url(item.url, fixture_hosts=fixture_hosts)
        except ValueError:
            continue
        host = domain_of(url)
        if any(host == domain or host.endswith("." + domain) for domain in excluded_domains):
            continue
        if url in seen:
            continue
        seen.add(url)
        text = f"{item.title} {item.snippet} {host}".casefold()
        matched = len(terms & set(_WORD.findall(text)))
        relevance = matched / max(1, len(terms))
        preferred = any(host == domain or host.endswith("." + domain) for domain in preferred_domains)
        primary_host = (
            host.endswith((".gov", ".gov.sa", ".edu", ".edu.sa", "python.org", "arxiv.org"))
            or host.startswith(("docs.", "developer."))
            or ".docs." in host
            or host.endswith(".readthedocs.io")
        )
        # Relevance remains primary; transparent origin hints only break close ties.
        score = relevance + (0.2 if preferred else 0.0) + (0.08 if primary_host else 0.0)
        published = _published_timestamp(item.published_at) if freshness in {"current", "recent"} else None
        scored.append((score, published or 0.0, item.rank, SearchResult(
            item.title, url, item.snippet, item.source, item.rank, item.published_at,
            {**dict(item.metadata), "relevance_terms_matched": matched, "preferred_domain": preferred,
             "primary_source_pattern": primary_host},
        )))
    scored.sort(key=lambda pair: (-pair[0], -pair[1], pair[2], pair[3].url))
    return [row for _, _, _, row in scored[:limit]]


def source_signals(source: ResearchSource) -> dict[str, object]:
    host = source.domain.casefold()
    signals: list[str] = []
    if host.endswith((".gov", ".gov.sa", ".edu", ".edu.sa")):
        signals.append("government_or_education_domain")
    if host.startswith("docs.") or ".docs." in host or host.endswith(".readthedocs.io"):
        signals.append("documentation_host_pattern")
    if source.published_at:
        signals.append("publication_date_exposed")
    if source.canonical_url and domain_of(source.canonical_url) == host:
        signals.append("canonical_domain_matches_source")
    if not source.published_at:
        signals.append("publication_date_unknown")
    return {
        "signals": tuple(signals),
        "source_type": classify_source(source.domain),
    }


def classify_source(domain: str) -> str:
    host = domain.casefold()
    if host.endswith((".gov", ".gov.sa", ".edu", ".edu.sa")):
        return "government_or_education"
    if host.startswith("docs.") or ".docs." in host or host.endswith(".readthedocs.io"):
        return "documentation"
    if any(name in host for name in ("arxiv.org", "scholar.google.", "semanticscholar.org")):
        return "research_index"
    return "web"


def is_duplicate(candidate: ResearchSource, existing: Iterable[ResearchSource]) -> bool:
    candidate_url = _safe_canonical(candidate.canonical_url or candidate.final_url)
    candidate_title = _normalized_title(candidate.title)
    candidate_words = set(_WORD.findall(candidate.text.casefold()))
    for item in existing:
        if candidate_url and candidate_url in {
            _safe_canonical(item.canonical_url or item.final_url),
            _safe_canonical(item.final_url),
            _safe_canonical(item.requested_url),
        }:
            return True
        if candidate.content_hash and candidate.content_hash == item.content_hash:
            return True
        title = _normalized_title(item.title)
        if candidate_title and title and SequenceMatcher(None, candidate_title, title).ratio() >= 0.94:
            prior_words = set(_WORD.findall(item.text.casefold()))
            if candidate_words and prior_words and len(candidate_words & prior_words) / len(candidate_words | prior_words) >= 0.86:
                return True
    return False


def _safe_canonical(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port
        scheme = parsed.scheme.casefold()
        default = 443 if scheme == "https" else 80
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != default:
            netloc += f":{port}"
        return urlsplit(value)._replace(scheme=scheme, netloc=netloc, fragment="").geturl()
    except ValueError:
        return ""


def _published_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
            try:
                parsed_date = datetime.strptime(text, pattern).date()
                return datetime.combine(parsed_date, datetime.min.time(), timezone.utc).timestamp()
            except ValueError:
                continue
    return None


def has_recent_publication_date(value: str | None, freshness: str) -> bool:
    timestamp = _published_timestamp(value)
    if timestamp is None:
        return False
    maximum_age_days = {"current": 365, "recent": 1095}.get(freshness)
    if maximum_age_days is None:
        return True
    age = datetime.now(timezone.utc).timestamp() - timestamp
    return 0 <= age <= maximum_age_days * 86400


def _normalized_title(value: str) -> str:
    return " ".join(_WORD.findall(value.casefold()))
