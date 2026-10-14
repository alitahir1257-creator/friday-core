"""Bounded English and Roman Urdu commands; future planners return ActionPlan."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote_plus, urlsplit

from friday.capabilities.registry import Action, ActionPlan, Capability


_DOMAIN = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#][^\s]*)?$", re.I)
_WEB_NAMES = {"youtube": "youtube.com", "google": "google.com", "github": "github.com"}
_NUMBER = r"\d{1,16}(?:\.\d{1,16})?"


@dataclass(frozen=True)
class CanonicalComputerIntent:
    source: str
    tool_name: str
    arguments: dict[str, str]


class CanonicalComputerIntentParser:
    """Recognize bounded local commands without performing their effects."""

    def parse(self, command: str) -> CanonicalComputerIntent | None:
        text = _normalize_intent_text(command)

        app = re.fullmatch(
            r"(?:please\s+)?(?:open|launch)\s+(?:the\s+)?(?P<app>.+?)"
            r"|(?P<app_suffix>.+?)\s+open",
            text,
        )
        if app:
            target = (app.group("app") or app.group("app_suffix") or "").strip()
            target = re.sub(r"\s+(?:application|app)$", "", target)
            target = {
                "calculator": "calculator", "visual studio code": "VS Code", "vs code": "VS Code",
                "google chrome": "Chrome", "chrome": "Chrome", "notepad": "Notepad",
            }.get(target, target)
            if target and len(target) <= 120:
                return CanonicalComputerIntent(
                    "canonical_app_open", "computer.open_app", {"app": target},
                )

        arithmetic = re.fullmatch(
            rf"(?P<left>{_NUMBER})\s+(?P<operator>plus|add|minus|subtract|multiply(?:\s+by)?|times|"
            rf"divided\s+by|divide(?:\s+by)?)\s+(?P<right>{_NUMBER})"
            rf"(?:\s+(?:calculate|compute|solve))?"
            rf"|(?:calculate|compute|solve)\s+(?P<left_prefix>{_NUMBER})\s+"
            rf"(?P<operator_prefix>plus|add|minus|subtract|multiply(?:\s+by)?|times|"
            rf"divided\s+by|divide(?:\s+by)?)\s+(?P<right_prefix>{_NUMBER})",
            text,
        )
        if arithmetic:
            left = arithmetic.group("left") or arithmetic.group("left_prefix")
            operator = arithmetic.group("operator") or arithmetic.group("operator_prefix")
            right = arithmetic.group("right") or arithmetic.group("right_prefix")
            operator = "plus" if operator in {"plus", "add"} else (
                "minus" if operator in {"minus", "subtract"} else (
                    "multiply" if operator.startswith("multiply") or operator == "times" else "divide"
                )
            )
            return CanonicalComputerIntent(
                "canonical_arithmetic", "computer.calculate",
                {"left": left, "operator": operator, "right": right},
            )

        has_drive = bool(re.search(r"\bc\s*(?:drive\b|:)", text))
        has_disk_space = bool(re.search(r"\b(?:disk|storage)\s+space\b", text))
        has_disk_request = bool(re.search(r"\b(?:check|show|tell|how\s+much|kitni|bachi|baki|left|remaining)\b", text))
        if (has_drive or has_disk_space) and has_disk_request:
            return CanonicalComputerIntent(
                "canonical_disk_space", "computer.disk_free_space", {},
            )

        has_readme = bool(re.search(r"\breadme(?:\.md)?\b", text))
        has_search_request = bool(re.search(r"\b(?:find|search|show|tell|where)\b", text))
        if has_readme and has_search_request:
            return CanonicalComputerIntent(
                "canonical_file_search", "computer.find_project_readme", {},
            )

        has_download = bool(re.search(r"\bdownloads?\b|\bdownloaded\b", text))
        has_recency = bool(re.search(r"\b(?:latest|newest|last|most\s+recent)\b", text))
        has_latest_request = bool(re.search(r"\b(?:show|tell|which|what|find|list)\b", text))
        if has_download and has_recency and has_latest_request:
            return CanonicalComputerIntent(
                "canonical_latest_download", "computer.latest_download", {},
            )

        return None


def _normalize_intent_text(command: str) -> str:
    text = re.sub(r"^\s*friday\b[\s,.:;!?-]*", "", str(command), count=1, flags=re.I).casefold()
    aliases = (
        (r"\bjholo\b", "kholo"),
        (r"\b(?:kholo|chalao)\b", "open"),
        (r"\b(?:dhondo|dhoondo|dhoondho)\b", "find"),
        (r"\bdikhao\b", "show"),
        (r"\bbatao\b", "tell"),
        (r"\b(?:mai|mein|main)\b", "in"),
        (r"\b(?:konsi|kaunsi)\b", "which"),
        (r"\bkaha\b", "where"),
        (r"\bkya\b", "what"),
        (r"\bhai\b", "is"),
    )
    for pattern, replacement in aliases:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\b(check|calculate|compute|solve)\s+(?:kro|karo)\b", r"\1", text)
    text = re.sub(r"[?!,;:]+", " ", text).strip(" .\t\r\n")
    return " ".join(text.split())


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
        notepad_text = re.fullmatch(r"(?:please\s+)?notepad\s+(?:mai|mein)\s+(.+?)\s+likh\s+do", text, re.I)
        if notepad_text:
            return ActionPlan((
                Action(Capability.COMPUTER, "OPEN_APP", "notepad"),
                Action(Capability.INPUT, "TYPE_TEXT", notepad_text.group(1).strip()),
            ))
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
