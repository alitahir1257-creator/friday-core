"""Shared typed-choice request and response contract for System-One providers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from friday.router import Route


MAX_STATE_CHARACTERS = 512
MAX_RESPONSE_BYTES = 64 * 1024
MIN_ROUTE_PROBABILITY = 0.5
ROUTE_CRITERIA = {
    Route.GENERAL: "ordinary conversation, explanation, or questions",
    Route.CODING: "software development, code, debugging, or technical implementation",
    Route.RESEARCH: "investigation, evidence gathering, comparison, or current information",
    Route.COMPUTER: "a request to control the user's computer or applications",
    Route.MEMORY: "a request to remember or recall user-provided information",
}
SENSITIVE_MARKER = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|secret|token|authorization|bearer|credential|"
    r"client[_ -]?secret|private[_ -]?key|access[_ -]?key|refresh[_ -]?token|cookie|session[_ -]?id)\b",
    re.IGNORECASE,
)
ENV_VALUE_ASSIGNMENT = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*\S+", re.ASCII)
SECRET_SHAPE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"[A-Za-z0-9_-]{40,})\b"
)


@dataclass(frozen=True)
class RouteChoice:
    route: Route
    probability: float
    confidence: float


def route_state_status(command: object) -> str | None:
    """Return a safe reason to keep a classification state local, if any."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_STATE_CHARACTERS:
        return "unsafe_or_oversized"
    if SENSITIVE_MARKER.search(command) or ENV_VALUE_ASSIGNMENT.search(command) or SECRET_SHAPE.search(command):
        return "sensitive"
    return None


def build_route_question(
    command: str,
    candidates: Iterable[Route],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the shared typed choice payload; model is required by hosted Jev."""
    options = tuple(dict.fromkeys(candidates))
    criteria = {route.value: ROUTE_CRITERIA[route] for route in options}
    body: dict[str, Any] = {
        "state": command.strip(),
        "questions": {
            "route": {
                "type": "choice",
                "instructions": (
                    "Choose the best FRIDAY processing category for this request. "
                    "This is only a routing hint; do not execute actions."
                ),
                "criteria": criteria,
            }
        },
    }
    if model is not None:
        body["model"] = model
    return body


def parse_route_answer(payload: Any, candidates: tuple[Route, ...]) -> RouteChoice | None:
    """Parse an untrusted System-One choice and require finite probabilities."""
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None
    answer = answers.get("route")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None
    try:
        route = Route(choice)
    except ValueError:
        return None
    if route not in candidates:
        return None

    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        return None
    probability = probabilities.get(choice)
    if isinstance(probability, bool) or not isinstance(probability, (float, int)):
        return None
    try:
        probability = float(probability)
    except (OverflowError, TypeError, ValueError):
        return None
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    try:
        confidence_value = float(confidence)
    except (OverflowError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(probability)
        or not 0.0 <= probability <= 1.0
        or not math.isfinite(confidence_value)
        or not 0.0 <= confidence_value <= 1.0
    ):
        return None
    return RouteChoice(
        route=route,
        probability=probability,
        confidence=confidence_value,
    )
