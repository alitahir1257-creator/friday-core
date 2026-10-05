"""One-shot isolated Playwright fallback for static pages with no readable text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit
import hashlib
import ipaddress
import re

from friday.capabilities.computer.browser import installed_browser
from friday.research.fetch import ResearchFetcher
from friday.research.models import ExtractedPage
from friday.research.urls import normalize_url


class ResearchBrowserRenderer(Protocol):
    def render(self, url: str, *, timeout_seconds: float, max_text_chars: int) -> ExtractedPage: ...


class PlaywrightResearchRenderer:
    """Uses the installed browser with an ephemeral context and a pinned public host."""

    def __init__(self, resolver=None) -> None:
        self._fetcher = ResearchFetcher(resolver=resolver) if resolver else ResearchFetcher()

    def render(self, url: str, *, timeout_seconds: float = 10.0, max_text_chars: int = 100_000) -> ExtractedPage:
        safe_url = normalize_url(url)
        parsed = urlsplit(safe_url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = self._fetcher._resolve_public(hostname, port)
        ipv4 = next((item for item in addresses if _is_ipv4(item)), None)
        if ipv4 is None:
            raise RuntimeError("Browser fallback requires a validated public IPv4 address.")
        executable, _channel = installed_browser()
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            rules = f"MAP {hostname} {ipv4},EXCLUDE localhost"
            browser = playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--no-first-run", f"--host-resolver-rules={rules}"],
                timeout=max(1000, int(timeout_seconds * 1000)),
            )
            context = browser.new_context(
                java_script_enabled=True,
                accept_downloads=False,
                service_workers="block",
                user_agent="FRIDAY-Research/1.0 (+read-only public research)",
            )

            def guard(route) -> None:
                candidate = route.request.url
                try:
                    candidate_url = normalize_url(candidate)
                    request_parts = urlsplit(candidate_url)
                    request_port = request_parts.port or (443 if request_parts.scheme == "https" else 80)
                    if (
                        request_parts.hostname != hostname
                        or request_parts.scheme != parsed.scheme
                        or request_port != port
                    ):
                        route.abort()
                        return
                except ValueError:
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            response = page.goto(safe_url, wait_until="domcontentloaded", timeout=max(1000, int(timeout_seconds * 1000)))
            if response is not None and "application/pdf" in response.headers.get("content-type", "").casefold():
                raise RuntimeError("Document parsing is deferred to Phase 7B.")
            final_url = normalize_url(page.url)
            if urlsplit(final_url).hostname != hostname:
                raise RuntimeError("The rendered page redirected outside its validated source host.")
            text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=max(1000, int(timeout_seconds * 1000)))).strip()[:max_text_chars]
            if not text:
                raise RuntimeError("The browser page did not contain readable text.")
            title = page.title()[:500] or final_url
            headings = tuple(item.strip()[:300] for item in page.locator("h1,h2,h3,h4,h5,h6").all_text_contents()[:100] if item.strip())
            metadata: Mapping[str, str] = page.locator("head").evaluate("head => ({canonical: head.querySelector('link[rel=canonical]')?.href || '', author: head.querySelector('meta[name=author]')?.content || '', published: head.querySelector('meta[property=\\\"article:published_time\\\"]')?.content || head.querySelector('meta[name=\\\"datePublished\\\"]')?.content || ''})")
            canonical = normalize_url(metadata.get("canonical", "")) if metadata.get("canonical") else final_url
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return ExtractedPage(
                final_url, canonical, title, headings, metadata.get("author") or None,
                metadata.get("published") or None, text, digest,
            )
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()
            playwright.stop()


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False
