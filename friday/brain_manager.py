"""Provider-neutral cloud-first brain selection with local-only fallback."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from friday.brain import LOG_PATH, MAX_OUTPUT_TOKENS, LocalQwenBrain
from friday.lifecycle import LocalBrainLifecycleManager
from friday.profile import AssistantProfile


CLOUDFLARE_DEFAULT_MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"
CLOUDFLARE_TIMEOUT_SECONDS = 30
ACCOUNT_ID_RE = re.compile(r"^[a-fA-F0-9]{32}$")
MODEL_RE = re.compile(r"^@cf/[A-Za-z0-9._/-]+$")
FREELLMAPI_DEFAULT_ENABLED = False
FREELLMAPI_DEFAULT_BASE_URL = "http://127.0.0.1:3001/v1"
FREELLMAPI_DEFAULT_MODEL = "auto"
FREELLMAPI_DEFAULT_TIMEOUT_SECONDS = 15.0
FREELLMAPI_MAX_TIMEOUT_SECONDS = 60.0
FREELLMAPI_MAX_RESPONSE_BYTES = 1024 * 1024
FREELLMAPI_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
OMNIROUTE_DEFAULT_ENABLED = False
OMNIROUTE_DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"
OMNIROUTE_DEFAULT_MODEL = "auto"
OMNIROUTE_DEFAULT_TIMEOUT_SECONDS = 15.0
OMNIROUTE_MAX_TIMEOUT_SECONDS = 60.0
OMNIROUTE_MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


CLOUDFLARE_HTTP = build_opener(_NoRedirect())
FREELLMAPI_HTTP = build_opener(ProxyHandler({}), _NoRedirect())
OMNIROUTE_HTTP = build_opener(ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class BrainReply:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str = ""


@dataclass(frozen=True)
class BrainRequestMetadata:
    """Caller-supplied route policy for requests known to contain private data."""

    sensitivity: str = "public"
    trusted_remote_providers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.sensitivity not in {"public", "private"}:
            raise ValueError("sensitivity must be 'public' or 'private'.")

    def allows_remote(self, provider_name: str) -> bool:
        return self.sensitivity == "public" or provider_name in self.trusted_remote_providers


class BrainProvider(Protocol):
    name: str
    is_local: bool

    def available(self) -> bool: ...

    def chat(self, messages: list[dict[str, str]]) -> BrainReply: ...

    def health(self) -> str: ...


class CloudflareQwenProvider:
    name = "cloudflare"
    is_local = False

    def __init__(self) -> None:
        self.account_id = os.environ.get("FRIDAY_CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.api_token = os.environ.get("FRIDAY_CLOUDFLARE_API_TOKEN", "").strip()
        self.model = os.environ.get("FRIDAY_CLOUDFLARE_MODEL", CLOUDFLARE_DEFAULT_MODEL).strip()
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/v1"

    def available(self) -> bool:
        return bool(
            ACCOUNT_ID_RE.fullmatch(self.account_id)
            and self.api_token
            and MODEL_RE.fullmatch(self.model)
        )

    def health(self) -> str:
        # No billable probe: the first real user chat establishes service health.
        return "CONFIGURED" if self.available() else "UNAVAILABLE"

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        if not self.available():
            raise RuntimeError("Cloudflare Workers AI is not configured.")
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Direct Workers AI only. Never route through AI Gateway or a third-party model.
        with CLOUDFLARE_HTTP.open(request, timeout=CLOUDFLARE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _reply_from_payload(payload, self.name)


class FreeLLMAPIProvider:
    """Optional OpenAI-compatible client for a user-managed FreeLLMAPI gateway."""

    # Wire contract: https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md#chat-completions

    name = "freellmapi"
    is_local = False

    def __init__(
        self,
        *,
        enabled: bool = FREELLMAPI_DEFAULT_ENABLED,
        base_url: str = FREELLMAPI_DEFAULT_BASE_URL,
        api_key: str = "",
        model: str = FREELLMAPI_DEFAULT_MODEL,
        timeout_seconds: float = FREELLMAPI_DEFAULT_TIMEOUT_SECONDS,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = ""
        self._api_key = ""
        self.model = ""
        self.timeout_seconds = FREELLMAPI_DEFAULT_TIMEOUT_SECONDS
        self._configuration_error = False
        self._status = "disabled" if not self.enabled else "unconfigured"
        self._http = opener or FREELLMAPI_HTTP

        endpoint = _validate_freellmapi_base_url(base_url)
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = math.nan
        normalized_model = model.strip()
        token = api_key.strip()

        if (
            endpoint is None
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= FREELLMAPI_MAX_TIMEOUT_SECONDS
            or not FREELLMAPI_MODEL_RE.fullmatch(normalized_model)
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            self._configuration_error = True
            self._status = "invalid_configuration" if self.enabled else "disabled"
            return

        self.base_url = endpoint
        self.model = normalized_model
        self.timeout_seconds = timeout
        if not self.enabled:
            self._status = "disabled"
            return
        if not token:
            self._status = "unconfigured"
            return

        self._api_key = token
        self._status = "configured"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> FreeLLMAPIProvider:
        values = os.environ if environ is None else environ
        raw_enabled = values.get("FRIDAY_FREELLMAPI_ENABLED", "false").strip().casefold()
        enabled = raw_enabled in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            base_url=values.get("FRIDAY_FREELLMAPI_BASE_URL", FREELLMAPI_DEFAULT_BASE_URL),
            api_key=values.get("FRIDAY_FREELLMAPI_API_KEY", ""),
            model=values.get("FRIDAY_FREELLMAPI_MODEL", FREELLMAPI_DEFAULT_MODEL),
            timeout_seconds=values.get(
                "FRIDAY_FREELLMAPI_TIMEOUT_SECONDS",
                FREELLMAPI_DEFAULT_TIMEOUT_SECONDS,
            ),
        )

    def available(self) -> bool:
        return self.enabled and not self._configuration_error and bool(self._api_key)

    def health(self) -> str:
        # Avoid a network probe: the first opted-in user chat establishes availability.
        return self._status.upper()

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        if not self.available():
            raise RuntimeError("FreeLLMAPI is not configured.")

        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with self._http.open(request, timeout=self.timeout_seconds) as response:
            raw = response.read(FREELLMAPI_MAX_RESPONSE_BYTES + 1)
        if len(raw) > FREELLMAPI_MAX_RESPONSE_BYTES:
            raise RuntimeError("FreeLLMAPI returned an oversized response.")
        payload = json.loads(raw.decode("utf-8"))
        return _reply_from_payload(payload, self.name)


class OmniRouteProvider:
    """Optional OpenAI-compatible client for a user-managed local OmniRoute gateway."""

    name = "omniroute"
    is_local = False

    def __init__(
        self,
        *,
        enabled: bool = OMNIROUTE_DEFAULT_ENABLED,
        base_url: str = OMNIROUTE_DEFAULT_BASE_URL,
        api_key: str = "",
        model: str = OMNIROUTE_DEFAULT_MODEL,
        timeout_seconds: float = OMNIROUTE_DEFAULT_TIMEOUT_SECONDS,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = ""
        self._api_key = ""
        self.model = ""
        self.timeout_seconds = OMNIROUTE_DEFAULT_TIMEOUT_SECONDS
        self._configuration_error = False
        self._status = "disabled" if not self.enabled else "unconfigured"
        self._http = opener or OMNIROUTE_HTTP

        endpoint = _validate_omniroute_base_url(base_url)
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = math.nan
        normalized_model = model.strip()
        token = api_key.strip()

        if (
            endpoint is None
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= OMNIROUTE_MAX_TIMEOUT_SECONDS
            or not FREELLMAPI_MODEL_RE.fullmatch(normalized_model)
            or len(token) > 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            self._configuration_error = True
            self._status = "invalid_configuration" if self.enabled else "disabled"
            return

        self.base_url = endpoint
        self.model = normalized_model
        self.timeout_seconds = timeout
        if not self.enabled:
            self._status = "disabled"
            return

        # REQUIRE_API_KEY is optional in OmniRoute; attach a key only when supplied.
        self._api_key = token
        self._status = "configured"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> OmniRouteProvider:
        values = os.environ if environ is None else environ
        raw_enabled = values.get("FRIDAY_OMNIROUTE_ENABLED", "false").strip().casefold()
        enabled = raw_enabled in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            base_url=values.get("FRIDAY_OMNIROUTE_BASE_URL", OMNIROUTE_DEFAULT_BASE_URL),
            api_key=values.get("FRIDAY_OMNIROUTE_API_KEY", ""),
            model=values.get("FRIDAY_OMNIROUTE_MODEL", OMNIROUTE_DEFAULT_MODEL),
            timeout_seconds=values.get(
                "FRIDAY_OMNIROUTE_TIMEOUT_SECONDS",
                OMNIROUTE_DEFAULT_TIMEOUT_SECONDS,
            ),
        )

    def available(self) -> bool:
        return self.enabled and not self._configuration_error

    def health(self) -> str:
        # Avoid a network probe: the first opted-in user chat establishes availability.
        return self._status.upper()

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        if not self.available():
            raise RuntimeError("OmniRoute is not configured.")

        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        with self._http.open(request, timeout=self.timeout_seconds) as response:
            raw = response.read(OMNIROUTE_MAX_RESPONSE_BYTES + 1)
        if len(raw) > OMNIROUTE_MAX_RESPONSE_BYTES:
            raise RuntimeError("OmniRoute returned an oversized response.")
        payload = json.loads(raw.decode("utf-8"))
        return _reply_from_payload(payload, self.name)


class LocalQwenProvider:
    name = "local"
    is_local = True

    def __init__(self, profile: AssistantProfile, lifecycle: LocalBrainLifecycleManager | None = None, brain: LocalQwenBrain | None = None) -> None:
        self.brain = brain or LocalQwenBrain(profile)
        self.lifecycle = lifecycle or LocalBrainLifecycleManager()

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "READY" if self.lifecycle.health() else self.lifecycle.state.value

    def chat(self, messages: list[dict[str, str]]) -> BrainReply:
        self.lifecycle.begin_request()
        try:
            text, prompt_tokens, completion_tokens = self.brain.chat(messages)
            return BrainReply(text, prompt_tokens, completion_tokens, self.name)
        finally:
            self.lifecycle.finish_request()

    def close(self) -> None:
        self.lifecycle.close()


class BrainManager:
    """The only brain-selection entry point used by FridayCore."""

    def __init__(
        self,
        profile: AssistantProfile,
        local_provider: LocalQwenProvider | None = None,
        cloud_provider: CloudflareQwenProvider | None = None,
        freellmapi_provider: FreeLLMAPIProvider | None = None,
        omniroute_provider: OmniRouteProvider | None = None,
    ) -> None:
        self.cloud_enabled = profile.cloud_brain_enabled
        self.local = local_provider or LocalQwenProvider(profile)
        self.cloud = cloud_provider or CloudflareQwenProvider()
        self.freellmapi = freellmapi_provider or FreeLLMAPIProvider.from_environment()
        self.omniroute = omniroute_provider or OmniRouteProvider.from_environment()

    def respond(
        self,
        command: str,
        history: list[dict[str, str]],
        on_status: Callable[[str], None] | None = None,
        request_metadata: BrainRequestMetadata | None = None,
    ) -> BrainReply:
        messages = self.local.brain.messages(command, history)
        metadata = request_metadata or BrainRequestMetadata()
        remote_attempted = False
        providers: list[BrainProvider] = []
        if self.omniroute.enabled:
            providers.append(self.omniroute)
            providers.append(self.freellmapi)
            if self.cloud_enabled:
                providers.append(self.cloud)
        else:
            # Preserve the established Cloudflare -> FreeLLMAPI order when OmniRoute is off.
            if self.cloud_enabled:
                providers.append(self.cloud)
            providers.append(self.freellmapi)

        for provider in providers:
            if not provider.available() or not metadata.allows_remote(provider.name):
                continue
            try:
                remote_attempted = True
                if on_status:
                    on_status("THINKING_CLOUD")
                reply = provider.chat(messages)
                _log_provider(f"brain={provider.name}")
                return reply
            except Exception as error:
                http_status = error.code if isinstance(error, HTTPError) else ""
                suffix = f" http_status={http_status}" if http_status else ""
                _log_provider(f"brain={provider.name}-failed error={type(error).__name__}{suffix}")

        if on_status:
            next_state = "THINKING_LOCAL" if self.local.lifecycle.health() else "STARTING_LOCAL_BRAIN"
            on_status(next_state)
        reply = self.local.chat(messages)
        _log_provider("brain=local-fallback" if remote_attempted else "brain=local")
        return reply

    def close(self) -> None:
        self.local.close()


def _reply_from_payload(payload: object, provider: str) -> BrainReply:
    if not isinstance(payload, dict):
        raise RuntimeError("The brain provider returned an invalid response.")
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise RuntimeError("The brain provider returned no assistant response.") from error
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("The brain provider returned no assistant response.")
    text = text.strip()
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return BrainReply(text, usage.get("prompt_tokens"), usage.get("completion_tokens"), provider)


def _log_provider(message: str) -> None:
    # Keep logs to provider/status metadata; never write prompts, response text, or credentials.
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(message + "\n")


def _validate_freellmapi_base_url(value: str) -> str | None:
    return _validate_openai_gateway_base_url(value)


def _validate_omniroute_base_url(value: str) -> str | None:
    endpoint = _validate_openai_gateway_base_url(value)
    if endpoint is None:
        return None
    try:
        hostname = urlsplit(endpoint).hostname
        if hostname is None or not ipaddress.ip_address(hostname).is_loopback:
            return None
    except ValueError:
        return None
    return endpoint


def _validate_openai_gateway_base_url(value: str) -> str | None:
    if not value or len(value) > 2048 or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        return None
    if parsed.scheme == "http":
        if hostname.casefold() != "localhost":
            try:
                if not ipaddress.ip_address(hostname).is_loopback:
                    return None
            except ValueError:
                return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, path, "", ""))
