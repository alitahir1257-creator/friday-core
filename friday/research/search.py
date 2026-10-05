"""Search provider contract and an opt-in SearXNG JSON adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import unescape
from html.parser import HTMLParser
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import json
import os
import re
import time

from friday.research.models import SearchResult


MAX_SEARCH_RESPONSE_BYTES = 512_000
SEARCH_TIMEOUT_SECONDS = 10.0


class SearchProvider(Protocol):
    name: str
    configured: bool

    def search(self, query: str, limit: int = 8, *, timeout_seconds: float = SEARCH_TIMEOUT_SECONDS) -> list[SearchResult]: ...


class SearchProviderError(RuntimeError):
    def __init__(self, code: str, message: str = "The search provider could not return results.") -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextOnly()
    try:
        parser.feed(value[:limit * 3])
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = value
    return re.sub(r"\s+", " ", unescape(text)).strip()[:limit]


class SearXNGSearchProvider:
    """Uses a user-configured SearXNG instance; never picks a public instance implicitly."""

    name = "searxng"

    def __init__(self, base_url: str = "", *, opener=None) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.configured = bool(self.base_url)
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> SearXNGSearchProvider:
        values = os.environ if environ is None else environ
        return cls(values.get("FRIDAY_RESEARCH_SEARXNG_URL", ""))

    @staticmethod
    def _validate_base_url(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        if len(value) > 2048 or any(ord(char) < 32 for char in value):
            raise ValueError("The configured search endpoint is invalid.")
        try:
            parsed = urlsplit(value.strip())
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                raise ValueError
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError
            _ = parsed.port
        except ValueError:
            raise ValueError("The configured search endpoint must be a plain HTTP/HTTPS base URL.") from None
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, "", ""))

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    ) -> list[SearchResult]:
        if not self.configured:
            raise SearchProviderError("SEARCH_CONFIGURATION_REQUIRED", "Set FRIDAY_RESEARCH_SEARXNG_URL to use search.")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SearchProviderError("INVALID_QUERY", "The public search query is invalid.")
        if isinstance(limit, bool) or not 1 <= limit <= 8:
            raise SearchProviderError("INVALID_LIMIT", "Search result limit must be from 1 to 8.")
        params = urlencode({"q": query, "format": "json", "language": "auto", "safesearch": 1})
        endpoint = f"{self.base_url}/search"
        body = params.encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={
                "User-Agent": "FRIDAY-Research/1.0 (+read-only public research)",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener.open(request, timeout=min(SEARCH_TIMEOUT_SECONDS, max(0.1, timeout_seconds))) as response:
                if response.status != 200:
                    raise SearchProviderError("SEARCH_HTTP_ERROR")
                if "application/json" not in response.headers.get("Content-Type", "").casefold():
                    raise SearchProviderError("SEARCH_INVALID_RESPONSE")
                raw = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
            if len(raw) > MAX_SEARCH_RESPONSE_BYTES:
                raise SearchProviderError("SEARCH_RESPONSE_TOO_LARGE")
            payload = json.loads(raw.decode("utf-8"))
        except SearchProviderError:
            raise
        except HTTPError as error:
            code = "SEARCH_HTTP_ERROR" if error.code < 500 else "SEARCH_PROVIDER_ERROR"
            raise SearchProviderError(code) from None
        except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
            raise SearchProviderError("SEARCH_PROVIDER_ERROR") from None
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SearchProviderError("SEARCH_INVALID_RESPONSE")
        output: list[SearchResult] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            title = _plain(row.get("title"), 500)
            snippet = _plain(row.get("content", row.get("snippet", "")), 1200)
            if not isinstance(url, str) or not url.startswith(("http://", "https://")) or not title:
                continue
            engines = row.get("engines", [])
            output.append(SearchResult(
                title=title,
                url=url[:4096],
                snippet=snippet,
                source=self.name,
                rank=len(output) + 1,
                published_at=row.get("publishedDate") if isinstance(row.get("publishedDate"), str) else None,
                metadata={
                    "engines": tuple(str(item)[:100] for item in engines[:8]) if isinstance(engines, Sequence) and not isinstance(engines, (str, bytes)) else (),
                    "elapsed_ms": elapsed_ms,
                },
            ))
        return output


def configured_search_providers(environ: Mapping[str, str] | None = None) -> tuple[SearchProvider, ...]:
    try:
        provider = SearXNGSearchProvider.from_environment(environ)
    except ValueError:
        # Invalid optional research configuration must not prevent FRIDAY from starting.
        return ()
    return (provider,) if provider.configured else ()
