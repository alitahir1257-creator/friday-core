"""Bounded English and Roman Urdu commands; future planners return ActionPlan."""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit

from friday.capabilities.registry import Action, ActionPlan, Capability


_DOMAIN = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#][^\s]*)?$", re.I)
_WEB_NAMES = {"youtube": "youtube.com", "google": "google.com", "github": "github.com"}


def website_target(value: str) -> str | None:
    value = value.strip().rstrip("/ ")
    if value.casefold().endswith(".exe"):
        return None
    if value.casefold() in _WEB_NAMES:
        value = _WEB_NAMES[value.casefold()]
    explicit_scheme = value.lower().startswith(("http://", "https://"))
    if not explicit_scheme and not _DOMAIN.fullmatch(value):
        return None
    url = value if explicit_scheme else f"https://{value}"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


class DeterministicComputerPlanner:
    def plan(self, request: str) -> ActionPlan | None:
        text = request.strip().rstrip(".?!")
        search = re.fullmatch(r"(?:google\s+par|search\s+google\s+for)\s+(.+?)\s+(?:search\s+karo|search)?", text, re.I)
        if search:
            query = search.group(1).strip()
            if query:
                return ActionPlan((Action(Capability.BROWSER, "NAVIGATE", f"https://www.google.com/search?q={quote_plus(query)}"),))
        rules = (
            (r"(?:open|launch)\s+(.+?)", "OPEN_APP", Capability.COMPUTER),
            (r"(.+?)\s+(?:kholo|chalao|open\s+karo)", "OPEN_APP", Capability.COMPUTER),
            (r"(?:switch\s+to|focus)\s+(.+?)", "FOCUS_APP", Capability.WINDOW),
            (r"(?:maximize|maximise)\s+(.+?)", "MAXIMIZE_WINDOW", Capability.WINDOW),
            (r"(?:minimize|minimise)\s+(.+?)", "MINIMIZE_WINDOW", Capability.WINDOW),
            (r"restore\s+(.+?)", "RESTORE_WINDOW", Capability.WINDOW),
            (r"close\s+(.+?)", "CLOSE_WINDOW", Capability.WINDOW),
        )
        if re.fullmatch(r"(?:new\s+tab|new\s+tab\s+kholo)", text, re.I):
            return ActionPlan((Action(Capability.BROWSER, "NEW_TAB"),))
        if re.fullmatch(r"(?:page\s+refresh\s+karo|refresh(?:\s+page)?)", text, re.I):
            return ActionPlan((Action(Capability.BROWSER, "REFRESH"),))
        for pattern, operation, capability in rules:
            match = re.fullmatch(pattern, text, re.I)
            if match:
                target = match.group(1).strip()
                if operation == "OPEN_APP":
                    url = website_target(target)
                    if url:
                        return ActionPlan((Action(Capability.BROWSER, "NAVIGATE", url),))
                return ActionPlan((Action(capability, operation, target),))
        url = website_target(text)
        if url:
            return ActionPlan((Action(Capability.BROWSER, "NAVIGATE", url),))
        return None
