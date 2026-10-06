"""Offline Phase 6B planner and worker acceptance; local stdio MCP only."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.brain_manager import BrainManager, BrainReply
from friday.capabilities.computer.parser import DeterministicComputerPlanner
from friday.capabilities.computer.policy import ApprovalPolicy as ComputerPolicy
from friday.capabilities.registry import ActionPlan, Risk
from friday.core import FridayCore
from friday.mcp import MCPClientManager, MCPServerConfig
from friday.model_policy import ModelPolicy
from friday.planner import (
    MAX_PLAN_REPAIRS,
    MAX_PLAN_STEPS,
    MAX_STEP_RETRIES,
    MAX_TOOL_CALLS,
    PlanStep,
    Planner,
    PlanValidationError,
    PlanValidator,
    StepStatus,
    TaskPlan,
    TaskStatus,
    WorkerExecutor,
    WorkerProfile,
)
from friday.profile import AssistantProfile
from friday.research.search import BrowserSearchProvider
from friday.router import Route, create_router
from friday.tools.adapters import register_knowledge_tools
from friday.tools.models import RiskLevel, ToolDefinition, ToolResult
from friday.tools.registry import ToolRegistry


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _register(
    registry: ToolRegistry,
    name: str,
    handler,
    *,
    namespace: str = "test",
    capability: str = "general",
    risk: RiskLevel = RiskLevel.READ_ONLY,
    properties: dict | None = None,
    required: list[str] | None = None,
    side_effects: str = "none",
    transmits_data: bool = False,
    privacy: frozenset[str] = frozenset({"public", "private", "local_only"}),
    source: str = "phase6b-test",
) -> str:
    tool = ToolDefinition(
        name=name,
        namespace=namespace,
        description="Phase 6B test tool.",
        input_schema=_schema(properties, required),
        handler=lambda args, _context: handler(args),
        capabilities=frozenset({capability}),
        risk_level=risk,
        privacy_support=privacy,
        source=source,
        side_effects=side_effects,
        transmits_data=transmits_data,
    )
    registry.register(tool)
    return tool.canonical_name


def _step(
    step_id: str,
    tool: str | None,
    *,
    capability: str = "general",
    profile: WorkerProfile = WorkerProfile.GENERAL,
    arguments: dict | None = None,
    dependencies: tuple[str, ...] = (),
    verification: str = "tool_success",
) -> PlanStep:
    return PlanStep(
        id=step_id,
        description=f"Run {step_id}.",
        profile=profile,
        capability=capability,
        preferred_tool=tool,
        arguments=arguments or {},
        dependencies=dependencies,
        verification=verification,
    )


def _plan(*steps: PlanStep, privacy: str = "public", goal: str = "Phase 6B smoke task.") -> TaskPlan:
    return TaskPlan(goal=goal, privacy=privacy, namespace="phase6b", steps=tuple(steps))


class _BrainSpy:
    cloud_enabled = False

    def __init__(self, unified=None) -> None:
        self.calls = 0
        self.unified = unified

    def respond(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("This offline smoke must not call a language provider.")

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


def _assert_rejected(validator: PlanValidator, plan: TaskPlan, code: str) -> None:
    try:
        validator.validate(plan)
    except PlanValidationError as error:
        assert error.code == code, (error.code, code)
    else:
        raise AssertionError(f"Expected plan rejection: {code}")


def run() -> None:
    network_calls = 0

    def no_network(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("External network calls are prohibited in Phase 6B smoke.")

    tracemalloc.start()
    try:
        with tempfile.TemporaryDirectory(prefix="friday-phase6b-") as temp_dir, \
                patch.object(socket.socket, "connect", no_network), \
                patch.object(socket.socket, "connect_ex", no_network), \
                patch.object(socket, "getaddrinfo", no_network):
            temp = Path(temp_dir)
            import friday.core as core_module

            # A. The existing one-step COMPUTER route stays deterministic.
            brain = _BrainSpy()
            core = FridayCore(AssistantProfile(), router=create_router(), brain_manager=brain)
            fake_computer = _FakeComputer()
            core._computer = fake_computer
            with patch.object(core_module, "LOG_PATH", temp / "core.log"):
                answer, prompt_tokens, decision = core.respond("open notepad")
            assert decision.route == Route.COMPUTER and answer == "Opened Notepad."
            assert prompt_tokens is None and brain.calls == 0 and core.planner.model_calls == 0
            assert core.last_tool_result and core.last_tool_result.success
            assert core.tool_registry.receipts[-1].tool_name == "computer.open_app"
            core.close()

            # A one-call knowledge lookup stays on the ToolRegistry path.
            class _DirectKnowledge:
                calls = 0
                def search(self, *_args, **_kwargs):
                    self.calls += 1
                    return []
            direct_knowledge = _DirectKnowledge()
            knowledge_brain = _BrainSpy(unified=direct_knowledge)
            knowledge_core = FridayCore(AssistantProfile(), router=create_router(), brain_manager=knowledge_brain)
            knowledge_core.respond("Search my memory for a local fact")
            assert direct_knowledge.calls == 1 and knowledge_brain.calls == 0
            assert knowledge_core.planner.model_calls == 0
            assert knowledge_core.last_tool_result and knowledge_core.last_tool_result.tool_name == "knowledge.search"
            knowledge_core.close()

            # B. A fake structured planner produces a minimal two-step COMPUTER plan.
            computer_tools = ToolRegistry()
            computer_calls = {"open": 0, "type": 0}
            open_name = _register(
                computer_tools, "open_app", lambda _args: computer_calls.__setitem__("open", computer_calls["open"] + 1) or "Opened Notepad.",
                namespace="computer", capability="computer", risk=RiskLevel.REVERSIBLE,
                properties={"app": {"type": "string", "minLength": 1}}, required=["app"], side_effects="local_desktop",
            )
            type_name = _register(
                computer_tools, "type_text", lambda _args: computer_calls.__setitem__("type", computer_calls["type"] + 1) or "Text entered.",
                namespace="computer", capability="computer", risk=RiskLevel.CONSEQUENTIAL,
                properties={"text": {"type": "string", "minLength": 1}}, required=["text"], side_effects="local_input",
            )
            planned_json = json.dumps({"steps": [
                {"id": "open", "description": "Open Notepad.", "profile": "COMPUTER", "capability": "computer",
                 "preferred_tool": open_name, "arguments": {"app": "Notepad"}, "dependencies": [], "verification": "window_exists"},
                {"id": "type", "description": "Type the requested text.", "profile": "COMPUTER", "capability": "computer",
                 "preferred_tool": type_name, "arguments": {"text": "FRIDAY WORKER OK"}, "dependencies": ["open"], "verification": "tool_success"},
            ]})
            fake_calls = []
            planner = Planner(None, computer_tools, model_call=lambda goal, prompt, privacy: fake_calls.append((goal, privacy)) or planned_json)
            started = time.perf_counter()
            plan = planner.plan("Open Notepad and type FRIDAY WORKER OK")
            planning_ms = (time.perf_counter() - started) * 1000
            assert len(plan.steps) == 2 and plan.steps[0].preferred_tool == open_name
            assert planner.last_selection.policy == ModelPolicy.SMART
            validator = PlanValidator(computer_tools)
            validation_started = time.perf_counter()
            validated = validator.validate(plan)
            validation_ms = (time.perf_counter() - validation_started) * 1000
            worker = WorkerExecutor(computer_tools, validator=validator, planner=planner)
            result = worker.execute(validated.plan)
            assert result.status == TaskStatus.WAITING_APPROVAL and computer_calls == {"open": 1, "type": 0}
            approval = worker.current_state()["pending_approval"]
            assert set(approval) == {"action_summary", "tool", "risk", "expected_side_effect"}
            assert approval["tool"] == type_name and approval["risk"] == "CONSEQUENTIAL"
            result = worker.approve(result.task_id, "YES")
            assert result.status == TaskStatus.SUCCEEDED and computer_calls == {"open": 1, "type": 1}

            # FridayCore routes the same multi-step request through Planner + WorkerExecutor.
            integrated_registry = ToolRegistry()
            integrated_calls = {"open": 0, "type": 0}
            integrated_open = _register(
                integrated_registry, "open_app", lambda _args: integrated_calls.__setitem__("open", integrated_calls["open"] + 1) or "Opened Notepad.",
                namespace="computer", capability="computer", risk=RiskLevel.REVERSIBLE,
                properties={"app": {"type": "string", "minLength": 1}}, required=["app"], side_effects="local_desktop",
            )
            integrated_type = _register(
                integrated_registry, "type_text", lambda _args: integrated_calls.__setitem__("type", integrated_calls["type"] + 1) or "Text entered.",
                namespace="computer", capability="computer", risk=RiskLevel.CONSEQUENTIAL,
                properties={"text": {"type": "string", "minLength": 1}}, required=["text"], side_effects="local_input",
            )
            integrated_json = json.dumps({"steps": [
                {"id": "open", "description": "Open Notepad.", "profile": "COMPUTER", "capability": "computer",
                 "preferred_tool": integrated_open, "arguments": {"app": "Notepad"}, "dependencies": []},
                {"id": "type", "description": "Type the requested text.", "profile": "COMPUTER", "capability": "computer",
                 "preferred_tool": integrated_type, "arguments": {"text": "FRIDAY WORKER OK"}, "dependencies": ["open"]},
            ]})
            integrated_planner = Planner(None, integrated_registry, model_call=lambda *_args: integrated_json)
            integrated_worker = WorkerExecutor(integrated_registry, planner=integrated_planner)
            integrated_brain = _BrainSpy()
            integrated_core = FridayCore(
                AssistantProfile(), router=create_router(), brain_manager=integrated_brain,
                planner=integrated_planner, worker_executor=integrated_worker,
            )
            integrated_core.respond("Open Notepad and type FRIDAY WORKER OK")
            assert integrated_planner.model_calls == 1 and integrated_brain.calls == 0
            assert integrated_calls == {"open": 1, "type": 0}
            state = integrated_core.current_task_state
            assert state["status"] == TaskStatus.WAITING_APPROVAL.value and state["current_step_id"] == "type"
            integrated_core.approve_task(state["task_id"], "YES")
            assert integrated_core.current_task_state["status"] == TaskStatus.SUCCEEDED.value
            assert integrated_calls == {"open": 1, "type": 1}
            integrated_core.close()

            # C-E. Unknown tools, excessive plans, and cycles are rejected.
            basic_registry = ToolRegistry()
            read_name = _register(basic_registry, "read", lambda _args: "ok")
            basic_validator = PlanValidator(basic_registry)
            _assert_rejected(basic_validator, _plan(_step("bad", "test.missing")), "UNKNOWN_TOOL")
            too_many = tuple(_step(f"s{i}", None) for i in range(MAX_PLAN_STEPS + 1))
            _assert_rejected(basic_validator, _plan(*too_many), "STEP_LIMIT")
            cycle = _plan(_step("a", None, dependencies=("b",)), _step("b", None, dependencies=("a",)))
            _assert_rejected(basic_validator, cycle, "DEPENDENCY_CYCLE")
            _assert_rejected(basic_validator, _plan(_step("unknown", None, capability="not_registered")), "UNKNOWN_CAPABILITY")
            _assert_rejected(basic_validator, _plan(_step("bad_args", read_name, arguments={"unexpected": 1})), "INVALID_ARGUMENTS")
            shell_registry = ToolRegistry()
            shell_name = _register(shell_registry, "run", lambda _args: "unsafe", namespace="shell")
            _assert_rejected(PlanValidator(shell_registry), _plan(_step("shell", shell_name)), "FORBIDDEN_TOOL")

            # F. READ_ONLY knowledge-style actions execute through ToolRegistry.
            read_calls = []
            read_name = _register(basic_registry, "lookup", lambda args: read_calls.append(args) or [{"value": 7}], namespace="knowledge",
                                  capability="knowledge", properties={"query": {"type": "string"}}, required=["query"])
            read_plan = _plan(_step("lookup", read_name, capability="knowledge", profile=WorkerProfile.KNOWLEDGE,
                                    arguments={"query": "local fact"}, verification="non_empty"))
            read_worker = WorkerExecutor(basic_registry)
            read_result = read_worker.execute(read_plan)
            assert read_result.status == TaskStatus.SUCCEEDED and len(read_calls) == 1
            assert basic_registry.receipts[-1].tool_name == read_name

            # G-H. Consequential actions wait; only an explicit YES executes once.
            approval_registry = ToolRegistry()
            publish_calls = []
            publish_name = _register(approval_registry, "publish", lambda args: publish_calls.append(args) or "Published.",
                                     risk=RiskLevel.CONSEQUENTIAL, side_effects="external_publish",
                                     properties={"payload": {"type": "string"}}, required=["payload"])
            approval_worker = WorkerExecutor(approval_registry)
            approval_plan = _plan(replace(
                _step("publish", publish_name, arguments={"payload": "approved value"}),
                risk_hint="READ_ONLY",
            ))
            approval_result = approval_worker.execute(approval_plan)
            assert approval_result.status == TaskStatus.WAITING_APPROVAL and not publish_calls
            assert set(approval_worker.current_state()["pending_approval"]) == {
                "action_summary", "tool", "risk", "expected_side_effect",
            }
            approval_plan.steps[0].arguments["payload"] = "unapproved changed value"
            approval_result = approval_worker.approve(approval_result.task_id, "YES")
            assert approval_result.status == TaskStatus.SUCCEEDED and publish_calls == [{"payload": "approved value"}]
            assert approval_worker.current_task.explicit_approvals == {"publish"}

            unsafe_mcp_calls = []
            unsafe_mcp_name = _register(approval_registry, "mutate", lambda _args: unsafe_mcp_calls.append(1),
                                        namespace="mcp.local", capability="mcp", risk=RiskLevel.CONSEQUENTIAL,
                                        side_effects="local_mcp", source="mcp:local")
            unsafe_mcp_worker = WorkerExecutor(approval_registry)
            unsafe_mcp_result = unsafe_mcp_worker.execute(_plan(_step("mutate", unsafe_mcp_name, capability="mcp")))
            assert unsafe_mcp_result.status == TaskStatus.WAITING_APPROVAL and not unsafe_mcp_calls

            destructive_calls = []
            destructive_name = _register(approval_registry, "delete", lambda _args: destructive_calls.append(1),
                                         risk=RiskLevel.DESTRUCTIVE, side_effects="local_delete")
            destructive_worker = WorkerExecutor(approval_registry)
            destructive_result = destructive_worker.execute(_plan(_step("delete", destructive_name)))
            assert destructive_result.status == TaskStatus.WAITING_APPROVAL and not destructive_calls

            denied_calls = []
            denied_name = _register(approval_registry, "denied", lambda _args: denied_calls.append(1),
                                    risk=RiskLevel.CONSEQUENTIAL, side_effects="external_send")
            denied_worker = WorkerExecutor(approval_registry)
            callback_count = 0
            def decline(_request):
                nonlocal callback_count
                callback_count += 1
                return "NO"
            denied_result = denied_worker.execute(_plan(_step("denied", denied_name)), approval_callback=decline)
            assert denied_result.status == TaskStatus.CANCELLED and not denied_calls and callback_count == 1

            # I. Cancellation after one step prevents all remaining calls.
            cancel_registry = ToolRegistry()
            cancel_calls = []
            cancel_worker = None
            def cancel_while_running(_args):
                cancel_calls.append("first")
                assert cancel_worker.cancel()
                return "finished current tool"
            first_name = _register(cancel_registry, "first", cancel_while_running)
            second_name = _register(cancel_registry, "second", lambda _args: cancel_calls.append("second") or "done")
            cancel_worker = WorkerExecutor(cancel_registry)
            cancel_result = cancel_worker.execute(_plan(
                _step("first", first_name), _step("second", second_name, dependencies=("first",)),
            ))
            assert cancel_result.status == TaskStatus.CANCELLED and cancel_calls == ["first"]
            assert cancel_worker.current_task.plan.steps[0].status == StepStatus.SUCCEEDED

            # J. A failed prerequisite blocks its dependent step.
            failure_registry = ToolRegistry()
            failed_calls = []
            fail_name = _register(failure_registry, "fail", lambda _args: (_ for _ in ()).throw(ValueError("synthetic")))
            dependent_name = _register(failure_registry, "dependent", lambda _args: failed_calls.append("dependent") or "bad")
            failure_worker = WorkerExecutor(failure_registry)
            failure_result = failure_worker.execute(_plan(
                _step("fail", fail_name), _step("dependent", dependent_name, dependencies=("fail",)),
            ))
            assert failure_result.status == TaskStatus.FAILED and not failed_calls
            assert next(step for step in failure_worker.current_task.plan.steps if step.id == "dependent").status == StepStatus.SKIPPED

            # K. One safe retry for a transient read-only failure.
            retry_registry = ToolRegistry()
            retry_calls = []
            def flaky(_args):
                retry_calls.append(1)
                if len(retry_calls) == 1:
                    raise TimeoutError("temporary")
                return 9
            retry_name = _register(retry_registry, "flaky", flaky, risk=RiskLevel.REVERSIBLE)
            retry_started = time.perf_counter()
            retry_result = WorkerExecutor(retry_registry).execute(_plan(_step("flaky", retry_name)))
            retry_total_ms = (time.perf_counter() - retry_started) * 1000
            assert retry_result.status == TaskStatus.SUCCEEDED and len(retry_calls) == 1 + MAX_STEP_RETRIES
            assert [step.attempt for step in retry_result.step_results] == [1, 2]

            # The independent task-call ceiling also holds when retries are used.
            cap_registry = ToolRegistry()
            cap_calls = {f"step{i}": 0 for i in range(7)}
            cap_steps = []
            for i in range(7):
                step_id = f"step{i}"
                def cap_handler(_args, key=step_id):
                    cap_calls[key] += 1
                    if cap_calls[key] == 1:
                        raise TimeoutError("retry once")
                    return "ok"
                tool = _register(cap_registry, f"tool{i}", cap_handler)
                cap_steps.append(_step(step_id, tool))
            cap_worker = WorkerExecutor(cap_registry)
            cap_result = cap_worker.execute(_plan(*cap_steps))
            assert cap_result.status == TaskStatus.BLOCKED and cap_worker.current_task.tool_calls == MAX_TOOL_CALLS

            # L. Consequential failures are never retried.
            conseq_calls = []
            def flaky_publish(_args):
                conseq_calls.append(1)
                raise TimeoutError("uncertain publish outcome")
            conseq_name = _register(approval_registry, "flaky_publish", flaky_publish,
                                    risk=RiskLevel.CONSEQUENTIAL, side_effects="external_publish")
            conseq_worker = WorkerExecutor(approval_registry)
            conseq_result = conseq_worker.execute(_plan(_step("publish", conseq_name)))
            conseq_result = conseq_worker.approve(conseq_result.task_id, "YES")
            assert conseq_result.status == TaskStatus.FAILED and len(conseq_calls) == 1

            # M-N. Repair a newly unavailable read-only tool at most once and validate the replacement.
            repair_registry = ToolRegistry()
            old_calls, new_calls = [], []
            old_name = _register(repair_registry, "old", lambda _args: old_calls.append(1), capability="general")
            new_name = _register(repair_registry, "new", lambda _args: new_calls.append(1), capability="general")
            repair_base = _plan(_step("read", old_name))
            class _Repairer:
                calls = 0
                invalid = False
                def repair(self, current, *, step_id, failure_code):
                    self.calls += 1
                    steps = tuple(
                        replace(step, preferred_tool=(new_name if step.id == step_id else step.preferred_tool), status=StepStatus.PENDING)
                        for step in current.steps
                    )
                    return replace(current, goal="new unauthorized goal" if self.invalid else current.goal, steps=steps)
            repairer = _Repairer()
            def disable_old(event):
                if event.event == "task_planned":
                    repair_registry.disable(old_name)
            repair_worker = WorkerExecutor(repair_registry, planner=repairer, event_sink=disable_old)
            repair_result = repair_worker.execute(repair_base)
            assert repair_result.status == TaskStatus.SUCCEEDED and new_calls == [1]
            assert repair_worker.current_task.repair_attempts == 1 and repairer.calls == 1

            repairer.invalid = True
            loop_worker = WorkerExecutor(repair_registry, planner=repairer, event_sink=disable_old)
            loop_result = loop_worker.execute(repair_base)
            assert loop_result.status == TaskStatus.BLOCKED and repairer.calls == 2
            assert loop_worker.current_task.repair_attempts == MAX_PLAN_REPAIRS

            # O. PRIVATE planning resolves only to local Qwen and rejects remote-data tools.
            private_registry = ToolRegistry()
            private_name = _register(private_registry, "local_read", lambda _args: "local", capability="general")
            remote_name = _register(private_registry, "remote", lambda _args: "remote", namespace="mcp",
                                   capability="mcp", risk=RiskLevel.READ_ONLY, side_effects="remote_read",
                                   transmits_data=True, privacy=frozenset({"public"}))
            private_json = json.dumps({"steps": [{
                "id": "local", "description": "Read local item.", "profile": "GENERAL", "capability": "general",
                "preferred_tool": private_name, "arguments": {}, "dependencies": [],
            }]})
            private_planner = Planner(None, private_registry, model_call=lambda *_args: private_json)
            private_plan = private_planner.plan("Use private local context", privacy="local_only")
            assert private_planner.last_selection.policy == ModelPolicy.PRIVATE
            assert private_planner.last_selection.preferred_backend == "local"
            assert private_planner.last_selection.targets == (private_planner.last_selection.targets[0],)
            assert private_planner.last_selection.targets[0].backend == "local"
            private_validator = PlanValidator(private_registry)
            _assert_rejected(private_validator, _plan(
                _step("remote", remote_name, capability="mcp", arguments={}), privacy="private",
            ), "PRIVACY_DENIED")
            assert private_registry.receipts == ()

            class _LocalProvider:
                name = "local"
                is_local = True
                def __init__(self, response):
                    from types import SimpleNamespace
                    from friday.lifecycle import LocalBrainState
                    self.response = response
                    self.calls = 0
                    self.brain = SimpleNamespace(model="Qwen3.5-4B", messages=lambda command, history: [
                        {"role": "system", "content": "FRIDAY test system"}, *history,
                        {"role": "user", "content": command},
                    ])
                    self.lifecycle = SimpleNamespace(health=lambda: False, state=LocalBrainState.OFF)
                def available(self): return True
                def health(self): return "OFF"
                def chat(self, _messages):
                    self.calls += 1
                    return BrainReply(self.response, provider="local", model="Qwen3.5-4B")
                def close(self): return
            class _RemoteProvider:
                name = "omniroute"
                is_local = False
                def __init__(self): self.calls = 0
                def available(self): return True
                def health(self): return "MOCK"
                def chat(self, _messages):
                    self.calls += 1
                    raise AssertionError("Private planning reached a remote provider.")
                def chat_with_model(self, _messages, _model): return self.chat(_messages)
                def close(self): return
            class _Context:
                def build_context(self, *_args, **kwargs):
                    return type("Context", (), {"privacy": kwargs["privacy_level"], "text": ""})()
            local_provider = _LocalProvider(private_json)
            remote_provider = _RemoteProvider()
            private_brain = BrainManager(
                AssistantProfile(), local_provider=local_provider, omniroute_provider=remote_provider,
                cloud_provider=remote_provider, freellmapi_provider=remote_provider,
                memory_manager=object(), knowledge_vault=object(), unified_manager=_Context(),
            )
            production_private_planner = Planner(private_brain, private_registry)
            import friday.brain_manager as brain_manager_module
            with patch.object(brain_manager_module, "LOG_PATH", temp / "private-brain.log"):
                production_private_plan = production_private_planner.plan("Use private local context", privacy="local_only")
            assert production_private_planner.last_selection.policy == ModelPolicy.PRIVATE
            assert local_provider.calls == 1 and remote_provider.calls == 0
            assert production_private_plan.privacy == "local_only"
            private_brain.close()

            # P. Unified knowledge search runs through the registered knowledge tool.
            from dataclasses import dataclass
            @dataclass
            class _Hit:
                source: str
                title: str
                text: str
                namespace: str = "phase6b"
            class _Unified:
                calls = 0
                def search(self, query, **_kwargs):
                    self.calls += 1
                    return [_Hit("memory", "smoke", query)]
            unified = _Unified()
            knowledge_registry = ToolRegistry()
            register_knowledge_tools(knowledge_registry, unified)
            knowledge_result = WorkerExecutor(knowledge_registry).execute(_plan(_step(
                "search", "knowledge.search", capability="knowledge", profile=WorkerProfile.KNOWLEDGE,
                arguments={"query": "phase 6b", "namespace": "phase6b", "privacy": "public", "max_results": 5},
                verification="non_empty",
            )))
            assert knowledge_result.status == TaskStatus.SUCCEEDED and unified.calls == 1
            assert knowledge_registry.receipts[-1].tool_name == "knowledge.search"
            _assert_rejected(PlanValidator(knowledge_registry), _plan(_step(
                "wrong_namespace", "knowledge.search", capability="knowledge", profile=WorkerProfile.KNOWLEDGE,
                arguments={"query": "private scope", "namespace": "other", "privacy": "public"},
            )), "PRIVACY_DENIED")
            _assert_rejected(PlanValidator(knowledge_registry), _plan(_step(
                "wrong_privacy", "knowledge.search", capability="knowledge", profile=WorkerProfile.KNOWLEDGE,
                arguments={"query": "private scope", "namespace": "phase6b", "privacy": "private"},
            )), "PRIVACY_DENIED")

            # Q. Local stdio MCP add goes through ToolRegistry and returns numeric 5.
            mcp_registry = ToolRegistry()
            native_calls = []
            native_name = _register(mcp_registry, "native", lambda _args: native_calls.append(1) or "native ok")
            mcp = MCPClientManager(mcp_registry)
            mcp_config = MCPServerConfig(
                name="phase6b", command=sys.executable,
                args=(str(ROOT / "tests" / "scripts" / "phase6a_mcp_server.py"),),
                enabled=True, allowed_tools=frozenset({"add"}),
                risk_map={"add": RiskLevel.READ_ONLY.value},
                capability_map={"add": frozenset({"mcp"})},
                privacy_support=frozenset({"public"}), transmits_data=False,
            )
            registered = mcp.connect(mcp_config)
            mcp_name = "mcp.phase6b.add"
            assert mcp_name in registered
            mcp_worker = WorkerExecutor(mcp_registry)
            mcp_result = mcp_worker.execute(_plan(_step(
                "add", mcp_name, capability="mcp", arguments={"a": 2, "b": 3}, verification="numeric_result",
            )))
            assert mcp_result.status == TaskStatus.SUCCEEDED
            assert mcp_result.step_results[-1].tool_result.structured_data == 5
            assert any(receipt.tool_name == mcp_name and receipt.success for receipt in mcp_registry.receipts)

            # R. An unavailable local MCP server fails closed while native tools still work.
            client = mcp.clients["phase6b"]
            assert client.process is not None
            client.process.terminate()
            client.wait_unavailable(2.0)
            failed_mcp = mcp_worker.execute(_plan(_step(
                "add", mcp_name, capability="mcp", arguments={"a": 2, "b": 3}, verification="numeric_result",
            )))
            native_result = mcp_registry.execute(native_name, {}, caller="phase6b-smoke")
            assert failed_mcp.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}
            assert native_result.success and native_calls == [1]
            mcp.disconnect_all()

            # S. Dry run validates/resolves tools and calculates approval with no side effect.
            dry_registry = ToolRegistry()
            dry_calls = []
            dry_name = _register(dry_registry, "reversible", lambda _args: dry_calls.append(1) or "ran",
                                 risk=RiskLevel.REVERSIBLE, side_effects="local_change")
            dry_publish = _register(dry_registry, "publish", lambda _args: dry_calls.append(1) or "ran",
                                    risk=RiskLevel.CONSEQUENTIAL, side_effects="external_publish")
            dry_worker = WorkerExecutor(dry_registry)
            dry_result = dry_worker.execute(_plan(_step("change", dry_name), _step("publish", dry_publish)), dry_run=True)
            assert dry_result.status == TaskStatus.DRY_RUN and not dry_calls
            assert dry_worker.current_task.tool_calls == 0
            assert len(dry_worker.current_task.dry_run_approval_requests) == 1

            # T. ToolRegistry receipts and events contain identifiers/status, never prompt or arguments.
            receipt_data = asdict(basic_registry.receipts[-1])
            assert receipt_data["tool_name"] == read_name and receipt_data["success"]
            assert "arguments" not in receipt_data
            assert any(event.event == "task_planned" for event in read_worker.current_task.events)
            assert any(event.event == "step_completed" for event in read_worker.current_task.events)
            assert all("arguments" not in asdict(event) and "goal" not in asdict(event) for event in read_worker.current_task.events)

            # U-V. Coding handoff is structured and Research reports that live search needs configuration.
            coding_brain = _BrainSpy()
            coding_core = FridayCore(AssistantProfile(), router=create_router(), brain_manager=coding_brain)
            coding_response, _, _ = coding_core.respond("Fix failing tests in my repository.")
            assert "assigned to Codex" in coding_response and coding_brain.calls == 0
            assert coding_core.current_task_run and coding_core.current_task_run.handoff["automatic"] is False
            assert coding_core.current_task_run.handoff["destination"] == "codex"
            assert coding_core.last_task_result.status == TaskStatus.HANDOFF_READY
            assert coding_core.planner.model_calls == 0
            coding_core.close()

            research_brain = _BrainSpy()
            with patch.dict("os.environ", {
                "FRIDAY_RESEARCH_SEARXNG_URL": "",
                "FRIDAY_RESEARCH_BROWSER_SEARCH_ENGINE": "unsupported",
            }):
                # Keep this failure-path assertion offline even when DuckDuckGo is reachable.
                with patch("friday.research.engine.configured_search_providers",
                           return_value=(BrowserSearchProvider("unsupported"),)):
                    research_core = FridayCore(AssistantProfile(), router=create_router(), brain_manager=research_brain)
                    research_response, _, _ = research_core.respond("Research the latest browser testing tools and cite sources.")
                    research_core.close()
            assert "SEARCH_UNAVAILABLE" in research_response
            assert research_brain.calls == 0 and research_core.planner.model_calls == 0
            assert research_core.current_task_run.status == TaskStatus.SUCCEEDED

            # W. Current-task state exposes the executing step without arguments or goal text.
            state_registry = ToolRegistry()
            observed = []
            state_worker = None
            def observe(_args):
                observed.append(state_worker.current_state())
                return "observed"
            state_name = _register(state_registry, "observe", observe)
            state_worker = WorkerExecutor(state_registry)
            state_result = state_worker.execute(_plan(_step("current", state_name), goal="private state must not leak"))
            assert state_result.status == TaskStatus.SUCCEEDED
            assert observed[0]["current_step_id"] == "current" and observed[0]["status"] == "RUNNING"
            assert "goal" not in observed[0] and "arguments" not in observed[0]

            # Verify safe retry and executor timing; resource estimate is intentionally approximate.
            executor_overhead_ms = max(0.0, retry_total_ms - sum(item.tool_result.duration_ms for item in retry_result.step_results if item.tool_result))
            run = read_worker.current_task
            approximate_run_bytes = (
                sys.getsizeof(run) + sys.getsizeof(run.events) + sys.getsizeof(run.step_results)
                + sum(sys.getsizeof(event) for event in run.events)
                + sum(sys.getsizeof(item) for item in run.step_results)
            )
            _, peak_memory = tracemalloc.get_traced_memory()
            assert network_calls == 0

    finally:
        tracemalloc.stop()

    print("FRIDAY PHASE 6B PASS")
    print(f"Limits: steps={MAX_PLAN_STEPS}, tool_calls={MAX_TOOL_CALLS}, retries={MAX_STEP_RETRIES}, repairs={MAX_PLAN_REPAIRS}")
    print("Checks: direct-computer, multi-step, validation, approval, cancellation, retry, repair, privacy, knowledge, local-mcp, dry-run, receipts, handoff, research, task-state: PASS")
    print(f"Latency: fake planning={planning_ms:.3f}ms, validation={validation_ms:.3f}ms, executor overhead={executor_overhead_ms:.3f}ms")
    print(f"Approximate resources: one task run={approximate_run_bytes} bytes shallow; smoke peak={peak_memory} bytes")
    print(f"External network calls: {network_calls}")


if __name__ == "__main__":
    run()
