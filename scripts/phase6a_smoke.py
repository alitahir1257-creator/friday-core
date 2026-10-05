"""Offline Phase 6A acceptance checks; no model, provider, or public MCP calls."""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.capabilities.computer.parser import DeterministicComputerPlanner
from friday.capabilities.computer.policy import ApprovalPolicy as ComputerPolicy
from friday.capabilities.registry import ActionPlan, Risk
from friday.connectors import ConnectorRegistry
from friday.connectors.local_files import ApprovedRoot, LocalFilesConnector
from friday.core import FridayCore
from friday.knowledge import KnowledgeVault
from friday.knowledge.unified import UnifiedKnowledgeManager
from friday.memory import MemoryManager
from friday.brain_manager import BrainRequestMetadata
from friday.profile import AssistantProfile
from friday.router import Route, create_router
from friday.tools.adapters import register_knowledge_tools
from friday.tools.models import ApprovalDecision, RiskLevel, ToolDefinition
from friday.tools.registry import ToolRegistrationError, ToolRegistry
from friday.mcp import MCPClientManager, MCPServerConfig


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def _definition(name: str, risk: RiskLevel | str, handler, *, source: str = "phase6a-test", schema: dict | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name, namespace="test", description="Harmless Phase 6A test tool.",
        input_schema=schema or _schema(), handler=handler, capabilities=frozenset({"test"}),
        risk_level=risk, privacy_support=frozenset({"public", "private", "local_only"}),
        source=source, side_effects="none",
    )


class _BrainSpy:
    cloud_enabled = False
    unified = None

    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("COMPUTER request reached a language model.")

    def close(self) -> None:
        return


class _FakeComputer:
    def __init__(self) -> None:
        self.planner = DeterministicComputerPlanner()
        self.policy = ComputerPolicy()
        self.plans: list[ActionPlan] = []

    def execute(self, plan: ActionPlan, approved: bool = False) -> tuple[str, Risk]:
        self.plans.append(plan)
        return "Opened Notepad.", Risk.SAFE

    def execute_request(self, request: str, approved: bool = False) -> tuple[str, Risk]:
        plan = self.planner.plan(request)
        if plan is None:
            return "No deterministic action.", Risk.BLOCKED
        return self.execute(plan, approved=approved)


def _computer_regression(base: Path) -> None:
    import friday.core as core_module

    brain = _BrainSpy()
    core = FridayCore(AssistantProfile(), router=create_router(), brain_manager=brain)
    fake = _FakeComputer()
    core._computer = fake
    try:
        with patch.object(core_module, "LOG_PATH", base / "core.log"):
            answer, prompt_tokens, decision = core.respond("open notepad")
            assert decision.route == Route.COMPUTER
            assert answer == "Opened Notepad."
            assert prompt_tokens is None and brain.calls == 0
            assert len(fake.plans) == 1 and fake.plans[0].actions[0].target.casefold() == "notepad"
            assert core.last_tool_result is not None and core.last_tool_result.success
            assert core.last_tool_result.metadata["receipt"]["tool_name"] == "computer.open_app"

            private_answer, _, private_decision = core.respond(
                "open github.com", request_metadata=BrainRequestMetadata(privacy="private")
            )
            assert private_decision.route == Route.COMPUTER
            assert private_answer.startswith("This tool invocation is blocked")
            assert len(fake.plans) == 1
            assert core.last_tool_result is not None and core.last_tool_result.status == "denied"
    finally:
        core.close()
    print("COMPUTER zero-LLM deterministic adapter: PASS")
    print("PRIVATE browser transmission boundary: PASS")


def _knowledge_smoke(base: Path, registry: ToolRegistry) -> None:
    memory = MemoryManager(base / "memory.sqlite3")
    vault = KnowledgeVault(base / "vault")
    files = base / "approved-files"
    files.mkdir()
    (files / "phase6a.txt").write_text("phase6a knowledge marker approved local file", encoding="utf-8")
    connectors = ConnectorRegistry()
    connector = LocalFilesConnector((ApprovedRoot(files, "friday", "private"),), enabled=True)
    connectors.register(connector)
    manager = UnifiedKnowledgeManager(memory, vault, connectors)
    memory.remember("phase6a knowledge marker private memory", namespace="friday", privacy="private")
    memory.remember("phase6a knowledge marker public memory", namespace="friday", privacy="public")
    vault.create_note("Projects/FRIDAY/phase6a-private.md", "phase6a knowledge marker private vault",
                      namespace="friday", privacy="private")
    register_knowledge_tools(registry, manager)
    try:
        knowledge_tools = registry.find_by_capability("knowledge")
        assert {tool.canonical_name for tool in knowledge_tools} == {
            "knowledge.search", "knowledge.list_sources",
        }
        print("knowledge capability lookup: PASS")

        result = registry.execute(
            "knowledge.search",
            {"query": "phase6a knowledge marker", "namespace": "friday", "privacy": "private", "max_results": 10},
            privacy="private",
        )
        assert result.success
        sources = {item["source_type"] for item in result.structured_data}
        assert {"memory", "vault", "local_file"} <= sources
        assert all(item["source_id"] and item["source_name"] for item in result.structured_data)
        assert any("private memory" in item["snippet"] for item in result.structured_data)
        assert any("private vault" in item["snippet"] for item in result.structured_data)
        assert any("approved local file" in item["snippet"] for item in result.structured_data)
        print("knowledge search and provenance: PASS")

        public = registry.execute(
            "knowledge.search", {"query": "phase6a knowledge marker", "namespace": "friday"}, privacy="public"
        )
        assert public.success
        assert all(item["privacy"] == "public" for item in public.structured_data)
        source_result = registry.execute("knowledge.list_sources", {})
        assert source_result.success
        assert {source["type"] for source in source_result.structured_data} >= {"memory", "vault", "local_file"}
        print("knowledge public filtering and source listing: PASS")
    finally:
        memory.close()


def _run_prior_regression(name: str, expected: str) -> None:
    script = ROOT / "scripts" / name
    completed = subprocess.run(
        [sys.executable, "-B", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    if completed.returncode:
        raise AssertionError(f"{name} failed.\n{completed.stdout[-2500:]}\n{completed.stderr[-2500:]}")
    if expected not in completed.stdout:
        raise AssertionError(f"{name} did not print its PASS marker.\n{completed.stdout[-1000:]}")
    print(f"{name}: PASS")


def run() -> dict[str, float | int]:
    network_calls = 0
    def no_network(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("Network calls are prohibited during Phase 6A tests.")

    tracemalloc.start()
    heap_before = tracemalloc.get_traced_memory()[0]
    with tempfile.TemporaryDirectory(prefix="friday-phase6a-") as temporary, \
            patch.object(socket.socket, "connect", no_network), \
            patch.object(socket.socket, "connect_ex", no_network), \
            patch.object(socket, "getaddrinfo", no_network):
        base = Path(temporary)
        registry = ToolRegistry()
        assert registry.list() == []

        calls = {"read": 0, "reversible": 0, "consequential": 0, "destructive": 0, "unknown": 0, "invalid": 0}
        harmless = _definition(
            "echo", RiskLevel.READ_ONLY,
            lambda args, _context: args["text"],
            source="phase6a-test",
            schema=_schema({"text": {"type": "string", "minLength": 1, "maxLength": 100}}, ["text"]),
        )
        registry.register(harmless)
        assert registry.get("test.echo") is harmless
        print("A TOOL REGISTRATION: PASS")
        try:
            registry.register(harmless)
        except ToolRegistrationError:
            pass
        else:
            raise AssertionError("Duplicate canonical tool name was accepted.")
        try:
            registry.register(_definition("echo", RiskLevel.READ_ONLY, lambda _a, _c: None, source="other"), replace_existing=True)
        except ToolRegistrationError:
            pass
        else:
            raise AssertionError("Replacement from a different source was accepted.")
        registry.register(
            ToolDefinition(**{**harmless.__dict__, "description": "Controlled same-source replacement."}),
            replace_existing=True,
        )
        print("B DUPLICATE REGISTRATION PROTECTION: PASS")

        registry.register(_definition("read", RiskLevel.READ_ONLY, lambda _a, _c: calls.__setitem__("read", calls["read"] + 1) or "read"))
        registry.register(_definition("reversible", RiskLevel.REVERSIBLE, lambda _a, _c: calls.__setitem__("reversible", calls["reversible"] + 1) or "changed"))
        registry.register(_definition("consequential", RiskLevel.CONSEQUENTIAL, lambda _a, _c: calls.__setitem__("consequential", calls["consequential"] + 1) or "sent"))
        registry.register(_definition("destructive", RiskLevel.DESTRUCTIVE, lambda _a, _c: calls.__setitem__("destructive", calls["destructive"] + 1) or "deleted"))
        registry.register(_definition("unknown", "UNTRUSTED", lambda _a, _c: calls.__setitem__("unknown", calls["unknown"] + 1) or "unsafe"))
        registry.register(_definition(
            "invalid", RiskLevel.READ_ONLY,
            lambda _a, _c: calls.__setitem__("invalid", calls["invalid"] + 1),
            schema=_schema({"value": {"type": "integer"}}, ["value"]),
        ))
        assert registry.execute("test.read", {}).success
        assert registry.execute("test.reversible", {}).success
        print("D READ_ONLY approval: PASS")
        print("E REVERSIBLE approval: PASS")

        sensitive_calls = 0
        def sensitive_handler(_args, _context):
            nonlocal sensitive_calls
            sensitive_calls += 1
            return "local private result"
        registry.register(ToolDefinition(
            name="sensitive", namespace="test", description="Private local test tool.",
            input_schema=_schema(), handler=sensitive_handler, capabilities=frozenset({"test"}),
            risk_level=RiskLevel.SENSITIVE, privacy_support=frozenset({"private", "local_only"}),
            source="phase6a-test", side_effects="none",
        ))
        assert registry.execute("test.sensitive", {}).status == "denied"
        private_sensitive = registry.execute("test.sensitive", {}, privacy="private")
        assert private_sensitive.success and sensitive_calls == 1
        print("SENSITIVE privacy routing without confirmation: PASS")

        denied = registry.execute("test.consequential", {})
        assert denied.requires_followup and calls["consequential"] == 0
        approved = registry.execute("test.consequential", {}, approval_callback=lambda request: request.tool_name == "test.consequential")
        assert approved.success and calls["consequential"] == 1
        print("F CONSEQUENTIAL confirmation and explicit approval: PASS")

        destructive = registry.execute("test.destructive", {})
        assert destructive.requires_followup and calls["destructive"] == 0
        assert registry.execute("test.destructive", {}, approval_callback=lambda _request: True).success
        assert calls["destructive"] == 1
        print("G DESTRUCTIVE confirmation: PASS")

        unknown = registry.execute("test.unknown", {})
        assert unknown.requires_followup and calls["unknown"] == 0
        print("H UNKNOWN RISK fail-safe: PASS")

        invalid = registry.execute("test.invalid", {"value": True})
        assert not invalid.success and invalid.status == "invalid_arguments" and calls["invalid"] == 0
        assert not registry.execute("test.echo", {"text": "valid", "extra": "rejected"}).success
        cyclic_value = []
        cyclic_value.append(cyclic_value)
        assert registry.execute("test.echo", {"text": cyclic_value}).status == "invalid_arguments"
        try:
            registry.register(_definition(
                "malformed_schema", RiskLevel.READ_ONLY, lambda _a, _c: None,
                schema={"type": "object", "properties": {"x": {"type": []}}},
            ))
        except ToolRegistrationError:
            pass
        else:
            raise AssertionError("A malformed tool schema was accepted.")
        print("I ARGUMENT VALIDATION before execution: PASS")

        registry.disable("test.read")
        assert registry.execute("test.read", {}).status == "disabled"
        registry.enable("test.read")
        assert registry.execute("test.read", {}).success
        print("W DISABLED TOOL: PASS")

        secret = "synthetic-phase6a-password-value"
        registry.register(_definition(
            "secret_echo", RiskLevel.READ_ONLY,
            lambda args, _context: args["password"],
            schema=_schema({"password": {"type": "string"}}, ["password"]),
        ))
        secret_result = registry.execute("test.secret_echo", {"password": secret})
        assert secret_result.success
        assert secret not in secret_result.summary and secret not in json.dumps(secret_result.metadata)
        assert "receipt" in secret_result.metadata
        print("V SAFE RECEIPT and secret-argument redaction: PASS")

        approval_secret = "synthetic-approval-api-key"
        approval_requests = []
        registry.register(ToolDefinition(
            name="approval_summary", namespace="test", description="Approval summary test.",
            input_schema=_schema({"api_key": {"type": "string"}}, ["api_key"]),
            handler=lambda _args, _context: "approved", capabilities=frozenset({"test"}),
            risk_level=RiskLevel.CONSEQUENTIAL, privacy_support=frozenset({"public"}), source="test",
        ))
        approval_summary_result = registry.execute(
            "test.approval_summary", {"api_key": approval_secret},
            approval_callback=lambda request: approval_requests.append(request) or True,
        )
        assert approval_summary_result.success and approval_requests
        assert approval_secret not in approval_requests[0].summary
        assert "[REDACTED]" in approval_requests[0].summary

        remote_calls = 0
        def remote_handler(_args, _context):
            nonlocal remote_calls
            remote_calls += 1
            return "remote"
        registry.register(ToolDefinition(
            name="remote_probe", namespace="remote", description="Synthetic transmitting tool.",
            input_schema=_schema(), handler=remote_handler, capabilities=frozenset({"communication"}),
            risk_level=RiskLevel.READ_ONLY, privacy_support=frozenset({"public"}), source="test",
            transmits_data=True, side_effects="external",
        ))
        private_remote = registry.execute("remote.remote_probe", {}, privacy="private")
        assert private_remote.status == "denied" and remote_calls == 0
        print("AF PRIVATE boundary: PASS")
        print("AG PRIVATE remote-call count: 0")

        _knowledge_smoke(base, registry)
        _computer_regression(base)
        print("L native COMPUTER adapter: PASS")
        print("M knowledge tool adapter: PASS")
        print("AH COMPUTER zero-LLM regression: PASS")

        # Bounded, local stdio MCP acceptance with explicit tool and risk maps.
        mcp_registry = ToolRegistry()
        mcp_manager = MCPClientManager(mcp_registry)
        disabled_config = MCPServerConfig("disabled", sys.executable, enabled=False)
        assert mcp_manager.connect(disabled_config) == ()
        assert not mcp_manager.clients
        print("X DISABLED MCP server starts no process: PASS")

        previous_secret = os.environ.get("FRIDAY_PHASE6A_SYNTHETIC_SECRET")
        os.environ["FRIDAY_PHASE6A_SYNTHETIC_SECRET"] = "synthetic-parent-only-secret"

        def cleanup_mcp_tests() -> None:
            mcp_manager.disconnect_all()
            if previous_secret is None:
                os.environ.pop("FRIDAY_PHASE6A_SYNTHETIC_SECRET", None)
            else:
                os.environ["FRIDAY_PHASE6A_SYNTHETIC_SECRET"] = previous_secret

        atexit.register(cleanup_mcp_tests)
        server_script = ROOT / "tests" / "scripts" / "phase6a_mcp_server.py"
        config = MCPServerConfig(
            name="test", command=sys.executable, args=("-B", str(server_script)),
            allowed_tools=frozenset({"echo", "add", "get_status"}),
            blocked_tools=frozenset({"blocked_noop"}),
            risk_map={"echo": "READ_ONLY", "add": "READ_ONLY"},
            capability_map={"echo": frozenset({"test", "mcp"}), "add": frozenset({"test", "mcp"})},
            privacy_support=frozenset({"public", "private", "local_only"}),
            transmits_data=False, timeout_seconds=3, startup_timeout_seconds=5,
        )
        discovery_started = time.perf_counter()
        discovered = mcp_manager.connect(config)
        discovery_ms = round((time.perf_counter() - discovery_started) * 1000, 3)
        assert {"mcp.test.echo", "mcp.test.add", "mcp.test.get_status"} <= set(discovered)
        assert "mcp.test.not_allowed" not in {tool.canonical_name for tool in mcp_registry.list()}
        print("M MCP local stdio connect: PASS")
        print("N MCP discovery and allowlist: PASS")

        echo_started = time.perf_counter()
        echo = mcp_registry.execute("mcp.test.echo", {"text": "FRIDAY MCP OK"})
        echo_ms = round((time.perf_counter() - echo_started) * 1000, 3)
        assert echo.success and echo.summary == "FRIDAY MCP OK"
        mcp_samples = [echo_ms]
        for index in range(7):
            started = time.perf_counter()
            measured = mcp_registry.execute("mcp.test.echo", {"text": f"timing {index}"})
            mcp_samples.append(round((time.perf_counter() - started) * 1000, 3))
            assert measured.success and measured.summary == f"timing {index}"
        add = mcp_registry.execute("mcp.test.add", {"a": 2, "b": 3})
        assert add.success and add.structured_data == 5 and add.summary == "5"
        print("O MCP echo invocation: PASS")
        print("P MCP add invocation: PASS")

        mcp_invalid = mcp_registry.execute("mcp.test.add", {"a": 2, "b": "3"})
        assert not mcp_invalid.success and mcp_invalid.status == "invalid_arguments"
        assert not mcp_registry.execute("mcp.test.echo", {"text": "ok", "extra": True}).success
        print("Q MCP invalid arguments: PASS")

        unknown_mcp = mcp_registry.execute("mcp.test.get_status", {})
        assert unknown_mcp.requires_followup
        approved_status = mcp_registry.execute(
            "mcp.test.get_status", {}, approval_callback=lambda request: request.tool_name == "mcp.test.get_status"
        )
        assert approved_status.success and approved_status.structured_data["calls"] == 1
        assert approved_status.structured_data["parent_marker_visible"] is False
        print("R unknown MCP risk confirmation: PASS")
        print("AE parent secret environment isolation: PASS")

        mcp_registry.register(_definition("native_probe", RiskLevel.READ_ONLY, lambda _a, _c: "native alive", source="native"))
        test_client = mcp_manager.clients["test"]
        test_process = test_client.process
        assert test_process is not None
        test_process.terminate()
        test_process.wait(timeout=3)
        assert test_client.wait_unavailable(3)
        deadline = time.monotonic() + 1
        while mcp_registry.get("mcp.test.echo").enabled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not mcp_registry.get("mcp.test.echo").enabled
        assert mcp_registry.execute("test.native_probe", {}).success
        assert mcp_registry.execute("mcp.test.echo", {"text": "after crash"}).status == "disabled"
        print("S MCP crash isolation: PASS")
        print("AC MCP crash isolation: PASS")

        cleanup_config = MCPServerConfig(
            name="cleanup", command=sys.executable, args=("-B", str(server_script)),
            allowed_tools=frozenset({"echo"}), risk_map={"echo": "READ_ONLY"},
            transmits_data=False, timeout_seconds=3, startup_timeout_seconds=5,
        )
        cleanup_names = mcp_manager.connect(cleanup_config)
        assert "mcp.cleanup.echo" in cleanup_names
        cleanup_client = mcp_manager.clients["cleanup"]
        cleanup_process = cleanup_client.process
        assert cleanup_process is not None
        mcp_manager.disconnect("cleanup")
        assert cleanup_process.poll() is not None

        legacy_config = MCPServerConfig(
            name="legacy", command=sys.executable,
            args=("-B", str(server_script), "--legacy"),
            allowed_tools=frozenset({"echo"}), risk_map={"echo": "READ_ONLY"},
            transmits_data=False, timeout_seconds=3, startup_timeout_seconds=5,
        )
        legacy_names = mcp_manager.connect(legacy_config)
        assert "mcp.legacy.echo" in legacy_names
        legacy_client = mcp_manager.clients["legacy"]
        assert not legacy_client._modern_protocol and legacy_client._protocol_version == "2025-11-25"
        legacy_echo = mcp_registry.execute("mcp.legacy.echo", {"text": "legacy fallback"})
        assert legacy_echo.success and legacy_echo.summary == "legacy fallback"
        legacy_process = legacy_client.process
        assert legacy_process is not None
        mcp_manager.disconnect("legacy")
        assert legacy_process.poll() is not None
        print("V MCP stdio transport modern and legacy fallback: PASS")

        mcp_manager.disconnect_all()
        assert test_process.poll() is not None and cleanup_process.poll() is not None and legacy_process.poll() is not None
        print("T MCP cleanup and controlled shutdown: PASS")
        print("AD MCP cleanup: PASS")

        cleanup_mcp_tests()
        atexit.unregister(cleanup_mcp_tests)

        # Prior gates use temporary fixtures and provider fakes; the full interactive
        # Phase 3A script is deliberately not launched by this offline regression.
        _run_prior_regression("phase5a_smoke.py", "Memory retrieval failure handling: PASS")
        _run_prior_regression("phase5b_smoke.py", "Vault archive operation: PASS")
        _run_prior_regression("phase5c_smoke.py", "FRIDAY PHASE 5C PASS")

        lookups = 1000
        started = time.perf_counter()
        for _ in range(lookups):
            assert registry.get("test.read") is not None
        lookup_us = round((time.perf_counter() - started) * 1_000_000 / lookups, 3)

        started = time.perf_counter()
        for _ in range(100):
            assert registry.execute("test.read", {}).success
        native_ms = round((time.perf_counter() - started) * 10, 3)

        mcp_invocation_ms = statistics.median(mcp_samples)
        _, peak = tracemalloc.get_traced_memory()
        heap_delta = max(0, peak - heap_before)
        assert network_calls == 0
        metrics = {
            "registry_lookup_us": lookup_us,
            "native_tool_wrapper_ms_per_call": native_ms,
            "mcp_discovery_ms": discovery_ms,
            "mcp_invocation_ms": mcp_invocation_ms,
            "traced_heap_peak_delta_bytes": heap_delta,
            "mcp_processes_at_cleanup": 0,
            "external_network_calls": network_calls,
        }
        print("AM/AP lightweight registry and resource metrics: " + json.dumps(metrics, sort_keys=True))
        print("AQ regression gates Phase 5A/5B/5C: PASS")
        print("FRIDAY PHASE 6A PASS")
        return metrics
    tracemalloc.stop()


if __name__ == "__main__":
    run()
