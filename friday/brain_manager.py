"""Provider-neutral cloud-first brain selection with local-only fallback."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from friday.brain import LOG_PATH, MAX_OUTPUT_TOKENS, LocalQwenBrain
from friday.lifecycle import LocalBrainLifecycleManager
from friday.profile import AssistantProfile


CLOUDFLARE_DEFAULT_MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"
CLOUDFLARE_TIMEOUT_SECONDS = 30
ACCOUNT_ID_RE = re.compile(r"^[a-fA-F0-9]{32}$")
MODEL_RE = re.compile(r"^@cf/[A-Za-z0-9._/-]+$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


CLOUDFLARE_HTTP = build_opener(_NoRedirect())


@dataclass(frozen=True)
class BrainReply:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str = ""


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
    ) -> None:
        self.cloud_enabled = profile.cloud_brain_enabled
        self.local = local_provider or LocalQwenProvider(profile)
        self.cloud = cloud_provider or CloudflareQwenProvider()

    def respond(
        self,
        command: str,
        history: list[dict[str, str]],
        on_status: Callable[[str], None] | None = None,
    ) -> BrainReply:
        messages = self.local.brain.messages(command, history)
        cloud_attempted = False
        if self.cloud_enabled and self.cloud.available():
            try:
                cloud_attempted = True
                if on_status:
                    on_status("THINKING_CLOUD")
                reply = self.cloud.chat(messages)
                _log_provider("brain=cloudflare")
                return reply
            except Exception as error:
                http_status = error.code if isinstance(error, HTTPError) else ""
                suffix = f" http_status={http_status}" if http_status else ""
                _log_provider(f"brain=cloudflare-failed error={type(error).__name__}{suffix}")

        if on_status:
            next_state = "THINKING_LOCAL" if self.local.lifecycle.health() else "STARTING_LOCAL_BRAIN"
            on_status(next_state)
        reply = self.local.chat(messages)
        _log_provider("brain=local-fallback" if cloud_attempted else "brain=local")
        return reply

    def close(self) -> None:
        self.local.close()


def _reply_from_payload(payload: object, provider: str) -> BrainReply:
    if not isinstance(payload, dict):
        raise RuntimeError("The brain provider returned an invalid response.")
    try:
        text = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise RuntimeError("The brain provider returned no assistant response.") from error
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return BrainReply(text, usage.get("prompt_tokens"), usage.get("completion_tokens"), provider)


def _log_provider(message: str) -> None:
    # Keep logs to provider/status metadata; never write prompts, response text, or credentials.
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(message + "\n")
