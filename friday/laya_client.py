"""Opt-in HTTP client for Laya's official TypeSafe Jev System-1 protocol."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from friday.router import Route


DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 30.0
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
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


HTTP = build_opener(_NoRedirect())


@dataclass(frozen=True)
class LayaRouteDecision:
    route: Route
    probability: float


class RemoteLayaClient:
    """Use Laya only for explicit ambiguous route choices; failures abstain."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "",
        api_key: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = ""
        self.api_key = ""
        self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        self._configuration_error = False
        self._status = "disabled" if not self.enabled else "unconfigured"
        self._http = opener or HTTP

        if not self.enabled:
            return

        normalized_url = _validate_base_url(base_url)
        if normalized_url is None:
            self._configuration_error = bool(base_url.strip())
            self._status = "invalid_configuration" if self._configuration_error else "unconfigured"
            return
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            self._configuration_error = True
            self._status = "invalid_configuration"
            return
        if not math.isfinite(timeout) or not 0.1 <= timeout <= MAX_TIMEOUT_SECONDS:
            self._configuration_error = True
            self._status = "invalid_configuration"
            return
        token = api_key.strip()
        if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            self._configuration_error = True
            self._status = "invalid_configuration"
            return

        self.base_url = normalized_url
        self.api_key = token
        self.timeout_seconds = timeout
        self._status = "configured"

    @classmethod
    def from_profile(cls, profile_enabled: bool) -> RemoteLayaClient:
        enabled_value = os.environ.get("FRIDAY_LAYA_ENABLED")
        enabled = profile_enabled
        if enabled_value is not None:
            parsed = _parse_bool(enabled_value)
            enabled = parsed if parsed is not None else False

        raw_timeout = os.environ.get("FRIDAY_LAYA_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = math.nan

        return cls(
            enabled=enabled,
            base_url=os.environ.get("FRIDAY_LAYA_BASE_URL", ""),
            api_key=os.environ.get("FRIDAY_LAYA_API_KEY", ""),
            timeout_seconds=timeout,
        )

    @property
    def status(self) -> str:
        """Safe status for UI/diagnostics; never includes the URL or API key."""
        return self._status

    @property
    def available(self) -> bool:
        return self.enabled and not self._configuration_error and bool(self.base_url)

    def health(self) -> bool:
        if not self.available:
            return False
        try:
            payload = self._request("GET", "/health")
        except Exception:
            self._status = "offline"
            return False
        healthy = isinstance(payload, dict) and payload.get("status") == "ok"
        self._status = "healthy" if healthy else "unhealthy"
        return healthy

    def classify_route(
        self,
        command: str,
        candidates: Iterable[Route],
    ) -> LayaRouteDecision | None:
        if not self.available:
            return None
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_STATE_CHARACTERS:
            self._status = "skipped_unsafe_or_oversized_state"
            return None
        if (
            SENSITIVE_MARKER.search(command)
            or ENV_VALUE_ASSIGNMENT.search(command)
            or SECRET_SHAPE.search(command)
        ):
            self._status = "skipped_sensitive_state"
            return None

        options = tuple(dict.fromkeys(candidates))
        if len(options) < 2 or any(not isinstance(route, Route) for route in options):
            self._status = "invalid_request"
            return None
        criteria = {route.value: ROUTE_CRITERIA[route] for route in options}
        body = {
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
        try:
            payload = self._request("POST", "/v1/systemone", body)
        except (HTTPError, URLError, TimeoutError, OSError):
            self._status = "offline"
            return None
        except Exception:
            self._status = "unavailable"
            return None

        decision = _parse_route_answer(payload, options)
        if decision is None:
            self._status = "invalid_response"
            return None
        if decision.route == Route.COMPUTER:
            self._status = "rejected_computer_route"
            return None
        if decision.probability < MIN_ROUTE_PROBABILITY:
            self._status = "low_confidence"
            return None
        self._status = "healthy"
        return decision

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with self._http.open(request, timeout=self.timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Laya response exceeded the configured size limit.")
        return json.loads(raw.decode("utf-8"))


def _validate_base_url(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        if parts.query or parts.fragment:
            return None
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
        if parts.scheme == "http":
            try:
                is_loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                is_loopback = parts.hostname.casefold() == "localhost"
            if not is_loopback:
                return None
        clean_path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, clean_path, "", "")).rstrip("/")
    except ValueError:
        return None


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return None


def _parse_route_answer(payload: Any, candidates: tuple[Route, ...]) -> LayaRouteDecision | None:
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
    return LayaRouteDecision(route=route, probability=probability)
