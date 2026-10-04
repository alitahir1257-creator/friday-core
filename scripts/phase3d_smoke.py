"""Targeted Phase 3D System-One provider and fallback checks; uses loopback mocks only."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.brain_manager import (  # noqa: E402
    BrainManager,
    BrainReply,
    CLOUDFLARE_DEFAULT_MODEL,
    CloudflareQwenProvider,
)
from friday.capabilities.registry import Risk  # noqa: E402
from friday.core import FridayCore  # noqa: E402
from friday.laya_client import RemoteLayaClient  # noqa: E402
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState  # noqa: E402
from friday.profile import AssistantProfile  # noqa: E402
from friday.router import Route  # noqa: E402
from friday.system_one import (  # noqa: E402
    JEV_DEFAULT_ENDPOINT,
    JEV_DEFAULT_ENABLED,
    JEV_DEFAULT_MODEL,
    HostedJevProvider,
    RemoteLayaProvider,
    SystemOneManager,
)
from scripts.phase3c_smoke import CountingBrainManager  # noqa: E402


AMBIGUOUS_COMMAND = "research a Python bug"
CHOICE_INSTRUCTIONS = (
    "Choose the best FRIDAY processing category for this request. "
    "This is only a routing hint; do not execute actions."
)
CURRENT_STAGE = "setup"


class MockSystemOne:
    def __init__(
        self,
        *,
        mode: str = "valid",
        expected_key: str = "",
        route: str = "RESEARCH",
        probability: float = 0.85,
        confidence: float | None = None,
    ) -> None:
        self.mode = mode
        self.expected_key = expected_key
        self.route = route
        self.probability = probability
        self.confidence = confidence
        self.post_count = 0
        self.auth_matches = False
        self.auth_present = False
        self.last_path = ""
        self.last_body: dict[str, Any] | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}/v1/systemone"
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"

    def _handler_type(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                state.post_count += 1
                state.last_path = self.path
                authorization = self.headers.get("Authorization", "")
                state.auth_present = bool(authorization)
                state.auth_matches = authorization == f"Bearer {state.expected_key}"
                length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
                try:
                    state.last_body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    state.last_body = None

                if state.mode == "timeout":
                    time.sleep(0.5)
                if state.mode == "unauthorized":
                    self._send(401, {"detail": "unauthorized"})
                    return
                if state.mode == "server_error":
                    self._send(500, {"detail": "server error"})
                    return
                if state.mode == "malformed":
                    self._send(200, {"answers": {"route": {"type": "choice", "choice": "NO_SUCH_ROUTE"}}})
                    return

                labels = list((state.last_body or {}).get("questions", {}).get("route", {}).get("criteria", {}))
                probabilities = {label: 0.02 for label in labels}
                if state.route in probabilities:
                    probabilities[state.route] = state.probability
                payload = {
                    "answers": {
                        "route": {
                            "type": "choice",
                            "choice": state.route,
                            "probabilities": probabilities,
                            "confidence": (
                                state.confidence
                                if state.confidence is not None
                                else state.probability
                            ),
                        }
                    },
                    "model": "jev-latest",
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                }
                self._send(200, payload)

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

    def __enter__(self) -> MockSystemOne:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class FakeComputerController:
    def __init__(self) -> None:
        self.calls = 0

    def execute_request(self, _command: str, approved: bool = False) -> tuple[str, Risk]:
        self.calls += 1
        return "mock computer action", Risk.SAFE


class RecordingSystemOneManager(SystemOneManager):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.last_decision = None

    def classify_route(self, command: str, candidates):
        self.last_decision = super().classify_route(command, candidates)
        return self.last_decision


class FakeLocalProvider:
    class Brain:
        @staticmethod
        def messages(command: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
            return [{"role": "user", "content": command}, *history]

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
        return BrainReply("local test answer", provider="local")

    def close(self) -> None:
        return


class CloudCallSpy:
    name = "cloudflare"
    is_local = False

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "CONFIGURED"

    def chat(self, _messages: list[dict[str, str]]) -> BrainReply:
        self.calls += 1
        raise AssertionError("Cloudflare must stay disabled in this focused regression.")


def make_jev(service: MockSystemOne, key: str, *, timeout: float = 1.0) -> HostedJevProvider:
    return HostedJevProvider(
        enabled=True,
        base_url=service.endpoint,
        api_key=key,
        model=JEV_DEFAULT_MODEL,
        timeout_seconds=timeout,
        opener=build_opener(ProxyHandler({})),
    )


def make_laya(service: MockSystemOne, key: str, *, enabled: bool = True, timeout: float = 1.0) -> RemoteLayaClient:
    return RemoteLayaClient(
        enabled=enabled,
        base_url=service.base_url,
        api_key=key,
        timeout_seconds=timeout,
        opener=build_opener(ProxyHandler({})),
    )


def run_core(
    profile: AssistantProfile,
    brain: CountingBrainManager,
    laya: RemoteLayaClient,
    manager: SystemOneManager,
    log_path: Path,
    command: str = AMBIGUOUS_COMMAND,
    seed_private_history: bool = False,
) -> tuple[FridayCore, Any]:
    import friday.core as core_module

    previous_log = core_module.LOG_PATH
    core_module.LOG_PATH = log_path
    try:
        core = FridayCore(
            profile,
            brain_manager=brain,
            laya_client=laya,
            systemone_manager=manager,
        )
        if seed_private_history:
            core._history.add_exchange("unrelated prior turn", "history must stay local")
        return core, core.respond(command)
    finally:
        core_module.LOG_PATH = previous_log


def main() -> None:
    global CURRENT_STAGE
    results: dict[str, str] = {}
    profile = AssistantProfile()
    used_test_keys: list[str] = []
    api_key = secrets.token_urlsafe(24)
    laya_key = secrets.token_urlsafe(24)
    env_key = secrets.token_urlsafe(24)
    used_test_keys.extend((api_key, laya_key, env_key))

    assert JEV_DEFAULT_ENABLED is False
    assert JEV_DEFAULT_ENDPOINT == "https://api.typesafe.ai/v1/systemone"
    assert JEV_DEFAULT_MODEL == "jev-latest"
    assert not HostedJevProvider.from_environment({}).available
    assert HostedJevProvider.from_environment({}).status == "disabled"
    environment_provider = HostedJevProvider.from_environment(
        {
            "FRIDAY_JEV_ENABLED": "true",
            "FRIDAY_JEV_BASE_URL": JEV_DEFAULT_ENDPOINT,
            "FRIDAY_JEV_API_KEY": env_key,
            "FRIDAY_JEV_MODEL": JEV_DEFAULT_MODEL,
            "FRIDAY_JEV_TIMEOUT_SECONDS": "5",
        }
    )
    assert environment_provider.available and environment_provider.model == JEV_DEFAULT_MODEL

    with tempfile.TemporaryDirectory(prefix="friday-phase3d-") as temp_dir:
        temp_path = Path(temp_dir)
        log_path = temp_path / "telemetry.log"

        # A: Jev is disabled by default and cannot make a request.
        CURRENT_STAGE = "jev_disabled"
        with MockSystemOne(expected_key=api_key) as service:
            disabled = HostedJevProvider(
                enabled=False,
                base_url=service.endpoint,
                api_key=api_key,
                opener=build_opener(ProxyHandler({})),
            )
            brain = CountingBrainManager()
            laya = make_laya(service, laya_key, enabled=False)
            manager = SystemOneManager(
                laya_provider=RemoteLayaProvider(laya), jev_provider=disabled
            )
            core, (_, _, decision) = run_core(profile, brain, laya, manager, log_path)
            core.close()
            assert decision.source == "builtin" and decision.route == Route.CODING
            assert brain.calls == 1 and service.post_count == 0
            results["jev_disabled"] = "PASS"

        # B: Deterministic COMPUTER actions skip Jev, Laya, and BrainManager.
        CURRENT_STAGE = "computer_bypass"
        with MockSystemOne(expected_key=api_key) as jev_service, MockSystemOne(
            expected_key=laya_key
        ) as laya_service:
            jev = make_jev(jev_service, api_key)
            laya = make_laya(laya_service, laya_key)
            brain = CountingBrainManager()
            manager = SystemOneManager(
                laya_provider=RemoteLayaProvider(laya), jev_provider=jev
            )
            core = FridayCore(
                profile,
                brain_manager=brain,
                laya_client=laya,
                systemone_manager=manager,
            )
            computer = FakeComputerController()
            core._computer = computer
            import friday.core as core_module

            previous_log = core_module.LOG_PATH
            core_module.LOG_PATH = log_path
            try:
                _, _, decision = core.respond("open notepad")
            finally:
                core_module.LOG_PATH = previous_log
                core.close()
            assert decision.route == Route.COMPUTER and computer.calls == 1
            assert brain.calls == 0
            assert jev_service.post_count == 0 and laya_service.post_count == 0
            results["computer_bypass"] = "PASS"

        # C: Hosted Jev's shared typed choice contract and auth header are exercised locally.
        CURRENT_STAGE = "jev_mock_success"
        with MockSystemOne(
            expected_key=api_key, probability=0.85, confidence=0.72
        ) as service:
            jev = make_jev(service, api_key)
            laya = make_laya(service, laya_key, enabled=False)
            brain = CountingBrainManager()
            manager = RecordingSystemOneManager(
                laya_provider=RemoteLayaProvider(laya), jev_provider=jev
            )
            core, (_, _, decision) = run_core(
                profile, brain, laya, manager, log_path, seed_private_history=True
            )
            core.close()
            body = service.last_body or {}
            route_question = body.get("questions", {}).get("route", {})
            assert decision.route == Route.RESEARCH and decision.source == "jev"
            assert brain.calls == 1 and service.post_count == 1
            assert service.last_path == "/v1/systemone" and service.auth_matches
            assert body.get("model") == "jev-latest"
            assert body.get("state") == AMBIGUOUS_COMMAND
            assert route_question.get("type") == "choice"
            assert route_question.get("instructions") == CHOICE_INSTRUCTIONS
            assert "messages" not in body and "history must stay local" not in json.dumps(body)
            assert manager.last_decision is not None
            assert manager.last_decision.probability == 0.85
            assert manager.last_decision.confidence == 0.72
            assert jev.last_http_status == 200
            results["jev_mock_success"] = "PASS"

        # D: Laya continues through SystemOneManager and retains priority over Jev.
        CURRENT_STAGE = "laya_compatibility"
        with MockSystemOne(expected_key=laya_key) as laya_service, MockSystemOne(
            expected_key=api_key
        ) as jev_service:
            laya = make_laya(laya_service, laya_key)
            jev = make_jev(jev_service, api_key)
            brain = CountingBrainManager()
            manager = SystemOneManager(
                laya_provider=RemoteLayaProvider(laya), jev_provider=jev
            )
            core, (_, _, decision) = run_core(profile, brain, laya, manager, log_path)
            core.close()
            assert decision.route == Route.RESEARCH and decision.source == "laya"
            assert laya_service.post_count == 1 and laya_service.auth_matches
            assert jev_service.post_count == 0
            assert laya_service.last_path == "/v1/systemone"
            assert core.laya_status == "healthy"
            results["laya_compatibility_and_priority"] = "PASS"

        # E/F: Timeout and HTTP errors abstain once, then FRIDAY's existing route/brain continues.
        CURRENT_STAGE = "timeout_and_http_errors"
        for mode, timeout, expected_status in (
            ("timeout", 0.2, None),
            ("unauthorized", 1.0, 401),
            ("server_error", 1.0, 500),
        ):
            CURRENT_STAGE = f"provider_fallback_{mode}"
            with MockSystemOne(mode=mode, expected_key=api_key) as service:
                jev = make_jev(service, api_key, timeout=timeout)
                laya = make_laya(service, laya_key, enabled=False)
                brain = CountingBrainManager()
                manager = SystemOneManager(
                    laya_provider=RemoteLayaProvider(laya), jev_provider=jev
                )
                core, (_, _, decision) = run_core(profile, brain, laya, manager, log_path)
                core.close()
                CURRENT_STAGE = (
                    f"provider_fallback_{mode}_{decision.route.value}_{decision.source}_"
                    f"b{brain.calls}_p{service.post_count}_s{jev.last_http_status}_e{expected_status}"
                )
                assert decision.route == Route.CODING and decision.source == "builtin", (
                    mode, decision.route.value, decision.source, brain.calls
                )
                assert brain.calls == 1 and service.post_count == 1, (
                    mode, brain.calls, service.post_count
                )
                assert jev.last_http_status == expected_status, (
                    mode, jev.last_http_status, expected_status
                )
        results["timeout_and_http_error_fallback"] = "PASS"

        # G/H: Malformed, low-confidence, and consequential COMPUTER labels are rejected.
        CURRENT_STAGE = "malformed_confidence_computer"
        for mode, probability in (("malformed", 0.85), ("valid", 0.4)):
            with MockSystemOne(mode=mode, expected_key=api_key, probability=probability) as service:
                jev = make_jev(service, api_key)
                laya = make_laya(service, laya_key, enabled=False)
                brain = CountingBrainManager()
                manager = SystemOneManager(
                    laya_provider=RemoteLayaProvider(laya), jev_provider=jev
                )
                core, (_, _, decision) = run_core(profile, brain, laya, manager, log_path)
                core.close()
                assert decision.route == Route.CODING and decision.source == "builtin"
                assert brain.calls == 1 and service.post_count == 1

        with MockSystemOne(expected_key=api_key, route="COMPUTER", probability=0.95) as service:
            jev = make_jev(service, api_key)
            manager = SystemOneManager(jev_provider=jev)
            choice = manager.classify_route(
                AMBIGUOUS_COMMAND, (Route.GENERAL, Route.CODING, Route.COMPUTER)
            )
            assert choice is None and service.post_count == 1
        results["malformed_confidence_and_computer_safety"] = "PASS"

        # J: Phase 3B providers and lifecycle contracts remain unchanged and local-first.
        CURRENT_STAGE = "phase3b_regression"
        fake_local = FakeLocalProvider()
        cloud = CloudCallSpy()
        brain_manager = BrainManager(
            profile,
            local_provider=fake_local,  # type: ignore[arg-type]
            cloud_provider=cloud,  # type: ignore[arg-type]
        )
        reply = brain_manager.respond("local test", [])
        brain_manager.close()
        configured_cloud = CloudflareQwenProvider()
        lifecycle = LocalBrainLifecycleManager()
        lifecycle_ok = (
            lifecycle.state == LocalBrainState.OFF
            and not lifecycle.owns_server
            and callable(lifecycle.begin_request)
            and callable(lifecycle.finish_request)
            and callable(lifecycle.close)
        )
        lifecycle.close()
        assert reply.provider == "local" and fake_local.calls == 1 and cloud.calls == 0
        assert profile.cloud_brain_enabled is False
        assert configured_cloud.model == CLOUDFLARE_DEFAULT_MODEL and lifecycle_ok
        results["phase3b_regression"] = "PASS"

        # I: Runtime-generated test keys stay out of telemetry, profile, and the current diff.
        CURRENT_STAGE = "secret_safety"
        profile_text = (ROOT / "profile.json").read_text(encoding="utf-8")
        telemetry = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        diff = subprocess.run(
            ["git", "-c", "safe.directory=D:/Projects/friday-core", "diff", "--"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", errors="replace")
        configured_env_key = os.environ.get("FRIDAY_JEV_API_KEY", "")
        secrets_absent = all(
            key not in profile_text and key not in telemetry and key not in diff
            for key in (*used_test_keys, *((configured_env_key,) if configured_env_key else ()))
        )
        authorization_absent = not re.search(
            r"(?im)^\s*authorization\s*:|\bbearer\s+\S+", telemetry
        )
        assert secrets_absent and authorization_absent
        results["secret_safety"] = "PASS"

    print(json.dumps({"phase": "3D", "results": results}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"phase": "3D", "failed_stage": CURRENT_STAGE, "fatal_error": type(error).__name__}), flush=True)
        raise SystemExit(1)
