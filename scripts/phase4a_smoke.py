"""Focused Phase 4A FreeLLMAPI adapter and routing checks; loopback mocks only."""

from __future__ import annotations

import json
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
    CLOUDFLARE_DEFAULT_MODEL,
    CloudflareQwenProvider,
    FREELLMAPI_DEFAULT_BASE_URL,
    FREELLMAPI_DEFAULT_MODEL,
    FREELLMAPI_DEFAULT_TIMEOUT_SECONDS,
    FreeLLMAPIProvider,
)
from friday.capabilities.registry import Risk  # noqa: E402
from friday.core import FridayCore  # noqa: E402
from friday.laya_client import RemoteLayaClient  # noqa: E402
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState  # noqa: E402
from friday.profile import AssistantProfile  # noqa: E402
from friday.router import Route  # noqa: E402
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
                if gateway.mode == "connection_error":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    self.connection.close()
                    return
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
            return [{"role": "system", "content": "test system"}, *history,
                    {"role": "user", "content": command}]

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


class FakeComputerController:
    def execute_request(self, _command: str, approved: bool = False):
        return "mocked notepad action", Risk.SAFE


def manager_for(
    provider: FreeLLMAPIProvider,
    local: FakeLocalProvider,
    *,
    cloud: FakeCloudProvider | None = None,
    cloud_enabled: bool = False,
) -> BrainManager:
    profile = AssistantProfile(cloud_brain_enabled=cloud_enabled)
    return BrainManager(
        profile,
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud,  # type: ignore[arg-type]
        freellmapi_provider=provider,
    )


def main() -> None:
    results: dict[str, str] = {}
    test_key = f"freellmapi-{secrets.token_urlsafe(32)}"
    with tempfile.TemporaryDirectory(prefix="friday-phase4a-") as temp_dir:
        test_log = Path(temp_dir) / "provider.log"
        profile_text = (ROOT / "profile.json").read_text(encoding="utf-8")
        assert test_key not in profile_text

        # A: Defaults are inert, and disabled FreeLLMAPI makes no request.
        with MockGateway(test_key) as gateway:
            provider = FreeLLMAPIProvider.from_environment({})
            assert not provider.enabled and not provider.available()
            assert provider.base_url == FREELLMAPI_DEFAULT_BASE_URL
            assert provider.model == FREELLMAPI_DEFAULT_MODEL == "auto"
            assert provider.timeout_seconds == FREELLMAPI_DEFAULT_TIMEOUT_SECONDS
            local = FakeLocalProvider()
            manager = manager_for(provider, local)
            reply = manager.respond("hello", [])
            assert reply.provider == "local" and local.calls == 1 and gateway.posts == 0
            manager.close()
        results["default_disabled_zero_gateway_calls"] = "PASS"

        # C: The official OpenAI-compatible path, Bearer auth, model, and response work.
        with MockGateway(test_key) as gateway:
            gateway_provider = FreeLLMAPIProvider.from_environment(
                {
                    "FRIDAY_FREELLMAPI_ENABLED": "true",
                    "FRIDAY_FREELLMAPI_BASE_URL": gateway.base_url,
                    "FRIDAY_FREELLMAPI_API_KEY": test_key,
                    "FRIDAY_FREELLMAPI_MODEL": "auto",
                    "FRIDAY_FREELLMAPI_TIMEOUT_SECONDS": "5",
                }
            )
            assert gateway_provider.available() and gateway_provider.health() == "CONFIGURED"
            local = FakeLocalProvider()
            manager = manager_for(gateway_provider, local)
            with _patched_log(test_log):
                reply = manager.respond("say hello", [])
            assert reply.text == "gateway reply" and reply.provider == "freellmapi"
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
            assert local.calls == 0
            manager.close()
        results["mocked_success"] = "PASS"

        # D/E: One gateway attempt; connection errors, timeout, HTTP errors, and
        # malformed responses all fall back to local Qwen without crashing.
        for mode, result_name, timeout in (
            ("connection_error", "connection_error_fallback", 1.0),
            ("timeout", "timeout_fallback", 0.1),
            ("429", "429_fallback", 1.0),
            ("500", "500_fallback", 1.0),
            ("malformed", "malformed_response_fallback", 1.0),
        ):
            with MockGateway(test_key) as gateway:
                gateway.mode = mode
                provider = FreeLLMAPIProvider(
                    enabled=True,
                    base_url=gateway.base_url,
                    api_key=test_key,
                    timeout_seconds=timeout,
                )
                local = FakeLocalProvider()
                manager = manager_for(provider, local)
                with _patched_log(test_log):
                    reply = manager.respond("say hello", [])
                assert reply.provider == "local" and local.calls == 1
                assert gateway.posts == 1
                manager.close()
            results[result_name] = "PASS"

        # Provider priority: explicitly enabled Cloudflare stays ahead of FreeLLMAPI.
        with MockGateway(test_key) as gateway:
            free = FreeLLMAPIProvider(
                enabled=True, base_url=gateway.base_url, api_key=test_key
            )
            local = FakeLocalProvider()
            cloud = FakeCloudProvider(succeeds=True)
            manager = manager_for(free, local, cloud=cloud, cloud_enabled=True)
            with _patched_log(test_log):
                reply = manager.respond("priority check", [])
            assert reply.provider == "cloudflare" and cloud.calls == 1
            assert gateway.posts == 0 and local.calls == 0
            manager.close()

        # B: A deterministic COMPUTER request bypasses Cloudflare, FreeLLMAPI,
        # local Qwen, and System-One. A fake controller avoids launching Notepad.
        with MockGateway(test_key) as gateway:
            free = FreeLLMAPIProvider(
                enabled=True, base_url=gateway.base_url, api_key=test_key
            )
            local = FakeLocalProvider()
            cloud = FakeCloudProvider()
            manager = manager_for(free, local, cloud=cloud, cloud_enabled=True)
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
            assert gateway.posts == cloud.calls == local.calls == systemone.calls == 0
            core.close()
        results["computer_zero_llm_and_systemone"] = "PASS"

        # F/H: Existing Cloudflare and local lifecycle contracts remain importable
        # and idle; System-One interfaces still construct without network probes.
        cloud = CloudflareQwenProvider()
        local = LocalBrainLifecycleManager()
        assert cloud.name == "cloudflare" and cloud.model
        assert CLOUDFLARE_DEFAULT_MODEL.startswith("@cf/")
        assert local.state == LocalBrainState.OFF and not local.owns_server
        assert callable(local.begin_request) and callable(local.finish_request)
        local.close()
        laya = RemoteLayaProvider(RemoteLayaClient.from_profile(False))
        jev = HostedJevProvider.from_environment({})
        systemone = SystemOneManager(laya_provider=laya, jev_provider=jev)
        assert callable(systemone.classify_route) and systemone.status
        results["cloudflare_local_qwen_systemone_regressions"] = "PASS"

        # G: Random runtime key never reaches application logs, profile.json, or git diff.
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
        assert test_key not in log_text and test_key not in profile_text and test_key not in diff
        assert "Authorization" not in log_text and "Bearer " not in log_text
        results["secret_safety"] = "PASS"

    print(json.dumps({"phase": "4A", "results": results}, sort_keys=True))


class _patched_log:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.original = None

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
