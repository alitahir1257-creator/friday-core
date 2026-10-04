"""Deterministic route labels and the replaceable router contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Route(str, Enum):
    GENERAL = "GENERAL"
    CODING = "CODING"
    RESEARCH = "RESEARCH"
    COMPUTER = "COMPUTER"
    MEMORY = "MEMORY"


@dataclass(frozen=True)
class RoutingDecision:
    route: Route
    confidence: float
    reason: str
    source: str = "builtin"


class Router(Protocol):
    def route(self, command: str) -> RoutingDecision:
        """Label a request without executing it."""


class BuiltinRouter:
    """Small keyword classifier used until a future router adapter is added."""

    _route_terms = (
        (
            Route.CODING,
            ("code", "coding", "python", "script", "debug", "bug", "function"),
        ),
        (
            Route.RESEARCH,
            (
                "research",
                "sources",
                "investigate",
                "compare",
                "find out",
                "latest information",
                "latest info",
            ),
        ),
        (
            Route.COMPUTER,
            (
                "computer",
                "browser",
                "chrome",
                "click",
                "open app",
                "open browser",
                "open chrome",
                "desktop",
            ),
        ),
        (
            Route.MEMORY,
            ("remember", "yaad rakh", "recall", "what did i say"),
        ),
    )

    def route(self, command: str) -> RoutingDecision:
        normalized = command.casefold()
        for route, terms in self._route_terms:
            for term in terms:
                if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized):
                    return RoutingDecision(
                        route=route,
                        confidence=0.9,
                        reason=f"matched '{term}'",
                    )
        return RoutingDecision(
            route=Route.GENERAL,
            confidence=0.6,
            reason="no specialized route keyword matched",
        )


def create_router() -> Router:
    """Return the configured adapter; future routers can replace this factory."""
    return BuiltinRouter()
