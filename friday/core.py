"""Application service joining routing, the local brain, and conversation state."""

from __future__ import annotations

from dataclasses import replace
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
from friday.mcp import MCPClientManager
from friday.model_policy import ModelPolicy, ModelPolicyEngine, ModelSelection, PrivacyLevel
from friday.tools.adapters import register_computer_tools, register_knowledge_tools
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
        self.tool_registry = ToolRegistry()
        register_computer_tools(self.tool_registry, self._computer_controller)
        register_knowledge_tools(self.tool_registry, getattr(self._brain_manager, "unified", None))
        self.mcp = MCPClientManager(self.tool_registry)
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
    ) -> tuple[str, int | None, RoutingDecision]:
        decision = self._router.route(command)
        requested_privacy = (
            request_metadata.effective_privacy if request_metadata is not None else None
        )
        privacy = self._model_policy_engine.classify_privacy(command, requested_privacy)
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

    def _computer_controller(self) -> ComputerController:
        if self._computer is None:
            self._computer = ComputerController()
        return self._computer

    def _computer_tool(self, command: str) -> tuple[str, dict]:
        plan = self._computer_controller().planner.plan(command)
        if plan is not None and len(plan.actions) == 1:
            action = plan.actions[0]
            if action.capability == Capability.COMPUTER and action.operation.upper() == "OPEN_APP":
                return "computer.open_app", {"app": action.target}
            if action.capability == Capability.BROWSER and action.operation.upper() == "NAVIGATE":
                return "browser.open_url", {"url": action.target}
        return "computer.dispatch_request", {"request": command}

    def close(self) -> None:
        self.mcp.disconnect_all()
        self._brain_manager.close()
