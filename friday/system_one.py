"""Provider-neutral typed decisions for FRIDAY's System-One route hints."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from friday.laya_client import RemoteLayaClient
from friday.system_one_contract import (
    MAX_RESPONSE_BYTES,
    MIN_ROUTE_PROBABILITY,
    RouteChoice,
    build_route_question,
    parse_route_answer,
    route_state_status,
)
from friday.router import Route


JEV_DEFAULT_ENABLED = False
JEV_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_DEFAULT_MODEL = "jev-latest"
JEV_DEFAULT_TIMEOUT_SECONDS = 5.0
JEV_MAX_TIMEOUT_SECONDS = 30.0
JEV_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


HTTP = build_opener(_NoRedirect())


@dataclass(frozen=True)
class SystemOneDecision:
    route: Route
    probability: float
    confidence: float
    provider: str


class SystemOneProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    @property
    def status(self) -> str: ...

    def classify_route(
        self, command: str, candidates: Iterable[Route]
    ) -> SystemOneDecision | None: ...


class RemoteLayaProvider:
    """Adapt the existing Phase 3C client to the provider-neutral interface."""

    name = "laya"

    def __init__(self, client: RemoteLayaClient) -> None:
        self.client = client

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def status(self) -> str:
        return self.client.status

    def classify_route(
        self, command: str, candidates: Iterable[Route]
    ) -> SystemOneDecision | None:
        decision = self.client.classify_route(command, candidates)
        if decision is None:
            return None
        return SystemOneDecision(
            decision.route,
            decision.probability,
            decision.confidence if decision.confidence is not None else decision.probability,
            self.name,
        )


class HostedJevProvider:
    """Optional TypeSafe System-One HTTP provider; disabled unless explicitly enabled."""

    name = "jev"

    def __init__(
        self,
        *,
        enabled: bool = JEV_DEFAULT_ENABLED,
        base_url: str = JEV_DEFAULT_ENDPOINT,
        api_key: str = "",
        model: str = JEV_DEFAULT_MODEL,
        timeout_seconds: float = JEV_DEFAULT_TIMEOUT_SECONDS,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = ""
        self.model = ""
        self._api_key = ""
        self.timeout_seconds = JEV_DEFAULT_TIMEOUT_SECONDS
        self._configuration_error = False
        self._last_http_status: int | None = None
        self._status = "disabled" if not self.enabled else "unconfigured"
        self._http = opener or HTTP

        if not self.enabled:
            return

        endpoint = _validate_systemone_endpoint(base_url)
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = math.nan
        normalized_model = model.strip()
        token = api_key.strip()

        if endpoint is None or not math.isfinite(timeout) or not 0.1 <= timeout <= JEV_MAX_TIMEOUT_SECONDS:
            self._configuration_error = True
            self._status = "invalid_configuration"
            return
        if not JEV_MODEL_PATTERN.fullmatch(normalized_model):
            self._configuration_error = True
            self._status = "invalid_configuration"
            return
        if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            self._configuration_error = True
            self._status = "invalid_configuration"
            return
        if not token:
            self._status = "unconfigured"
            return

        self.base_url = endpoint
        self.model = normalized_model
        self._api_key = token
        self.timeout_seconds = timeout
        self._status = "configured"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> HostedJevProvider:
        values = os.environ if environ is None else environ
        raw_enabled = values.get("FRIDAY_JEV_ENABLED", "false").strip().casefold()
        enabled = raw_enabled in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            base_url=values.get("FRIDAY_JEV_BASE_URL", JEV_DEFAULT_ENDPOINT),
            api_key=values.get("FRIDAY_JEV_API_KEY", ""),
            model=values.get("FRIDAY_JEV_MODEL", JEV_DEFAULT_MODEL),
            timeout_seconds=values.get(
                "FRIDAY_JEV_TIMEOUT_SECONDS", JEV_DEFAULT_TIMEOUT_SECONDS
            ),
        )

    @property
    def available(self) -> bool:
        return self.enabled and not self._configuration_error and bool(self._api_key)

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_http_status(self) -> int | None:
        """Expose only the HTTP status code, never response headers or credentials."""
        return self._last_http_status

    def classify_route(
        self, command: str, candidates: Iterable[Route]
    ) -> SystemOneDecision | None:
        if not self.available:
            return None

        state_status = route_state_status(command)
        if state_status is not None:
            self._status = f"skipped_{state_status}_state"
            return None

        options = tuple(dict.fromkeys(candidates))
        if len(options) < 2 or any(not isinstance(route, Route) for route in options):
            self._status = "invalid_request"
            return None

        body = build_route_question(command, options, model=self.model)
        request = Request(
            self.base_url,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._http.open(request, timeout=self.timeout_seconds) as response:
                self._last_http_status = response.getcode()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            self._last_http_status = error.code
            self._status = "http_error"
            return None
        except (URLError, TimeoutError, OSError):
            self._status = "offline"
            return None
        except Exception:
            self._status = "unavailable"
            return None

        if len(raw) > MAX_RESPONSE_BYTES:
            self._status = "invalid_response"
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._status = "invalid_response"
            return None

        choice: RouteChoice | None = parse_route_answer(payload, options)
        decision = (
            SystemOneDecision(
                choice.route,
                choice.probability,
                choice.confidence,
                self.name,
            )
            if choice is not None
            else None
        )
        if decision is None:
            self._status = "invalid_response"
            return None
        self._status = "healthy"
        return decision


class SystemOneManager:
    """Try explicitly configured decision providers in order and otherwise abstain."""

    def __init__(
        self,
        *,
        laya_provider: SystemOneProvider | None = None,
        jev_provider: SystemOneProvider | None = None,
    ) -> None:
        # Preserve Phase 3C Laya priority if both optional providers are enabled.
        self._providers = tuple(
            provider
            for provider in (laya_provider, jev_provider)
            if provider is not None
        )
        self._last_status: str | None = None

    @property
    def status(self) -> str:
        if self._last_status is not None:
            return self._last_status
        for provider in self._providers:
            if provider.available:
                return f"{provider.name}_configured"
        for provider in self._providers:
            if provider.status != "disabled":
                return f"{provider.name}_{provider.status}"
        return "disabled"

    def classify_route(
        self, command: str, candidates: Iterable[Route]
    ) -> SystemOneDecision | None:
        options = tuple(dict.fromkeys(candidates))
        if len(options) < 2 or any(not isinstance(route, Route) for route in options):
            self._last_status = "invalid_request"
            return None

        for provider in self._providers:
            if not provider.available:
                continue
            try:
                decision = provider.classify_route(command, options)
            except Exception:
                self._last_status = f"{provider.name}_unavailable"
                continue
            self._last_status = f"{provider.name}_{provider.status}"
            if decision is None:
                continue
            if not isinstance(decision, SystemOneDecision):
                self._last_status = f"{provider.name}_invalid_response"
                continue
            probability = decision.probability
            if isinstance(probability, bool) or not isinstance(probability, (float, int)):
                self._last_status = f"{provider.name}_invalid_response"
                continue
            confidence = decision.confidence
            if isinstance(confidence, bool) or not isinstance(confidence, (float, int)):
                self._last_status = f"{provider.name}_invalid_response"
                continue
            try:
                normalized_probability = float(probability)
                normalized_confidence = float(confidence)
            except (OverflowError, TypeError, ValueError):
                self._last_status = f"{provider.name}_invalid_response"
                continue
            if (
                not isinstance(decision.route, Route)
                or decision.route == Route.COMPUTER
                or decision.route not in options
                or not math.isfinite(normalized_probability)
                or not 0.0 <= normalized_probability <= 1.0
                or not math.isfinite(normalized_confidence)
                or not 0.0 <= normalized_confidence <= 1.0
            ):
                self._last_status = f"{provider.name}_invalid_response"
                continue
            if normalized_probability < MIN_ROUTE_PROBABILITY:
                self._last_status = f"{provider.name}_low_confidence"
                continue
            return SystemOneDecision(
                decision.route,
                normalized_probability,
                normalized_confidence,
                provider.name,
            )
        return None


def _validate_systemone_endpoint(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path.rstrip("/") != "/v1/systemone"
        ):
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
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except ValueError:
        return None
