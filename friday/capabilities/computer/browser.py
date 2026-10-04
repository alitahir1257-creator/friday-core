"""Playwright adapter using an installed Chrome or Edge executable only."""

from __future__ import annotations

import ipaddress
import os
import re
import winreg
from pathlib import Path
from urllib.parse import urlsplit


class BrowserControlError(ValueError):
    pass


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserControlError("Only normal HTTP/HTTPS websites can be opened.")
    if parsed.username or parsed.password or any(ord(char) < 32 for char in value):
        raise BrowserControlError("The website address contains unsafe characters or credentials.")
    try:
        host = parsed.hostname.casefold().rstrip(".").encode("idna").decode("ascii")
        _ = parsed.port
    except (UnicodeError, ValueError):
        raise BrowserControlError("The website address is invalid.") from None
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise BrowserControlError("Local computer addresses are blocked.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise BrowserControlError("The website host is invalid.") from None
    else:
        if not address.is_global:
            raise BrowserControlError("Local and private network addresses are blocked.")
    return value


def installed_browser() -> tuple[str, str]:
    for name, channel in (("chrome.exe", "chrome"), ("msedge.exe", "msedge")):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as key:
                    path = winreg.QueryValue(key, None).strip('"')
                if Path(path).is_file():
                    return path, channel
            except OSError:
                continue
        for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""), os.environ.get("LOCALAPPDATA", "")):
            if root:
                relative = "Google/Chrome/Application/chrome.exe" if channel == "chrome" else "Microsoft/Edge/Application/msedge.exe"
                path = Path(root) / relative
                if path.is_file():
                    return str(path), channel
    raise BrowserControlError("Chrome or Edge is not installed or could not be located.")


class BrowserController:
    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._page = None
        self.backend = "Playwright with installed Chrome/Edge"

    def _ensure(self) -> None:
        if self._context is not None:
            return
        executable, _ = installed_browser()
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            profile = Path(__file__).resolve().parents[3] / ".browser-profile"
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile), executable_path=executable, headless=False,
                args=["--no-first-run"], timeout=30000,
            )
            self._context.route("**/*", self._guard_navigation)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise

    @staticmethod
    def _guard_navigation(route) -> None:
        if route.request.is_navigation_request():
            try:
                validate_url(route.request.url)
            except BrowserControlError:
                route.abort()
                return
        route.continue_()

    def _current(self):
        self._ensure()
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
        return self._page

    def navigate(self, url: str) -> str:
        page = self._current()
        page.goto(validate_url(url), wait_until="domcontentloaded", timeout=30000)
        return f"Opened {page.url}."

    def new_tab(self) -> str:
        self._ensure()
        self._page = self._context.new_page()
        return "Opened a new tab."

    def history(self, operation: str) -> str:
        page = self._current()
        if operation == "BACK":
            page.go_back(wait_until="domcontentloaded", timeout=15000)
        elif operation == "FORWARD":
            page.go_forward(wait_until="domcontentloaded", timeout=15000)
        elif operation == "REFRESH":
            page.reload(wait_until="domcontentloaded", timeout=15000)
        else:
            raise BrowserControlError("Unsupported browser history action.")
        return f"Browser page: {page.url}."

    def inspect_page(self, limit: int = 80) -> list[dict[str, str]]:
        page = self._current()
        return page.locator("a, button, input, textarea, select").evaluate_all(
            "(nodes, limit) => nodes.filter(n => n.getClientRects().length).slice(0, limit).map(n => ({role: n.getAttribute('role') || n.tagName.toLowerCase(), name: n.getAttribute('aria-label') || n.innerText || n.getAttribute('placeholder') || '', type: n.getAttribute('type') || ''}))",
            limit,
        )

    def visible_text(self, max_chars: int = 10000) -> str:
        return self._current().locator("body").inner_text(timeout=10000)[:max_chars]

    def _unique(self, role: str, name: str):
        if not role or not name:
            raise BrowserControlError("A role and exact accessible name are required.")
        locator = self._current().get_by_role(role, name=name, exact=True)
        if locator.count() != 1:
            raise BrowserControlError("I can't reliably identify that page control yet.")
        return locator

    def click_element(self, role: str, name: str) -> str:
        self._unique(role, name).click(timeout=10000)
        return f"Clicked {name}."

    def type_element(self, role: str, name: str, value: str) -> str:
        locator = self._unique(role, name)
        if locator.get_attribute("type") in {"password", "hidden"}:
            raise BrowserControlError("I won't enter text in a password or hidden field.")
        locator.fill(value, timeout=10000)
        return f"Entered text in {name}."

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
