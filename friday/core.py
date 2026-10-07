"""Application service joining routing, the local brain, and conversation state."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import time

from friday.brain import LOG_PATH
from friday.brain_manager import BrainManager, BrainRequestMetadata
from friday.conversation import ConversationHistory
from friday.laya_client import RemoteLayaClient
from friday.profile import AssistantProfile
from friday.router import RoutingDecision, Router, create_router
from friday.router import Route
from friday.system_one import HostedJevProvider, RemoteLayaProvider, SystemOneManager
from friday.capabilities.computer.controller import ComputerController
from friday.capabilities.registry import Capability
from friday.vision import VisionManager
from friday.mcp import MCPClientManager
from friday.model_policy import ModelPolicy, ModelPolicyEngine, ModelSelection, PrivacyLevel
from friday.planner import (
    Planner,
    TaskRun,
    TaskResult,
    WorkerExecutor,
    WorkerProfile,
    is_document_request,
    is_multi_step_request,
)
from friday.connectors.local_files import ApprovedRoot, LocalFilesConnector
from friday.documents import DocumentManager
from friday.tools.adapters import register_computer_tools, register_knowledge_tools
from friday.tools.vision import register_vision_tools
from friday.tools.computer_actions import register_grounded_computer_tools
from friday.tools.documents import register_document_tools
from friday.tools.research import register_research_tools
from friday.apis.registry import build_default_api_registry
from friday.tools.public_apis import register_public_api_tools
from friday.research.engine import ResearchEngine
from friday.tools.models import ToolResult
from friday.tools.registry import ToolRegistry
from typing import Callable


class FridayCore:
    def __init__(
        self,
        profile: AssistantProfile,
        router: Router | None = None,
        brain_manager: BrainManager | None = None,
        laya_client: RemoteLayaClient | None = None,
        systemone_manager: SystemOneManager | None = None,
        model_policy_engine: ModelPolicyEngine | None = None,
        planner: Planner | None = None,
        worker_executor: WorkerExecutor | None = None,
        research_engine: ResearchEngine | None = None,
    ) -> None:
        self._router = router if router is not None else create_router()
        self._brain_manager = brain_manager or BrainManager(profile)
        self._laya_client = laya_client or RemoteLayaClient.from_profile(profile.laya_enabled)
        self._systemone_manager = systemone_manager or SystemOneManager(
            laya_provider=RemoteLayaProvider(self._laya_client),
            jev_provider=HostedJevProvider.from_environment(),
        )
        self._model_policy_engine = model_policy_engine or ModelPolicyEngine()
        self._history = ConversationHistory(max_turns=6)
        self._computer: ComputerController | None = None
        self._vision: VisionManager | None = None
        self.tool_registry = ToolRegistry()
        register_computer_tools(self.tool_registry, self._computer_controller)
        register_vision_tools(self.tool_registry, self._vision_manager)
        register_grounded_computer_tools(
            self.tool_registry, self._vision_manager, self._computer_controller,
        )
        register_knowledge_tools(self.tool_registry, getattr(self._brain_manager, "unified", None))
        configured_roots = getattr(profile, "document_roots", (r"D:\FRIDAY\documents",))
        public_roots = getattr(profile, "public_document_roots", ())
        approved_roots = tuple(
            ApprovedRoot(root, namespace="friday", privacy=privacy)
            for privacy, paths in (("private", configured_roots), ("public", public_roots))
            for root in paths if Path(root).is_dir()
        )
        self.document_files = LocalFilesConnector(
            approved_roots, enabled=bool(approved_roots), max_file_bytes=20 * 1024 * 1024,
        )
        self.research_engine = research_engine or ResearchEngine(self._brain_manager)
        register_research_tools(self.tool_registry, self.research_engine)
        self.api_registry = build_default_api_registry()
        register_public_api_tools(self.tool_registry, self.api_registry, self.research_engine)
        self.documents = DocumentManager(self.document_files, self._brain_manager, self.research_engine)
        register_document_tools(self.tool_registry, self.documents)
        self.mcp = MCPClientManager(self.tool_registry)
        memory = getattr(self._brain_manager, "memory", None)
        self.planner = planner or Planner(self._brain_manager, self.tool_registry, self._model_policy_engine,
                                          experience=getattr(memory, "experience", None))
        self.worker_executor = worker_executor or WorkerExecutor(
            self.tool_registry, planner=self.planner,
        )
        self.current_task_run: TaskRun | None = None
        self.last_task_result: TaskResult | None = None
        self.last_tool_result: ToolResult | None = None
        self.pending_approval: str | None = None
        self._pending_tool_invocation: tuple[str, dict, str, dict] | None = None
        self._last_model_selection: ModelSelection | None = None

    @property
    def last_model_selection(self) -> ModelSelection | None:
        """Return the latest secret-free policy and actual route metadata."""
        return self._last_model_selection

    @property
    def laya_status(self) -> str:
        """Return a secret-free readiness state for the optional remote classifier."""
        return self._laya_client.status

    @property
    def systemone_status(self) -> str:
        """Return safe status metadata for whichever typed decision providers are configured."""
        return self._systemone_manager.status

    def respond(
        self,
        command: str,
        on_status: Callable[[str], None] | None = None,
        request_metadata: BrainRequestMetadata | None = None,
        *,
        dry_run: bool = False,
        approval_callback: Callable[[Any], str] | None = None,
    ) -> tuple[str, int | None, RoutingDecision]:
        decision = self._router.route(command)
        requested_privacy = (
            request_metadata.effective_privacy if request_metadata is not None else None
        )
        privacy = self._model_policy_engine.classify_privacy(command, requested_privacy)
        document_request = is_document_request(command)
        explicit_public_document = (
            request_metadata is not None
            and request_metadata.sensitivity == "public"
            and request_metadata.privacy == "public"
            and request_metadata.task_class != ModelPolicy.PRIVATE.value
        )
        if document_request and privacy == PrivacyLevel.PUBLIC and not explicit_public_document:
            privacy = PrivacyLevel.PRIVATE

        selection = self._model_policy_engine.select(
            command,
            route_hint=decision.route,
            privacy=privacy,
            requested_policy=request_metadata.task_class if request_metadata is not None else None,
            cloudflare_enabled=bool(getattr(self._brain_manager, "cloud_enabled", False)),
        )
        multi_step = is_multi_step_request(command) or document_request
        if decision.route == Route.COMPUTER:
            deterministic_plan = self._deterministic_computer_plan(command)
            multi_step = multi_step or bool(
                deterministic_plan is not None and len(deterministic_plan.actions) > 1
            )

        namespace = request_metadata.memory_namespace if request_metadata is not None else "global"
        if selection.policy == ModelPolicy.CODING:
            decision = RoutingDecision(
                Route.CODING, 1.0, selection.reason, "model_policy",
            )
            handoff = self.planner.coding_handoff(command, privacy=privacy.value, namespace=namespace)
            result = self.worker_executor.execute(handoff)
            self.current_task_run = self.worker_executor.current_task
            self.last_task_result = result
            self._last_model_selection = replace(
                selection, selected_backend="codex", selected_model="codex", latency_ms=0.0,
            )
            return "Repository work is assigned to Codex; FRIDAY chat fallback is disabled.", None, decision

        research_requested = (
            selection.policy == ModelPolicy.RESEARCH
            or self._model_policy_engine.requires_research(command)
        )
        structured_api_requested = self.planner.has_public_api_intent(command, privacy=privacy.value)
        if (
            research_requested
            and not self.tool_registry.find_by_capability("research")
            and not self._is_knowledge_lookup(command)
        ):
            unavailable = self.planner.capability_placeholder(
                command, capability="research", profile=WorkerProfile.RESEARCH,
                privacy=privacy.value, namespace=namespace,
            )
            result = self.worker_executor.execute(unavailable)
            self.current_task_run = self.worker_executor.current_task
            self.last_task_result = result
            self._last_model_selection = selection
            return "Research tools are unavailable; web research is planned for Phase 7A.", None, decision

        is_memory_recall = decision.route == Route.MEMORY and not re.search(
            r"\b(?:remember|yaad\s+rakh)\b", command, re.IGNORECASE,
        )
        if (self._is_knowledge_lookup(command) or is_memory_recall) and not multi_step:
            privacy_value = getattr(privacy, "value", privacy)
            result = self.tool_registry.execute(
                "knowledge.search",
                {"query": command, "namespace": namespace, "privacy": privacy_value, "max_results": 10},
                caller="friday_core",
                privacy=privacy_value,
                action_metadata={"route": decision.route.value, "route_source": decision.source},
            )
            self.last_tool_result = result
            self.pending_approval = command if result.requires_followup else None
            self._pending_tool_invocation = None
            self._last_model_selection = replace(
                selection, selected_backend="tool_registry", selected_model=None, latency_ms=result.duration_ms,
            )
            return result.summary, None, decision

        if multi_step or research_requested or structured_api_requested:
            planning_policy = ModelPolicy.PRIVATE if privacy != PrivacyLevel.PUBLIC else ModelPolicy.SMART
            planning_selection = self._model_policy_engine.select(
                command,
                route_hint=Route.GENERAL,
                privacy=privacy,
                requested_policy=planning_policy,
                cloudflare_enabled=bool(getattr(self._brain_manager, "cloud_enabled", False)),
                planning=True,
            )
            plan = self.planner.plan(command, privacy=privacy.value, namespace=namespace)
            task_result = self.worker_executor.execute(
                plan, dry_run=dry_run, approval_callback=approval_callback,
            )
            self.current_task_run = self.worker_executor.current_task
            self.last_task_result = task_result
            last_result = next(
                (item.tool_result for item in reversed(task_result.step_results) if isinstance(item.tool_result, ToolResult)),
                None,
            )
            if last_result is not None:
                self.last_tool_result = last_result
            self._last_model_selection = self.planner.last_selection or planning_selection
            if (research_requested or structured_api_requested) and not multi_step and last_result is not None:
                return last_result.summary, None, decision
            if document_request and last_result is not None and last_result.success and last_result.tool_name.startswith("documents."):
                rendered = json.dumps(last_result.structured_data, ensure_ascii=False, default=str)
                if len(rendered) > 20_000:
                    rendered = rendered[:20_000] + "\n[Document tool output truncated at 20000 characters.]"
                return rendered, None, decision
            return task_result.summary, None, decision

        ambiguity = getattr(self._router, "ambiguous_routes", None)
        candidates = ambiguity(command) if callable(ambiguity) else None
        if decision.route != Route.COMPUTER and candidates and privacy == PrivacyLevel.PUBLIC:
            remote_decision = self._systemone_manager.classify_route(command, candidates)
            if remote_decision is not None:
                decision = RoutingDecision(
                    route=remote_decision.route,
                    confidence=remote_decision.probability,
                    reason="System-One route hint accepted",
                    source=remote_decision.provider,
                )

        selection = self._model_policy_engine.select(
            command,
            route_hint=decision.route,
            privacy=privacy,
            requested_policy=request_metadata.task_class if request_metadata is not None else None,
            cloudflare_enabled=bool(getattr(self._brain_manager, "cloud_enabled", False)),
        )
        if selection.policy == ModelPolicy.COMPUTER and decision.route != Route.COMPUTER:
            decision = RoutingDecision(
                route=Route.COMPUTER,
                confidence=1.0,
                reason=selection.reason,
                source="model_policy",
            )
        policy_routes = {
            ModelPolicy.CODING: Route.CODING,
            ModelPolicy.RESEARCH: Route.RESEARCH,
        }
        if selection.policy in policy_routes and decision.route != policy_routes[selection.policy]:
            decision = RoutingDecision(
                route=policy_routes[selection.policy],
                confidence=1.0,
                reason=selection.reason,
                source="model_policy",
            )
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"route={decision.route.value} source={decision.source} "
                f"confidence={decision.confidence:.2f} reason={selection.reason} "
                f"policy={selection.policy.value} privacy={selection.privacy.value}\n"
            )

        if decision.route == Route.COMPUTER:
            started = time.perf_counter()
            if on_status is not None:
                on_status("EXECUTING")
            tool_name, arguments = self._computer_tool(command)
            privacy_value = getattr(selection.privacy, "value", selection.privacy)
            action_metadata = {"route": decision.route.value, "route_source": decision.source}
            result = self.tool_registry.execute(
                tool_name, arguments, caller="friday_core", privacy=privacy_value,
                action_metadata=action_metadata,
            )
            self.last_tool_result = result
            self.pending_approval = command if result.requires_followup else None
            self._pending_tool_invocation = (
                tool_name, arguments, str(privacy_value), action_metadata
            ) if result.requires_followup else None
            self._last_model_selection = replace(
                selection,
                selected_backend="computer",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return result.summary, None, decision

        reply = self._brain_manager.respond(
            command,
            self._history.messages(),
            on_status,
            request_metadata=request_metadata,
            model_selection=selection,
        )
        self._last_model_selection = reply.selection or selection
        answer = reply.text
        if selection.privacy == PrivacyLevel.PUBLIC and selection.policy != ModelPolicy.CODING:
            self._history.add_exchange(command, answer)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"brain={reply.provider} prompt_tokens={reply.prompt_tokens} "
                f"output_tokens={reply.completion_tokens if reply.completion_tokens is not None else 'unknown'} "
                f"policy={selection.policy.value} backend={reply.provider} "
                f"model={reply.model or 'unknown'} latency_ms={reply.latency_ms if reply.latency_ms is not None else 'unknown'} "
                f"fallback_count={reply.fallback_count}\n"
            )
        return answer, reply.prompt_tokens, decision

    def approve_pending(self, approved: bool) -> str:
        command = self.pending_approval
        invocation = self._pending_tool_invocation
        self.pending_approval = None
        self._pending_tool_invocation = None
        if command is None:
            raise RuntimeError("There is no pending action to approve.")
        if invocation is None:
            raise RuntimeError("The pending tool invocation is unavailable.")
        tool_name, arguments, privacy, action_metadata = invocation
        result = self.tool_registry.execute(
            tool_name, arguments, caller="friday_core", privacy=privacy,
            action_metadata=action_metadata,
            approval_callback=lambda _request: approved,
        )
        self.last_tool_result = result
        return result.summary if approved else "Action cancelled."

    def approve_task(self, task_id: str, answer: str) -> str:
        result = self.worker_executor.approve(task_id, answer)
        self.current_task_run = self.worker_executor.current_task
        return result.summary

    def skip_task_step(self, task_id: str) -> str:
        result = self.worker_executor.skip(task_id)
        self.current_task_run = self.worker_executor.current_task
        return result.summary

    def cancel_task(self, task_id: str | None = None) -> bool:
        cancelled = self.worker_executor.cancel(task_id)
        self.current_task_run = self.worker_executor.current_task
        return cancelled

    def dispatch_automation_computer(self, goal: str, privacy: str, *, approved: bool | None = None) -> ToolResult | None:
        """Run a single deterministic COMPUTER action through ToolRegistry."""
        plan = self._computer_controller().planner.plan(goal)
        if plan is None or len(plan.actions) != 1:
            return None
        tool_name, arguments = self._computer_tool(goal)
        return self.tool_registry.execute(
            tool_name, arguments, caller="friday_automation", privacy=privacy,
            action_metadata={"route": "COMPUTER", "route_source": "deterministic_automation"},
            approval_callback=(lambda _request: approved) if approved is not None else None,
        )

    @property
    def current_task_state(self) -> dict | None:
        return self.worker_executor.current_state()

    @staticmethod
    def _is_knowledge_lookup(command: str) -> bool:
        return bool(re.search(
            r"\b(?:search|find|look up|recall|what did (?:i|we) say|what do i know)\b.{0,100}\b(?:my |our |friday'?s )?"
            r"(?:memory|knowledge|notes?|vault)\b|"
            r"\b(?:memory|knowledge|notes?|vault)\b.{0,100}\b(?:search|find|look up|recall)\b",
            command,
            re.IGNORECASE,
        ))

    def _computer_controller(self) -> ComputerController:
        if self._computer is None:
            self._computer = ComputerController()
        return self._computer

    def _vision_manager(self) -> VisionManager:
        if self._vision is None:
            self._vision = VisionManager()
        return self._vision

    def _computer_tool(self, command: str) -> tuple[str, dict]:
        action = re.fullmatch(
            r"(?:please\s+)?(?:(double\s+click|click|focus)(?:\s+on)?)\s+(.+)",
            command.strip().rstrip(".?!"), re.IGNORECASE,
        )
        if action:
            operation, query = action.group(1).casefold(), action.group(2).strip()
            if query:
                tool = {
                    "double click": "computer.double_click_target",
                    "click": "computer.click_target",
                    "focus": "computer.focus_target",
                }[operation]
                return tool, {"query": query}
        plan = self._deterministic_computer_plan(command)
        if plan is not None and len(plan.actions) == 1:
            action = plan.actions[0]
            if action.capability == Capability.COMPUTER and action.operation.upper() == "OPEN_APP":
                return "computer.open_app", {"app": action.target}
            if action.capability == Capability.BROWSER and action.operation.upper() == "NAVIGATE":
                return "browser.open_url", {"url": action.target}
        return "computer.dispatch_request", {"request": command}

    def _deterministic_computer_plan(self, command: str):
        planner = getattr(self._computer_controller(), "planner", None)
        plan = getattr(planner, "plan", None)
        return plan(command) if callable(plan) else None

    def close(self) -> None:
        self.mcp.disconnect_all()
        self._brain_manager.close()
