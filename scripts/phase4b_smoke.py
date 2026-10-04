"""Focused Phase 4B OmniRoute adapter and fallback checks; loopback mocks only."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.brain_manager import (  # noqa: E402
    BrainManager,
    BrainReply,
    BrainRequestMetadata,
    CLOUDFLARE_DEFAULT_MODEL,
    CloudflareQwenProvider,
    FREELLMAPI_DEFAULT_BASE_URL,
    FREELLMAPI_DEFAULT_MODEL,
    FreeLLMAPIProvider,
    OMNIROUTE_DEFAULT_BASE_URL,
    OMNIROUTE_DEFAULT_MODEL,
    OMNIROUTE_DEFAULT_TIMEOUT_SECONDS,
    OmniRouteProvider,
)
from friday.capabilities.registry import Risk  # noqa: E402
from friday.core import FridayCore  # noqa: E402
from friday.laya_client import RemoteLayaClient  # noqa: E402
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState  # noqa: E402
from friday.profile import AssistantProfile  # noqa: E402
from friday.router import Route, RoutingDecision  # noqa: E402
from friday.system_one import (  # noqa: E402
    HostedJevProvider,
    RemoteLayaProvider,
    SystemOneManager,
)


class MockGateway:
    def __init__(self, expected_key: str) -> None:
        self.expected_key = expected_key
        self.mode = "success"
        self.posts = 0
        self.auth_matches = False
        self.request_path = ""
        self.body: dict[str, Any] | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"

    def _handler_type(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                gateway.posts += 1
                gateway.request_path = self.path
                gateway.auth_matches = self.headers.get("Authorization", "") == (
                    f"Bearer {gateway.expected_key}"
                )
                length = min(int(self.headers.get("Content-Length", "0")), 128 * 1024)
                try:
                    gateway.body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    gateway.body = None

                if gateway.mode == "timeout":
                    time.sleep(0.4)
                if gateway.mode == "429":
                    self._send(429, {"error": {"message": "rate limited"}})
                    return
                if gateway.mode == "500":
                    self._send(500, {"error": {"message": "server error"}})
                    return
                if gateway.mode == "malformed":
                    self._send(200, {"choices": []})
                    return
                self._send(
                    200,
                    {
                        "id": "chatcmpl-mock",
                        "choices": [
                            {"message": {"role": "assistant", "content": "gateway reply"}}
                        ],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    },
                )

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> MockGateway:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class FakeLocalProvider:
    class Brain:
        @staticmethod
        def messages(command: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
            return [
                {"role": "system", "content": "test system"},
                *history,
                {"role": "user", "content": command},
            ]

    class Lifecycle:
        @staticmethod
        def health() -> bool:
            return False

    def __init__(self) -> None:
        self.brain = self.Brain()
        self.lifecycle = self.Lifecycle()
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "READY"

    def chat(self, _messages: list[dict[str, str]]) -> BrainReply:
        self.calls += 1
        return BrainReply("local fallback", provider="local")

    def close(self) -> None:
        return


class FakeCloudProvider:
    name = "cloudflare"
    is_local = False

    def __init__(self, *, succeeds: bool = False) -> None:
        self.succeeds = succeeds
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "CONFIGURED"

    def chat(self, _messages: list[dict[str, str]]) -> BrainReply:
        self.calls += 1
        if self.succeeds:
            return BrainReply("cloud reply", provider=self.name)
        raise RuntimeError("simulated cloud failure")


class CountingSystemOneManager:
    def __init__(self) -> None:
        self.calls = 0

    def classify_route(self, *_args: object, **_kwargs: object):
        self.calls += 1
        return None


class AmbiguousRouter:
    def route(self, _command: str) -> RoutingDecision:
        return RoutingDecision(Route.GENERAL, 0.5, "test ambiguity")

    def ambiguous_routes(self, _command: str) -> tuple[Route, Route]:
        return (Route.GENERAL, Route.CODING)


class FakeComputerController:
    def execute_request(self, _command: str, approved: bool = False):
        return "mocked notepad action", Risk.SAFE


def manager_for(
    local: FakeLocalProvider,
    *,
    omni: OmniRouteProvider,
    free: FreeLLMAPIProvider,
    cloud: FakeCloudProvider | None = None,
    cloud_enabled: bool = False,
) -> BrainManager:
    return BrainManager(
        AssistantProfile(cloud_brain_enabled=cloud_enabled),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud,  # type: ignore[arg-type]
        freellmapi_provider=free,
        omniroute_provider=omni,
    )


def configured_omni(base_url: str, key: str, *, timeout: float = 2.0) -> OmniRouteProvider:
    return OmniRouteProvider(
        enabled=True,
        base_url=base_url,
        api_key=key,
        model="auto",
        timeout_seconds=timeout,
    )


def configured_free(base_url: str, key: str) -> FreeLLMAPIProvider:
    return FreeLLMAPIProvider(
        enabled=True,
        base_url=base_url,
        api_key=key,
        model="auto",
        timeout_seconds=2.0,
    )


def main() -> None:
    results: dict[str, str] = {}
    omni_key = f"omniroute-{secrets.token_urlsafe(32)}"
    free_key = f"freellmapi-{secrets.token_urlsafe(32)}"
    with tempfile.TemporaryDirectory(prefix="friday-phase4b-") as temp_dir:
        test_log = Path(temp_dir) / "provider.log"
        profile_text = (ROOT / "profile.json").read_text(encoding="utf-8")

        # A: Provider defaults remain inert; disabled OmniRoute preserves the old order.
        with MockGateway(free_key) as gateway:
            omni = OmniRouteProvider.from_environment({})
            assert not omni.enabled and not omni.available()
            assert omni.base_url == OMNIROUTE_DEFAULT_BASE_URL
            assert omni.model == OMNIROUTE_DEFAULT_MODEL == "auto"
            assert omni.timeout_seconds == OMNIROUTE_DEFAULT_TIMEOUT_SECONDS
            assert not OmniRouteProvider(
                enabled=True,
                base_url="https://gateway.example/v1",
            ).available()
            free = configured_free(gateway.base_url, free_key)
            local = FakeLocalProvider()
            cloud = FakeCloudProvider(succeeds=True)
            manager = manager_for(
                local,
                omni=omni,
                free=free,
                cloud=cloud,
                cloud_enabled=True,
            )
            reply = manager.respond("priority check", [])
            assert reply.provider == "cloudflare"
            assert cloud.calls == 1 and gateway.posts == 0 and local.calls == 0
            manager.close()
        results["default_disabled_and_existing_order"] = "PASS"

        # C: OpenAI-compatible request, optional Bearer key, auto model, and normalized reply.
        with MockGateway(omni_key) as gateway:
            omni = configured_omni(gateway.base_url, omni_key)
            free = FreeLLMAPIProvider.from_environment({})
            local = FakeLocalProvider()
            cloud = FakeCloudProvider()
            manager = manager_for(
                local,
                omni=omni,
                free=free,
                cloud=cloud,
                cloud_enabled=True,
            )
            with _patched_log(test_log):
                reply = manager.respond("say hello", [])
            assert reply.text == "gateway reply" and reply.provider == "omniroute"
            assert reply.prompt_tokens == 7 and reply.completion_tokens == 3
            assert gateway.posts == 1 and gateway.auth_matches
            assert gateway.request_path == "/v1/chat/completions"
            assert gateway.body == {
                "model": "auto",
                "messages": [
                    {"role": "system", "content": "test system"},
                    {"role": "user", "content": "say hello"},
                ],
                "max_tokens": 1024,
                "temperature": 0.3,
                "stream": False,
            }
            assert free.health() == "DISABLED"
            assert cloud.calls == local.calls == 0
            manager.close()
        results["mocked_success_and_brainmanager_normalization"] = "PASS"

        # D-H: One OmniRoute attempt, followed by the existing FreeLLMAPI provider.
        for mode, timeout, result_name in (
            ("timeout", 0.1, "timeout_fallback"),
            ("429", 2.0, "429_fallback"),
            ("500", 2.0, "500_fallback"),
            ("malformed", 2.0, "malformed_response_fallback"),
        ):
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                omni_gateway.mode = mode
                omni = configured_omni(omni_gateway.base_url, omni_key, timeout=timeout)
                free = configured_free(free_gateway.base_url, free_key)
                local = FakeLocalProvider()
                cloud = FakeCloudProvider()
                manager = manager_for(
                    local,
                    omni=omni,
                    free=free,
                    cloud=cloud,
                    cloud_enabled=True,
                )
                with _patched_log(test_log):
                    reply = manager.respond("say hello", [])
                assert reply.provider == "freellmapi" and reply.text == "gateway reply"
                assert omni_gateway.posts == free_gateway.posts == 1
                assert omni_gateway.auth_matches and free_gateway.auth_matches
                assert local.calls == cloud.calls == 0
                manager.close()
            results[result_name] = "PASS"

        # Q: The existing FreeLLMAPI adapter still sends its OpenAI-compatible request.
        with MockGateway(free_key) as gateway:
            omni = OmniRouteProvider.from_environment({})
            free = configured_free(gateway.base_url, free_key)
            local = FakeLocalProvider()
            manager = manager_for(local, omni=omni, free=free)
            with _patched_log(test_log):
                reply = manager.respond("free gateway regression", [])
            assert reply.provider == "freellmapi" and gateway.posts == 1
            assert gateway.auth_matches and gateway.request_path == "/v1/chat/completions"
            assert gateway.body is not None and gateway.body.get("model") == "auto"
            assert local.calls == 0
            manager.close()
        assert FREELLMAPI_DEFAULT_BASE_URL.endswith("/v1")
        assert FREELLMAPI_DEFAULT_MODEL == "auto"
        results["freellmapi_structural_regression"] = "PASS"

        # P: Deterministic COMPUTER remains outside all LLM and System-One providers.
        with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
            omni = configured_omni(omni_gateway.base_url, omni_key)
            free = configured_free(free_gateway.base_url, free_key)
            local = FakeLocalProvider()
            cloud = FakeCloudProvider()
            manager = manager_for(
                local,
                omni=omni,
                free=free,
                cloud=cloud,
                cloud_enabled=True,
            )
            systemone = CountingSystemOneManager()
            core = FridayCore(
                AssistantProfile(cloud_brain_enabled=True),
                brain_manager=manager,  # type: ignore[arg-type]
                systemone_manager=systemone,  # type: ignore[arg-type]
            )
            core._computer = FakeComputerController()  # type: ignore[assignment]
            with _patched_core_log(test_log):
                _, _, decision = core.respond("open notepad")
            assert decision.route == Route.COMPUTER
            assert omni_gateway.posts == free_gateway.posts == 0
            assert cloud.calls == local.calls == systemone.calls == 0
            core.close()
        results["computer_zero_llm_regression"] = "PASS"

        # I/J/R/S: Private requests stay local unless the caller explicitly trusts a route.
        with MockGateway(omni_key) as gateway:
            omni = configured_omni(gateway.base_url, omni_key)
            local = FakeLocalProvider()
            systemone = CountingSystemOneManager()
            manager = manager_for(
                local,
                omni=omni,
                free=FreeLLMAPIProvider.from_environment({}),
                cloud=FakeCloudProvider(),
                cloud_enabled=True,
            )
            core = FridayCore(
                AssistantProfile(cloud_brain_enabled=True),
                router=AmbiguousRouter(),  # type: ignore[arg-type]
                brain_manager=manager,  # type: ignore[arg-type]
                systemone_manager=systemone,  # type: ignore[arg-type]
            )
            with _patched_log(test_log), _patched_core_log(test_log):
                reply, _, _ = core.respond(
                    "private text",
                    request_metadata=BrainRequestMetadata(sensitivity="private"),
                )
            assert reply == "local fallback"
            assert gateway.posts == systemone.calls == 0 and local.calls == 1
            core.close()
        with MockGateway(omni_key) as gateway:
            omni = configured_omni(gateway.base_url, omni_key)
            local = FakeLocalProvider()
            manager = manager_for(
                local,
                omni=omni,
                free=FreeLLMAPIProvider.from_environment({}),
            )
            reply = manager.respond(
                "trusted private text",
                [],
                request_metadata=BrainRequestMetadata(
                    sensitivity="private",
                    trusted_remote_providers=frozenset({"omniroute"}),
                ),
            )
            assert reply.provider == "omniroute" and gateway.posts == 1
            assert local.calls == 0
            manager.close()

        cloud = CloudflareQwenProvider()
        lifecycle = LocalBrainLifecycleManager()
        assert cloud.name == "cloudflare" and cloud.is_local is False
        assert CLOUDFLARE_DEFAULT_MODEL.startswith("@cf/")
        assert lifecycle.state == LocalBrainState.OFF and not lifecycle.owns_server
        assert callable(lifecycle.begin_request) and callable(lifecycle.finish_request)
        lifecycle.close()
        laya_client = RemoteLayaClient.from_profile(False)
        laya = RemoteLayaProvider(laya_client)
        jev = HostedJevProvider.from_environment({})
        systemone = SystemOneManager(laya_provider=laya, jev_provider=jev)
        assert callable(systemone.classify_route) and systemone.status
        assert laya.available is False and jev.available is False
        results["privacy_cloudflare_qwen_systemone_regressions"] = "PASS"

        # K: Fake keys, auth headers, and provider responses are absent from app artifacts.
        log_text = test_log.read_text(encoding="utf-8") if test_log.exists() else ""
        diff = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=D:/Projects/friday-core",
                "diff",
                "HEAD",
                "--",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", errors="replace")
        candidates = [omni_key, free_key]
        configured_key = os.environ.get("FRIDAY_OMNIROUTE_API_KEY", "")
        if configured_key:
            candidates.append(configured_key)
        assert all(key not in log_text and key not in profile_text and key not in diff for key in candidates)
        assert "Authorization" not in log_text and "Bearer " not in log_text
        results["secret_safety"] = "PASS"

    print(json.dumps({"phase": "4B", "results": results}, sort_keys=True))


class _patched_log:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        import friday.brain_manager as module

        self.module = module
        self.original = module.LOG_PATH
        module.LOG_PATH = self.path

    def __exit__(self, *_args: object) -> None:
        self.module.LOG_PATH = self.original


class _patched_core_log:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        import friday.core as module

        self.module = module
        self.original = module.LOG_PATH
        module.LOG_PATH = self.path

    def __exit__(self, *_args: object) -> None:
        self.module.LOG_PATH = self.original


if __name__ == "__main__":
    main()
