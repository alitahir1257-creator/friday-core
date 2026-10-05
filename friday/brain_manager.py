"""Provider-neutral cloud-first brain selection with local-only fallback."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
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

from friday.brain import LOG_PATH, MAX_OUTPUT_TOKENS, MODEL_NAME, LocalQwenBrain
from friday.knowledge import KnowledgeVault
from friday.knowledge.unified import UnifiedKnowledgeManager
from friday.lifecycle import LocalBrainLifecycleManager
from friday.memory import MemoryManager
from friday.model_policy import (
    APPROVED_FREE_GEMINI_MODELS,
    ModelPolicy,
    ModelPolicyEngine,
    ModelSelection,
    ModelTarget,
    PrivacyLevel,
    is_approved_free_gemini_model_response,
)
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
    model: str | None = None
    policy: ModelPolicy | None = None
    privacy: PrivacyLevel = PrivacyLevel.PUBLIC
    latency_ms: float | None = None
    fallback_count: int = 0
    selection: ModelSelection | None = None


@dataclass(frozen=True)
class BrainRequestMetadata:
    """Caller-supplied route policy for requests known to contain private data."""

    sensitivity: str = "public"
    trusted_remote_providers: frozenset[str] = frozenset()
    privacy: str | None = None
    task_class: str | None = None
    memory_namespace: str = "global"

    def __post_init__(self) -> None:
        valid = {"public", "private", "local_only"}
        if self.sensitivity not in valid or (self.privacy is not None and self.privacy not in valid):
            raise ValueError("privacy must be 'public', 'private', or 'local_only'.")
        if self.task_class is not None and self.task_class not in {policy.value for policy in ModelPolicy}:
            raise ValueError("task_class must be a supported model policy.")

    @property
    def effective_privacy(self) -> str:
        levels = {"public": 0, "private": 1, "local_only": 2}
        values = [self.sensitivity, self.privacy or "public"]
        if self.task_class == ModelPolicy.PRIVATE.value:
            values.append("private")
        return max(values, key=levels.__getitem__)

    def allows_remote(self, provider_name: str) -> bool:
        return self.effective_privacy == "public" and (
            not self.trusted_remote_providers or provider_name in self.trusted_remote_providers
        )


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
        if self.model not in APPROVED_FREE_GEMINI_MODELS:
            raise RuntimeError("OmniRoute requires an explicit approved free model.")
        return self._approved_reply(messages, self.model)

    def chat_with_model(self, messages: list[dict[str, str]], model: str) -> BrainReply:
        """Send one request to an explicitly approved free Gemini model."""
        if model not in APPROVED_FREE_GEMINI_MODELS:
            raise RuntimeError("OmniRoute model is outside the approved free model pool.")
        return self._approved_reply(messages, model)

    def _approved_reply(self, messages: list[dict[str, str]], model: str) -> BrainReply:
        reply = self._chat(messages, model)
        if reply.model is not None and not is_approved_free_gemini_model_response(
            model, reply.model
        ):
            raise RuntimeError("OmniRoute returned a model outside the approved free model pool.")
        return replace(reply, model=model)

    def _chat(self, messages: list[dict[str, str]], model: str) -> BrainReply:
        if not self.available():
            raise RuntimeError("OmniRoute is not configured.")

        body = json.dumps(
            {
                "model": model,
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
        reply = _reply_from_payload(payload, self.name)
        return replace(reply, model=reply.model or model)


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
        memory_manager: MemoryManager | None = None,
        knowledge_vault: KnowledgeVault | None = None,
        unified_manager: UnifiedKnowledgeManager | None = None,
    ) -> None:
        self.cloud_enabled = profile.cloud_brain_enabled
        self.local = local_provider or LocalQwenProvider(profile)
        self.cloud = cloud_provider or CloudflareQwenProvider()
        self.freellmapi = freellmapi_provider or FreeLLMAPIProvider.from_environment()
        self.omniroute = omniroute_provider or OmniRouteProvider.from_environment()
        self.policy_engine = ModelPolicyEngine()
        try:
            self.memory = memory_manager if memory_manager is not None else MemoryManager()
        except Exception as error:
            self.memory = None
            self._log_memory_event(f"memory_unavailable error={type(error).__name__}")
        try:
            self.knowledge = knowledge_vault if knowledge_vault is not None else KnowledgeVault()
        except Exception as error:
            self.knowledge = None
            _log_provider(f"knowledge_unavailable error={type(error).__name__}")
        self.unified = unified_manager if unified_manager is not None else UnifiedKnowledgeManager(self.memory, self.knowledge)

    def respond(
        self,
        command: str,
        history: list[dict[str, str]],
        on_status: Callable[[str], None] | None = None,
        request_metadata: BrainRequestMetadata | None = None,
        model_selection: ModelSelection | None = None,
    ) -> BrainReply:
        metadata = request_metadata or BrainRequestMetadata()
        privacy = self.policy_engine.classify_privacy(command, metadata.effective_privacy)
        requested_policy = metadata.task_class
        if model_selection is not None:
            levels = {
                PrivacyLevel.PUBLIC: 0,
                PrivacyLevel.PRIVATE: 1,
                PrivacyLevel.LOCAL_ONLY: 2,
            }
            if levels[model_selection.privacy] > levels[privacy]:
                privacy = model_selection.privacy
            requested_policy = requested_policy or ModelPolicy(model_selection.policy).value
        # Rebuild the target list at this trust boundary; caller-supplied routes cannot add
        # arbitrary models or bypass the local-only privacy rule.
        selection = self.policy_engine.select(
            command,
            privacy=privacy,
            requested_policy=requested_policy,
            cloudflare_enabled=self.cloud_enabled,
        )

        if selection.policy == ModelPolicy.COMPUTER:
            raise RuntimeError("COMPUTER requests must use the deterministic action dispatcher.")
        if selection.policy == ModelPolicy.CODING:
            message = "Repository work is assigned to Codex; FRIDAY chat fallback is disabled."
            result = replace(selection, selected_backend="codex", selected_model="codex", latency_ms=0.0)
            return BrainReply(
                message,
                provider="codex",
                model="codex",
                policy=selection.policy,
                privacy=selection.privacy,
                latency_ms=0.0,
                selection=result,
            )
        if selection.policy in {ModelPolicy.VISION, ModelPolicy.VOICE}:
            raise RuntimeError(f"{selection.policy.value} routing is a placeholder in this phase.")

        messages = self.local.brain.messages(command, history)
        targets = selection.targets
        if selection.privacy in {PrivacyLevel.PRIVATE, PrivacyLevel.LOCAL_ONLY}:
            targets = (ModelTarget("local", self.local_model),)
        try:
            context = self.unified.build_context(
                command, namespace=metadata.memory_namespace,
                privacy_level=selection.privacy.value, route_backend=selection.preferred_backend,
            )
            if context.privacy not in {"public", "private", "local_only"} or (
                context.privacy != "public" and (
                    selection.privacy == PrivacyLevel.PUBLIC or selection.preferred_backend != "local"
                )
            ):
                raise PermissionError("Unified context is not allowed on this route.")
            if context.text:
                messages[1:1] = [{"role": "system", "content": context.text}]
        except Exception as error:
            _log_provider(f"unified_retrieval_failed error={type(error).__name__}")

        started = time.perf_counter()
        fallback_count = 0
        for target in targets:
            provider = self._provider_for(target.backend)
            if provider is None or not provider.available():
                fallback_count += 1
                self._log_policy_attempt(selection, target, fallback_count, "unavailable")
                continue
            try:
                if on_status and not provider.is_local:
                    on_status("THINKING_CLOUD")
                elif on_status:
                    next_state = (
                        "THINKING_LOCAL"
                        if self.local.lifecycle.health()
                        else "STARTING_LOCAL_BRAIN"
                    )
                    on_status(next_state)
                reply = self._call_provider(provider, target, messages)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                actual_model = reply.model or target.model or self._provider_model(provider)
                result = replace(
                    selection,
                    selected_backend=provider.name,
                    selected_model=actual_model,
                    latency_ms=elapsed_ms,
                    fallback_count=fallback_count,
                )
                reply = replace(
                    reply,
                    provider=provider.name,
                    model=actual_model,
                    policy=selection.policy,
                    privacy=selection.privacy,
                    latency_ms=elapsed_ms,
                    fallback_count=fallback_count,
                    selection=result,
                )
                self._log_policy_attempt(selection, target, fallback_count, "success", elapsed_ms)
                return reply
            except Exception as error:
                http_status = error.code if isinstance(error, HTTPError) else ""
                suffix = f" http_status={http_status}" if http_status else ""
                fallback_count += 1
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                self._log_policy_attempt(
                    selection,
                    target,
                    fallback_count,
                    f"failed:{type(error).__name__}{suffix}",
                    elapsed_ms,
                )

        if selection.privacy in {PrivacyLevel.PRIVATE, PrivacyLevel.LOCAL_ONLY}:
            raise RuntimeError("Private/local-only route error: local Qwen is unavailable.")
        raise RuntimeError("No approved model route succeeded.")

    @property
    def local_model(self) -> str:
        return getattr(self.local.brain, "model", MODEL_NAME)

    def _provider_for(self, backend: str) -> BrainProvider | None:
        return {
            "omniroute": self.omniroute,
            "freellmapi": self.freellmapi,
            "cloudflare": self.cloud if self.cloud_enabled else None,
            "local": self.local,
        }.get(backend)

    @staticmethod
    def _provider_model(provider: BrainProvider) -> str | None:
        return getattr(provider, "model", None)

    @staticmethod
    def _call_provider(
        provider: BrainProvider, target: ModelTarget, messages: list[dict[str, str]]
    ) -> BrainReply:
        if target.backend == "omniroute":
            send_model = getattr(provider, "chat_with_model", None)
            if not callable(send_model) or target.model is None:
                raise RuntimeError("OmniRoute explicit-model routing is unavailable.")
            return send_model(messages, target.model)
        return provider.chat(messages)

    @staticmethod
    def _log_policy_attempt(
        selection: ModelSelection,
        target: ModelTarget,
        fallback_count: int,
        outcome: str,
        latency_ms: float | None = None,
    ) -> None:
        elapsed = "unknown" if latency_ms is None else f"{latency_ms:.1f}"
        model = target.model or "provider-configured"
        _log_provider(
            f"policy={selection.policy.value} privacy={selection.privacy.value} "
            f"backend={target.backend} model={model} latency_ms={elapsed} "
            f"success={outcome == 'success'} fallback_count={fallback_count} "
            f"outcome={outcome}"
        )

    def close(self) -> None:
        self.local.close()
        if self.memory is not None:
            try:
                self.memory.close()
            except Exception as error:
                self._log_memory_event(f"memory_close_failed error={type(error).__name__}")

    @staticmethod
    def _log_memory_event(message: str) -> None:
        try:
            _log_provider(message)
        except Exception:
            # Persistent memory is optional; a filesystem logging failure must not break chat.
            pass


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
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        model = None
    return BrainReply(
        text,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        provider,
        model=model,
    )


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
