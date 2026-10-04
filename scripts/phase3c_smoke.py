"""Targeted, model-free Phase 3C checks using a local HTTP contract mock."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

import friday.core as core_module
from friday.brain import API_BASE
from friday.brain_manager import BrainReply
from friday.core import FridayCore
from friday.laya_client import RemoteLayaClient
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState
from friday.profile import AssistantProfile
from friday.router import Route
from friday.capabilities.registry import Risk


TEST_API_KEY = "phase3c-secret-sentinel-do-not-log"
COMMAND = "research a Python bug"


class MockLayaService:
    def __init__(self, mode: str = "valid") -> None:
        self.state = SimpleNamespace(
            mode=mode,
            get_count=0,
            post_count=0,
            last_path="",
            last_body=None,
            auth_present_on_health=False,
            auth_matches=False,
            lock=threading.Lock(),
        )

        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                with state.lock:
                    state.get_count += 1
                    state.last_path = self.path
                    state.auth_present_on_health = self.headers.get("Authorization") is not None
                self._send({"status": "ok"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                with state.lock:
                    state.post_count += 1
                    state.last_path = self.path
                    state.last_body = json.loads(raw.decode("utf-8"))
                    state.auth_matches = self.headers.get("Authorization") == f"Bearer {TEST_API_KEY}"
                if state.mode == "timeout":
                    time.sleep(0.3)
                if state.mode == "malformed":
                    payload = {
                        "model": "laya-rl-agent",
                        "answers": {"route": {
                            "type": "choice",
                            "choice": "UNRECOGNIZED",
                            "probabilities": {"UNRECOGNIZED": 0.99},
                            "confidence": 0.99,
                        }},
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    }
                else:
                    payload = {
                        "model": "laya-rl-agent",
                        "answers": {"route": {
                            "type": "choice",
                            "choice": "RESEARCH",
                            "probabilities": {"GENERAL": 0.05, "CODING": 0.15, "RESEARCH": 0.80},
                            "confidence": 0.72,
                        }},
                        "usage": {"input_tokens": 8, "output_tokens": 0},
                    }
                self._send(payload)

            def _send(self, payload: dict[str, object]) -> None:
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> MockLayaService:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class CountingBrainManager:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *_args: object, **_kwargs: object) -> BrainReply:
        self.calls += 1
        return BrainReply("mock brain reply", provider="test")

    def close(self) -> None:
        return


class FakeComputerController:
    def __init__(self) -> None:
        self.calls = 0

    def execute_request(self, _command: str, approved: bool = False) -> tuple[str, Risk]:
        self.calls += 1
        return "mock computer action", Risk.SAFE


def make_client(service: MockLayaService, *, enabled: bool = True, timeout: float = 1.0) -> RemoteLayaClient:
    return RemoteLayaClient(
        enabled=enabled,
        base_url=service.base_url,
        api_key=TEST_API_KEY,
        timeout_seconds=timeout,
        opener=build_opener(ProxyHandler({})),
    )


def run_with_log(core: FridayCore, command: str, log_path: Path):
    previous = core_module.LOG_PATH
    core_module.LOG_PATH = log_path
    try:
        return core.respond(command)
    finally:
        core_module.LOG_PATH = previous


def main() -> None:
    results: dict[str, str] = {}
    profile = AssistantProfile()
    if profile.laya_enabled:
        raise AssertionError("Laya must default to disabled.")

    with tempfile.TemporaryDirectory(prefix="friday-phase3c-") as temp_dir:
        log_path = Path(temp_dir) / "friday-test.log"

        # A: explicit disabled mode must not probe or classify remotely.
        with MockLayaService() as service:
            brain = CountingBrainManager()
            laya = make_client(service, enabled=False)
            core = FridayCore(profile, brain_manager=brain, laya_client=laya)
            _, _, decision = run_with_log(core, COMMAND, log_path)
            assert decision.route == Route.CODING and decision.source == "builtin"
            assert brain.calls == 1 and service.state.post_count == 0 and service.state.get_count == 0
            assert core.laya_status == "disabled"
            results["disabled"] = "PASS"

        # Ordinary conversation remains on BrainManager even when Laya is configured.
        with MockLayaService() as service:
            brain = CountingBrainManager()
            core = FridayCore(profile, brain_manager=brain, laya_client=make_client(service))
            _, _, decision = run_with_log(core, "Who are you?", log_path)
            assert decision.route == Route.GENERAL and decision.source == "builtin"
            assert brain.calls == 1 and service.state.post_count == 0 and service.state.get_count == 0
            results["ordinary_general_bypass"] = "PASS"

        # B: a deterministic app action bypasses both Laya and BrainManager.
        with MockLayaService() as service:
            brain = CountingBrainManager()
            computer = FakeComputerController()
            core = FridayCore(profile, brain_manager=brain, laya_client=make_client(service))
            core._computer = computer
            _, _, decision = run_with_log(core, "open notepad", log_path)
            assert decision.route == Route.COMPUTER
            assert brain.calls == 0 and computer.calls == 1
            assert service.state.post_count == 0 and service.state.get_count == 0
            results["computer_bypass"] = "PASS"

        # C/F: verify the official health and System-1 contract, auth header, minimal state,
        # and that neither the bearer nor unrelated conversation history is logged/sent.
        with MockLayaService() as service:
            brain = CountingBrainManager()
            laya = make_client(service)
            assert laya.health() is True
            core = FridayCore(profile, brain_manager=brain, laya_client=laya)
            core._history.add_exchange("old unrelated history", "private history marker")
            _, _, decision = run_with_log(core, COMMAND, log_path)
            request = service.state.last_body
            assert decision.route == Route.RESEARCH and decision.source == "laya"
            assert brain.calls == 1 and service.state.get_count == 1 and service.state.post_count == 1
            assert service.state.last_path == "/v1/systemone"
            assert service.state.auth_matches and not service.state.auth_present_on_health
            assert isinstance(request, dict) and request.get("state") == COMMAND
            assert "questions" in request and "messages" not in request
            serialized_request = json.dumps(request)
            assert "private history marker" not in serialized_request
            assert TEST_API_KEY not in serialized_request
            assert TEST_API_KEY not in log_path.read_text(encoding="utf-8")
            assert core.laya_status == "healthy"
            results["healthy_laya"] = "PASS"
            results["secret_hygiene"] = "PASS"

            # Credential-like command content must remain local even with remote mode enabled.
            before = service.state.post_count
            sensitive = f"research a Python bug using api_key={TEST_API_KEY}"
            _, _, sensitive_decision = run_with_log(core, sensitive, log_path)
            assert service.state.post_count == before
            assert sensitive_decision.source == "builtin"
            assert TEST_API_KEY not in log_path.read_text(encoding="utf-8")

        # D: request timeout abstains to the original deterministic route and brain.
        with MockLayaService(mode="timeout") as service:
            brain = CountingBrainManager()
            laya = make_client(service, timeout=0.1)
            core = FridayCore(profile, brain_manager=brain, laya_client=laya)
            _, _, decision = run_with_log(core, COMMAND, log_path)
            assert decision.route == Route.CODING and decision.source == "builtin"
            assert brain.calls == 1 and service.state.post_count == 1, (
                brain.calls,
                service.state.post_count,
                laya.status,
            )
            assert laya.status == "offline"
            results["timeout_fallback"] = "PASS"

        # E: malformed route labels are rejected and use the same local fallback.
        with MockLayaService(mode="malformed") as service:
            brain = CountingBrainManager()
            laya = make_client(service)
            core = FridayCore(profile, brain_manager=brain, laya_client=laya)
            _, _, decision = run_with_log(core, COMMAND, log_path)
            assert decision.route == Route.CODING and decision.source == "builtin"
            assert brain.calls == 1 and service.state.post_count == 1
            assert laya.status == "invalid_response"
            results["malformed_fallback"] = "PASS"

    # G: import and OFF/ownership/idle invariants from the Phase 3B lifecycle remain intact.
    lifecycle = LocalBrainLifecycleManager(idle_seconds=2)
    assert lifecycle.state == LocalBrainState.OFF and not lifecycle.owns_server
    assert API_BASE.startswith("http://127.0.0.1:")
    lifecycle.close()
    assert lifecycle.state == LocalBrainState.OFF and not lifecycle.owns_server
    results["phase3b_lifecycle"] = "PASS"

    print(json.dumps({"phase": "3C", "results": results}, sort_keys=True))


if __name__ == "__main__":
    main()
