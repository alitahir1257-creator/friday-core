"""Search provider contract with optional SearXNG and browser-backed search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import unescape
from html.parser import HTMLParser
from threading import Lock
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import json
import ipaddress
import os
import re
import time

from friday.research.models import SearchResult
from friday.research.urls import normalize_url


MAX_SEARCH_RESPONSE_BYTES = 512_000
SEARCH_TIMEOUT_SECONDS = 10.0
MAX_SEARCH_RESULTS = 8
BROWSER_SEARCH_ENGINE_ENV = "FRIDAY_RESEARCH_BROWSER_SEARCH_ENGINE"
_BROWSER_ENGINES = {
    "duckduckgo": (
        "https://html.duckduckgo.com/html/",
        ("html.duckduckgo.com", "duckduckgo.com"),
    ),
}
_SEARCH_CHALLENGE = re.compile(
    r"\b(?:captcha|verify (?:that )?you are human|unusual traffic|unusual activity|"
    r"automated requests|security challenge|robot check|access denied)\b",
    re.I,
)
_NO_RESULTS = re.compile(r"\bno results (?:found|for)\b", re.I)
_TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid"})
# ponytail: serialize one browser search; add per-run browser leases if research concurrency needs to grow.
_BROWSER_SEARCH_LOCK = Lock()


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


class BrowserSearchProvider:
    """Search one configured public results page with FRIDAY's installed browser."""

    name = "browser"

    def __init__(self, engine: str = "duckduckgo") -> None:
        self.engine = engine.strip().casefold() if isinstance(engine, str) else ""
        self.configured = self.engine in _BROWSER_ENGINES

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BrowserSearchProvider:
        values = os.environ if environ is None else environ
        return cls(values.get(BROWSER_SEARCH_ENGINE_ENV, "duckduckgo"))

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    ) -> list[SearchResult]:
        if not self.configured:
            raise SearchProviderError("SEARCH_UNAVAILABLE", "The configured browser search engine is unsupported.")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SearchProviderError("INVALID_QUERY", "The public search query is invalid.")
        if isinstance(limit, bool) or not 1 <= limit <= 8:
            raise SearchProviderError("INVALID_LIMIT", "Search result limit must be from 1 to 8.")
        timeout = min(SEARCH_TIMEOUT_SECONDS, max(0.1, timeout_seconds))
        if not _BROWSER_SEARCH_LOCK.acquire(timeout=timeout):
            raise SearchProviderError("SEARCH_TIMEOUT")
        try:
            rows = self._search_page(query.strip(), limit, timeout)
        except SearchProviderError:
            raise
        except TimeoutError:
            raise SearchProviderError("SEARCH_TIMEOUT") from None
        except Exception:
            raise SearchProviderError("SEARCH_UNAVAILABLE") from None
        finally:
            _BROWSER_SEARCH_LOCK.release()
        if not rows:
            raise SearchProviderError("NO_RESULTS")
        return rows[:limit]

    def _search_page(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

        from friday.capabilities.computer.browser import installed_browser
        from friday.research.fetch import ResearchFetcher

        search_url, allowed_hosts = _BROWSER_ENGINES[self.engine]
        timeout = min(SEARCH_TIMEOUT_SECONDS, max(0.1, timeout_seconds))
        timeout_ms = max(1000, int(timeout * 1000))
        fetcher = ResearchFetcher()
        pinned: dict[str, str] = {}
        for host in allowed_hosts:
            addresses = fetcher._resolve_public(host, 443)
            ipv4 = next((address for address in addresses if _is_ipv4(address)), None)
            if ipv4 is None:
                raise SearchProviderError("SEARCH_UNAVAILABLE")
            pinned[host] = ipv4

        executable, _channel = installed_browser()
        url = f"{search_url}?{urlencode({'q': query})}"
        rules = ",".join([*(f"MAP {host} {address}" for host, address in pinned.items()), "EXCLUDE localhost"])
        playwright = sync_playwright().start()
        browser = None
        context = None
        started = time.perf_counter()
        try:
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--no-first-run", "--disable-background-networking", f"--host-resolver-rules={rules}"],
                timeout=timeout_ms,
            )
            context = browser.new_context(
                java_script_enabled=False,
                accept_downloads=False,
                service_workers="block",
                user_agent="FRIDAY-Research/1.0 (+read-only public research)",
            )

            def guard(route) -> None:
                try:
                    candidate = normalize_url(route.request.url)
                    parts = urlsplit(candidate)
                    if (
                        parts.scheme != "https"
                        or parts.hostname not in allowed_hosts
                        or (parts.port is not None and parts.port != 443)
                    ):
                        route.abort()
                        return
                except ValueError:
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                raise SearchProviderError("SEARCH_TIMEOUT") from None
            if response is None:
                raise SearchProviderError("SEARCH_UNAVAILABLE")
            if urlsplit(page.url).hostname not in allowed_hosts:
                raise SearchProviderError("SEARCH_UNAVAILABLE")

            body = page.locator("body").inner_text(timeout=timeout_ms)[:12_000]
            title = page.title()[:500]
            if response.status in {403, 429} or _SEARCH_CHALLENGE.search(f"{title}\n{body}"):
                raise SearchProviderError("SEARCH_CHALLENGE")
            if not 200 <= response.status < 300:
                raise SearchProviderError("SEARCH_UNAVAILABLE")
            rows = page.locator(".result").evaluate_all(
                """(nodes, limit) => {
                    const results = [];
                    for (const node of nodes) {
                        if (!node.getClientRects().length ||
                            node.matches('.result--ad, .result--sponsored, [data-testid="ad"]') ||
                            node.querySelector('.badge--ad, [data-testid="ad-label"], [aria-label="Ad"]')) continue;
                        const link = node.querySelector('a.result__a');
                        if (!link || !link.getClientRects().length) continue;
                        const snippet = node.querySelector('.result__snippet');
                        results.push({
                            title: link.innerText.trim(),
                            url: link.href,
                            snippet: snippet && snippet.getClientRects().length ? snippet.innerText.trim() : ''
                        });
                        if (results.length >= limit) break;
                    }
                    return results;
                }""",
                limit,
            )
            output: list[SearchResult] = []
            seen: set[str] = set()
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                result_url = _normalize_browser_result_url(row.get("url"), self.engine, url)
                result_title = _plain(row.get("title"), 500)
                if not result_url or not result_title or result_url in seen:
                    continue
                seen.add(result_url)
                output.append(SearchResult(
                    title=result_title,
                    url=result_url,
                    snippet=_plain(row.get("snippet"), 1200),
                    source=self.name,
                    rank=len(output) + 1,
                    metadata={"engine": self.engine, "elapsed_ms": elapsed_ms},
                ))
            if output:
                return output
            if _NO_RESULTS.search(body):
                raise SearchProviderError("NO_RESULTS")
            raise SearchProviderError("SEARCH_UNAVAILABLE")
        except PlaywrightTimeoutError:
            raise SearchProviderError("SEARCH_TIMEOUT") from None
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                playwright.stop()
            except Exception:
                pass


def _normalize_browser_result_url(value: object, engine: str, base_url: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = urljoin(base_url, value.strip())
    try:
        parsed = urlsplit(candidate)
        if engine == "duckduckgo" and parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path == "/l/":
            candidate = parse_qs(parsed.query).get("uddg", [""])[0]
        normalized = normalize_url(candidate)
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").casefold()
        if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
            return None
        query = urlencode([
            (name, item) for name, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not name.casefold().startswith("utm_") and name.casefold() not in _TRACKING_PARAMETERS
        ])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except ValueError:
        return None


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def configured_search_providers(environ: Mapping[str, str] | None = None) -> tuple[SearchProvider, ...]:
    values = os.environ if environ is None else environ
    providers: list[SearchProvider] = []
    try:
        provider = SearXNGSearchProvider.from_environment(values)
    except ValueError:
        provider = None
    if provider is not None and provider.configured:
        providers.append(provider)
    providers.append(BrowserSearchProvider.from_environment(values))
    return tuple(providers)
