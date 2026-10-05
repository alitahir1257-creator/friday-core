"""Phase 4D policy and bounded-routing checks; real calls require --real-only."""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import friday.brain_manager as brain_manager_module  # noqa: E402
import friday.core as core_module  # noqa: E402
from friday.brain import MODEL_NAME  # noqa: E402
from friday.brain_manager import (  # noqa: E402
    BrainManager,
    BrainReply,
    BrainRequestMetadata,
    CLOUDFLARE_DEFAULT_MODEL,
    CloudflareQwenProvider,
    FreeLLMAPIProvider,
    OmniRouteProvider,
    _reply_from_payload,
)
from friday.capabilities.registry import Risk  # noqa: E402
from friday.core import FridayCore  # noqa: E402
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState  # noqa: E402
from friday.model_policy import (  # noqa: E402
    FAST_FALLBACK_MODEL_DEFAULT,
    FAST_MODEL_DEFAULT,
    SMART_FALLBACK_MODEL_DEFAULT,
    SMART_MODEL_DEFAULT,
    ModelPolicy,
    ModelPolicyConfig,
    ModelPolicyEngine,
    ModelSelection,
    ModelTarget,
    PrivacyLevel,
)
from friday.profile import AssistantProfile  # noqa: E402
from friday.router import Route, RoutingDecision  # noqa: E402
from friday.system_one import HostedJevProvider, RemoteLayaClient, RemoteLayaProvider, SystemOneManager  # noqa: E402


class MockGateway:
    def __init__(
        self,
        key: str,
        *,
        mode: str = "success",
        text: str = "mock reply",
        response_model: str | None = None,
    ) -> None:
        self.key = key
        self.mode = mode
        self.text = text
        self.response_model = response_model
        self.posts = 0
        self.auth_matches = False
        self.models: list[str] = []
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
                gateway.auth_matches = self.headers.get("Authorization", "") == (
                    f"Bearer {gateway.key}"
                )
                length = min(int(self.headers.get("Content-Length", "0")), 128 * 1024)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    model = body.get("model") if isinstance(body, dict) else None
                    gateway.models.append(model if isinstance(model, str) else "")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    gateway.models.append("")
                if gateway.mode != "success":
                    self._send(503, {"error": {"message": "mock provider failure"}})
                    return
                self._send(
                    200,
                    {
                        "choices": [{"message": {"role": "assistant", "content": gateway.text}}],
                        "model": gateway.response_model or gateway.models[-1],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
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
    name = "local"
    is_local = True

    class Brain:
        @staticmethod
        def messages(command: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
            return [{"role": "system", "content": "test"}, *history, {"role": "user", "content": command}]

    class Lifecycle:
        @staticmethod
        def health() -> bool:
            return False

    def __init__(self, *, fails: bool = False) -> None:
        self.brain = self.Brain()
        self.lifecycle = self.Lifecycle()
        self.calls = 0
        self.fails = fails

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "READY"

    def chat(self, _messages: list[dict[str, str]]) -> BrainReply:
        self.calls += 1
        if self.fails:
            raise RuntimeError("mock local failure")
        return BrainReply("local reply", provider=self.name, model=MODEL_NAME)

    def close(self) -> None:
        return


class FakeCloudflareProvider:
    name = "cloudflare"
    is_local = False
    model = "approved-cloudflare-model"

    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "CONFIGURED"

    def chat(self, _messages: list[dict[str, str]]) -> BrainReply:
        self.calls += 1
        if not self.succeeds:
            raise RuntimeError("mock Cloudflare failure")
        return BrainReply("cloudflare reply", provider=self.name, model=self.model)


class CountingSystemOneManager:
    status = "DISABLED"

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
        return "mocked deterministic action", Risk.SAFE


class FakeCloudflareHTTP:
    def __init__(self) -> None:
        self.posts = 0
        self.model = ""
        self.auth_present = False

    def open(self, request, timeout: float):
        self.posts += 1
        self.auth_present = bool(request.get_header("Authorization"))
        payload = json.loads(request.data.decode("utf-8"))
        self.model = payload["model"]
        response = {
            "choices": [{"message": {"role": "assistant", "content": "cloudflare mock"}}],
            "model": self.model,
        }
        return io.BytesIO(json.dumps(response).encode("utf-8"))


class PatchedLogs:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        self.previous_brain_log = brain_manager_module.LOG_PATH
        self.previous_core_log = core_module.LOG_PATH
        brain_manager_module.LOG_PATH = self.path
        core_module.LOG_PATH = self.path
        return self

    def __exit__(self, *_args: object) -> None:
        brain_manager_module.LOG_PATH = self.previous_brain_log
        core_module.LOG_PATH = self.previous_core_log


def manager_for(
    local: FakeLocalProvider,
    *,
    omni: OmniRouteProvider | None = None,
    free: FreeLLMAPIProvider | None = None,
    cloud: Any = None,
    cloud_enabled: bool = False,
) -> BrainManager:
    return BrainManager(
        AssistantProfile(cloud_brain_enabled=cloud_enabled),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud,
        freellmapi_provider=free or FreeLLMAPIProvider.from_environment({}),
        omniroute_provider=omni or OmniRouteProvider.from_environment({}),
    )


def core_for(
    manager: BrainManager,
    systemone: CountingSystemOneManager | None = None,
    *,
    router: Any = None,
) -> FridayCore:
    return FridayCore(
        AssistantProfile(cloud_brain_enabled=manager.cloud_enabled),
        router=router,
        brain_manager=manager,
        systemone_manager=systemone or CountingSystemOneManager(),  # type: ignore[arg-type]
        model_policy_engine=ModelPolicyEngine(ModelPolicyConfig()),
    )


def omni_for(gateway: MockGateway, key: str) -> OmniRouteProvider:
    return OmniRouteProvider(enabled=True, base_url=gateway.base_url, api_key=key, model="auto", timeout_seconds=2)


def free_for(gateway: MockGateway, key: str) -> FreeLLMAPIProvider:
    return FreeLLMAPIProvider(enabled=True, base_url=gateway.base_url, api_key=key, model="auto", timeout_seconds=2)


def run_mocks() -> dict[str, str]:
    results: dict[str, str] = {}
    omni_key = secrets.token_urlsafe(24)
    free_key = secrets.token_urlsafe(24)
    private_marker = "PHASE4D_PRIVATE_BODY_SENTINEL"

    with tempfile.TemporaryDirectory(prefix="friday-phase4d-") as temp_dir:
        log_path = Path(temp_dir) / "route.log"
        with PatchedLogs(log_path):
            # A: deterministic COMPUTER bypasses every model and System-One provider.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                cloud = FakeCloudflareProvider()
                systemone = CountingSystemOneManager()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                    cloud=cloud,
                    cloud_enabled=True,
                )
                core = core_for(manager, systemone)
                core._computer = FakeComputerController()  # type: ignore[assignment]
                _, _, decision = core.respond("open notepad")
                assert decision.route == Route.COMPUTER
                assert core.last_model_selection is not None
                assert core.last_model_selection.policy == ModelPolicy.COMPUTER
                assert omni_gateway.posts == free_gateway.posts == 0
                assert cloud.calls == local.calls == systemone.calls == 0
                core.close()
            results["computer_no_llm"] = "PASS"

            # B: simple rewrite selects FAST's exact preferred free model.
            with MockGateway(
                omni_key, response_model="gemini-3.1-flash-lite"
            ) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key), free=free_for(free_gateway, free_key))
                core = core_for(manager)
                core.respond("Rewrite this sentence more clearly: The app is very fast.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.FAST
                assert selection.preferred_backend == "omniroute"
                assert selection.preferred_model == FAST_MODEL_DEFAULT
                assert selection.selected_model == FAST_MODEL_DEFAULT
                assert omni_gateway.models == [FAST_MODEL_DEFAULT]
                assert free_gateway.posts == local.calls == 0
                core.close()
            results["fast_classification_and_model"] = "PASS"

            # C: architecture tradeoffs are SMART, not research just because they compare.
            with MockGateway(omni_key) as omni_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key))
                core = core_for(manager)
                core.respond("Compare two alternative architectures for a local-first desktop AI assistant and explain the tradeoffs.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.SMART
                assert selection.preferred_model == SMART_MODEL_DEFAULT
                assert selection.selected_model == SMART_MODEL_DEFAULT
                assert omni_gateway.models == [SMART_MODEL_DEFAULT]
                core.close()
            results["smart_classification_and_model"] = "PASS"

            # D: repository edits are explicitly handed to Codex, with no chat fallback.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key), free=free_for(free_gateway, free_key))
                core = core_for(manager)
                answer, _, decision = core.respond("Fix the failing unit tests in my repository.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.CODING
                assert selection.preferred_backend == "codex" and decision.route == Route.CODING
                assert "Codex" in answer
                assert omni_gateway.posts == free_gateway.posts == local.calls == 0
                core.close()
            results["coding_codex_handoff"] = "PASS"

            # E: RESEARCH is capability metadata over the SMART route; no web access.
            with MockGateway(omni_key) as omni_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key))
                core = core_for(manager)
                core.respond("Research the latest developments in local AI agents and summarize the sources.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.RESEARCH
                assert selection.preferred_model == SMART_MODEL_DEFAULT
                assert selection.selected_backend == "omniroute"
                assert omni_gateway.models == [SMART_MODEL_DEFAULT]
                core.close()
            results["research_uses_smart_no_web"] = "PASS"

            # F/G: privacy wins over task complexity and bypasses all remote systems.
            for label, text in (
                ("private", f"{private_marker}: summarize my private financial document"),
                ("private_hard_task", "Compare several architectures using my confidential contract"),
            ):
                with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                    local = FakeLocalProvider()
                    cloud = FakeCloudflareProvider()
                    systemone = CountingSystemOneManager()
                    manager = manager_for(
                        local,
                        omni=omni_for(omni_gateway, omni_key),
                        free=free_for(free_gateway, free_key),
                        cloud=cloud,
                        cloud_enabled=True,
                    )
                    core = core_for(manager, systemone, router=AmbiguousRouter())
                    core.respond(text, request_metadata=BrainRequestMetadata(privacy="private"))
                    selection = core.last_model_selection
                    assert selection is not None and selection.policy == ModelPolicy.PRIVATE
                    assert selection.privacy == PrivacyLevel.PRIVATE
                    assert selection.selected_backend == "local"
                    assert omni_gateway.posts == free_gateway.posts == 0
                    assert cloud.calls == systemone.calls == 0 and local.calls == 1
                    core.close()
                results[label] = "PASS"

            # A hand-built selection cannot smuggle a remote target past PRIVATE.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                )
                forged = ModelSelection(
                    ModelPolicy.PRIVATE,
                    PrivacyLevel.PUBLIC,
                    "caller supplied metadata",
                    "omniroute",
                    FAST_MODEL_DEFAULT,
                    (ModelTarget("freellmapi"),),
                )
                reply = manager.respond(
                    "Summarize my private financial document.", [], model_selection=forged
                )
                assert reply.policy == ModelPolicy.PRIVATE and reply.provider == "local"
                assert omni_gateway.posts == free_gateway.posts == 0 and local.calls == 1
                manager.close()
            results["caller_selection_cannot_bypass_private"] = "PASS"

            # Explicit local_only also fails closed and ignores legacy trust grants.
            metadata = BrainRequestMetadata(
                sensitivity="public",
                privacy="local_only",
                trusted_remote_providers=frozenset({"omniroute", "freellmapi", "cloudflare"}),
            )
            assert metadata.effective_privacy == "local_only" and not metadata.allows_remote("omniroute")
            results["local_only_overrides_trusted_provider"] = "PASS"

            # An explicit PRIVATE task class also prevents System-One classification.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                systemone = CountingSystemOneManager()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                )
                core = core_for(manager, systemone, router=AmbiguousRouter())
                core.respond("Summarize this document.", request_metadata=BrainRequestMetadata(task_class="PRIVATE"))
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.PRIVATE
                assert selection.privacy == PrivacyLevel.PRIVATE
                assert systemone.calls == omni_gateway.posts == free_gateway.posts == 0
                assert local.calls == 1
                core.close()
            results["private_task_class_skips_systemone"] = "PASS"

            # H: two bounded OmniRoute model attempts, then the configured FreeLLMAPI route.
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key), free=free_for(free_gateway, free_key))
                core = core_for(manager)
                core.respond("Rewrite this line clearly.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.FAST
                assert omni_gateway.models == [FAST_MODEL_DEFAULT, FAST_FALLBACK_MODEL_DEFAULT]
                assert free_gateway.posts == 1 and local.calls == 0
                assert selection.selected_backend == "freellmapi" and selection.fallback_count == 2
                core.close()
            results["fast_fallback_to_freellmapi"] = "PASS"

            # H2: if FreeLLMAPI also fails, FAST reaches local Qwen once.
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(free_key, mode="failure") as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key), free=free_for(free_gateway, free_key))
                core = core_for(manager)
                core.respond("Rewrite this line clearly.")
                selection = core.last_model_selection
                assert selection is not None and selection.selected_backend == "local"
                assert omni_gateway.posts == 2 and free_gateway.posts == 1 and local.calls == 1
                assert selection.fallback_count == 3
                core.close()
            results["fast_fallback_to_local"] = "PASS"

            # I: SMART attempts OmniRoute models, FreeLLMAPI, then configured direct Cloudflare.
            cloud_key = secrets.token_urlsafe(24)
            account_id = secrets.token_hex(16)
            cloud_http = FakeCloudflareHTTP()
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(free_key, mode="failure") as free_gateway:
                local = FakeLocalProvider()
                with patch.dict(
                    os.environ,
                    {
                        "FRIDAY_CLOUDFLARE_ACCOUNT_ID": account_id,
                        "FRIDAY_CLOUDFLARE_API_TOKEN": cloud_key,
                        "FRIDAY_CLOUDFLARE_MODEL": CLOUDFLARE_DEFAULT_MODEL,
                    },
                    clear=False,
                ), patch.object(brain_manager_module, "CLOUDFLARE_HTTP", cloud_http):
                    cloud = CloudflareQwenProvider()
                    manager = manager_for(
                        local,
                        omni=omni_for(omni_gateway, omni_key),
                        free=free_for(free_gateway, free_key),
                        cloud=cloud,
                        cloud_enabled=True,
                    )
                    core = core_for(manager)
                    core.respond("Compare two architectures and explain the tradeoffs.")
                    selection = core.last_model_selection
                    assert selection is not None and selection.policy == ModelPolicy.SMART
                    assert omni_gateway.models == [SMART_MODEL_DEFAULT, SMART_FALLBACK_MODEL_DEFAULT]
                    assert free_gateway.posts == cloud_http.posts == 1
                    assert cloud_http.model == CLOUDFLARE_DEFAULT_MODEL and cloud_http.auth_present
                    assert selection.selected_backend == "cloudflare" and selection.fallback_count == 3
                    assert local.calls == 0
                    core.close()
            results["smart_fallback_cloudflare"] = "PASS"

            # I2: without enabled Cloudflare, SMART ends with one local Qwen attempt.
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(free_key, mode="failure") as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key), free=free_for(free_gateway, free_key))
                core = core_for(manager)
                core.respond("Compare two architectures and explain the tradeoffs.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.SMART
                assert omni_gateway.posts == 2 and free_gateway.posts == 1 and local.calls == 1
                assert selection.selected_backend == "local"
                core.close()
            results["smart_fallback_local"] = "PASS"

            # J: unknown requests safely default to SMART. No implicit auto model.
            with MockGateway(omni_key) as omni_gateway:
                local = FakeLocalProvider()
                manager = manager_for(local, omni=omni_for(omni_gateway, omni_key))
                core = core_for(manager)
                core.respond("Tell me about that.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.SMART
                assert selection.preferred_model == SMART_MODEL_DEFAULT
                assert omni_gateway.models == [SMART_MODEL_DEFAULT]
                core.close()
            results["unknown_defaults_smart"] = "PASS"

            # Vision and voice remain representable but cannot dispatch through chat.
            engine = ModelPolicyEngine(ModelPolicyConfig())
            assert engine.select("image input", requested_policy=ModelPolicy.VISION).preferred_backend is None
            assert engine.select("audio input", requested_policy=ModelPolicy.VOICE).preferred_backend is None
            results["vision_voice_placeholders"] = "PASS"

            # System-One still constructs and handles route hints; no model is called here.
            laya = RemoteLayaProvider(RemoteLayaClient.from_profile(False))
            jev = HostedJevProvider.from_environment({})
            systemone = SystemOneManager(laya_provider=laya, jev_provider=jev)
            assert systemone.classify_route("ambiguous", (Route.GENERAL, Route.CODING)) is None
            assert laya.available is False and jev.available is False
            results["systemone_structural_regression"] = "PASS"

            # Local Qwen remains lazy, and secret/prompt bodies stay out of logs.
            lifecycle = LocalBrainLifecycleManager()
            assert lifecycle.state == LocalBrainState.OFF and not lifecycle.owns_server
            lifecycle.close()
            log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            assert all(secret not in log_text for secret in (omni_key, free_key, cloud_key))
            assert "Authorization" not in log_text and "Bearer " not in log_text
            assert private_marker not in log_text
            assert "policy=PRIVATE" in log_text and "backend=local" in log_text
            results["lazy_qwen_and_privacy_logs"] = "PASS"

    print(json.dumps({"phase": "4D", "mode": "mock", "results": results}, sort_keys=True))
    assert all(result == "PASS" for result in results.values())


def run_phase4d_2_mocks() -> None:
    """Run only the targeted Phase 4D.2 routing and alias regressions."""
    results: dict[str, str] = {}
    omni_key = secrets.token_urlsafe(24)
    free_key = secrets.token_urlsafe(24)

    with tempfile.TemporaryDirectory(prefix="friday-phase4d-2-") as temp_dir:
        log_path = Path(temp_dir) / "route.log"
        with PatchedLogs(log_path):
            # COMPUTER actions stay deterministic and make no model calls.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                cloud = FakeCloudflareProvider()
                systemone = CountingSystemOneManager()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                    cloud=cloud,
                    cloud_enabled=True,
                )
                core = core_for(manager, systemone)
                core._computer = FakeComputerController()  # type: ignore[assignment]
                _, _, decision = core.respond("open notepad")
                assert decision.route == Route.COMPUTER
                assert omni_gateway.posts == free_gateway.posts == 0
                assert cloud.calls == local.calls == systemone.calls == 0
                core.close()
            results["computer_zero_llm"] = "PASS"

            # PRIVATE requests use the local provider without any remote calls.
            with MockGateway(omni_key) as omni_gateway, MockGateway(free_key) as free_gateway:
                local = FakeLocalProvider()
                cloud = FakeCloudflareProvider()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                    cloud=cloud,
                    cloud_enabled=True,
                )
                core = core_for(manager)
                core.respond(
                    "Summarize my private financial document.",
                    request_metadata=BrainRequestMetadata(privacy="private"),
                )
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.PRIVATE
                assert local.calls == 1
                assert omni_gateway.posts == free_gateway.posts == cloud.calls == 0
                core.close()
            results["private_remote_calls_zero"] = "PASS"

            # FAST preserves its bounded OmniRoute -> FreeLLMAPI fallback chain.
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(
                free_key
            ) as free_gateway:
                local = FakeLocalProvider()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                )
                core = core_for(manager)
                core.respond("Rewrite clearly: The app run very fast.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.FAST
                assert omni_gateway.models == [FAST_MODEL_DEFAULT, FAST_FALLBACK_MODEL_DEFAULT]
                assert free_gateway.posts == 1 and local.calls == 0
                assert selection.selected_backend == "freellmapi" and selection.fallback_count == 2
                core.close()
            results["fast_fallback_chain"] = "PASS"

            # SMART preserves its bounded OmniRoute -> FreeLLMAPI -> Cloudflare chain.
            with MockGateway(omni_key, mode="failure") as omni_gateway, MockGateway(
                free_key, mode="failure"
            ) as free_gateway:
                local = FakeLocalProvider()
                cloud = FakeCloudflareProvider()
                manager = manager_for(
                    local,
                    omni=omni_for(omni_gateway, omni_key),
                    free=free_for(free_gateway, free_key),
                    cloud=cloud,
                    cloud_enabled=True,
                )
                core = core_for(manager)
                core.respond("Compare two simple software architecture options.")
                selection = core.last_model_selection
                assert selection is not None and selection.policy == ModelPolicy.SMART
                assert omni_gateway.models == [SMART_MODEL_DEFAULT, SMART_FALLBACK_MODEL_DEFAULT]
                assert free_gateway.posts == cloud.calls == 1 and local.calls == 0
                assert selection.selected_backend == "cloudflare" and selection.fallback_count == 3
                core.close()
            results["smart_fallback_chain"] = "PASS"

            # Malformed assistant responses continue to fail closed.
            for payload in (
                {},
                {"choices": []},
                {"choices": [{"message": {"content": "  "}}]},
            ):
                try:
                    _reply_from_payload(payload, "omniroute")
                except RuntimeError:
                    continue
                raise AssertionError("Malformed OmniRoute response was accepted.")
            results["malformed_response_rejected"] = "PASS"

            # Only exact configured model IDs and their approved bare aliases pass.
            prompt = [{"role": "user", "content": "hello"}]
            for returned_model, expected in (
                ("gemini-3-flash-preview", "bare_alias_accepted"),
                (SMART_MODEL_DEFAULT, "qualified_id_accepted"),
            ):
                with MockGateway(omni_key, response_model=returned_model) as gateway:
                    provider = omni_for(gateway, omni_key)
                    reply = provider.chat_with_model(prompt, SMART_MODEL_DEFAULT)
                    assert reply.model == SMART_MODEL_DEFAULT and gateway.posts == 1
                results[expected] = "PASS"

            for returned_model in (
                "evil/gemini-3-flash-preview",
                "unknown/gemini-3-flash-preview",
                "other/model",
            ):
                with MockGateway(omni_key, response_model=returned_model) as gateway:
                    provider = omni_for(gateway, omni_key)
                    try:
                        provider.chat_with_model(prompt, SMART_MODEL_DEFAULT)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError(
                            f"Unauthorized returned model was accepted: {returned_model}"
                        )
                    assert gateway.posts == 1
            results["arbitrary_alias_rejected"] = "PASS"

    print(json.dumps({"phase": "4D.2", "mode": "focused-mock", "results": results}, sort_keys=True))
    assert all(result == "PASS" for result in results.values())


def run_real() -> None:
    results: list[dict[str, Any]] = []
    profiles = (
        (ModelPolicy.FAST, "Reply exactly with: FRIDAY FAST OK", "FRIDAY FAST OK"),
        (ModelPolicy.SMART, "Reply exactly with: FRIDAY SMART OK", "FRIDAY SMART OK"),
    )
    for policy, prompt, expected in profiles:
        profile = AssistantProfile(cloud_brain_enabled=False)
        manager = BrainManager(profile)
        if not manager.omniroute.available():
            results.append(
                {"policy": policy.value, "status": "NOT_RUN", "reason": "OmniRoute is not configured"}
            )
            manager.close()
            continue
        selection = manager.policy_engine.select(
            prompt,
            requested_policy=policy,
            cloudflare_enabled=False,
        )
        # Allow at most one actual upstream call; mock tests cover the fallback chain.
        real_calls = 0
        original_call = BrainManager._call_provider

        def primary_only(provider, target, messages):
            nonlocal real_calls
            if (
                real_calls
                or target.backend != "omniroute"
                or target.model != selection.preferred_model
            ):
                raise RuntimeError("Primary-only real smoke blocked a fallback call.")
            real_calls += 1
            return original_call(provider, target, messages)

        manager._call_provider = primary_only  # type: ignore[method-assign]
        manager._provider_for = lambda backend: (  # type: ignore[method-assign]
            manager.omniroute if backend == "omniroute" else None
        )
        started = time.perf_counter()
        try:
            reply = manager.respond(
                prompt,
                [],
                request_metadata=BrainRequestMetadata(task_class=policy.value),
                model_selection=selection,
            )
            actual = reply.selection
            lifecycle_idle = manager.local.lifecycle.state == LocalBrainState.OFF
            results.append(
                {
                    "policy": policy.value,
                    "status": "PASS" if reply.text.strip() == expected and lifecycle_idle else "FAIL",
                    "backend": actual.selected_backend if actual else "unknown",
                    "model": actual.selected_model if actual else "unknown",
                    "latency_ms": actual.latency_ms if actual else None,
                    "fallback_count": actual.fallback_count if actual else None,
                    "real_upstream_calls": real_calls,
                    "qwen_unloaded": lifecycle_idle,
                }
            )
        except Exception as error:
            results.append(
                {
                    "policy": policy.value,
                    "status": "FAIL",
                    "backend": "omniroute",
                    "model": selection.preferred_model,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "real_upstream_calls": real_calls,
                    "error_type": type(error).__name__,
                }
            )
        finally:
            manager.close()
    print(json.dumps({"phase": "4D", "mode": "real", "results": results}, sort_keys=True))
    if not all(result["status"] in {"PASS", "NOT_RUN"} for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    if "--phase4d-2-mocks" in sys.argv:
        run_phase4d_2_mocks()
    elif "--real-only" in sys.argv:
        run_real()
    else:
        run_mocks()
