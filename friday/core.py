"""Application service joining routing, the local brain, and conversation state."""

from __future__ import annotations

from friday.brain import LOG_PATH
from friday.brain_manager import BrainManager
from friday.conversation import ConversationHistory
from friday.laya_client import RemoteLayaClient
from friday.profile import AssistantProfile
from friday.router import RoutingDecision, Router, create_router
from friday.router import Route
from friday.capabilities.registry import Risk
from friday.capabilities.computer.controller import ComputerController
from typing import Callable


class FridayCore:
    def __init__(
        self,
        profile: AssistantProfile,
        router: Router | None = None,
        brain_manager: BrainManager | None = None,
        laya_client: RemoteLayaClient | None = None,
    ) -> None:
        self._router = router if router is not None else create_router()
        self._brain_manager = brain_manager or BrainManager(profile)
        self._laya_client = laya_client or RemoteLayaClient.from_profile(profile.laya_enabled)
        self._history = ConversationHistory(max_turns=6)
        self._computer: ComputerController | None = None
        self.pending_approval: str | None = None

    @property
    def laya_status(self) -> str:
        """Return a secret-free readiness state for the optional remote classifier."""
        return self._laya_client.status

    def respond(self, command: str, on_status: Callable[[str], None] | None = None) -> tuple[str, int | None, RoutingDecision]:
        decision = self._router.route(command)
        ambiguity = getattr(self._router, "ambiguous_routes", None)
        candidates = ambiguity(command) if callable(ambiguity) else None
        if candidates:
            remote_decision = self._laya_client.classify_route(command, candidates)
            if remote_decision is not None:
                decision = RoutingDecision(
                    route=remote_decision.route,
                    confidence=remote_decision.probability,
                    reason="remote System-1 route hint accepted",
                    source="laya",
                )
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"route={decision.route.value} source={decision.source} "
                f"confidence={decision.confidence:.2f} reason={decision.reason}\n"
            )

        if decision.route == Route.COMPUTER:
            if on_status is not None:
                on_status("EXECUTING")
            if self._computer is None:
                self._computer = ComputerController()
            answer, risk = self._computer.execute_request(command)
            self.pending_approval = command if risk == Risk.CONFIRM else None
            return answer, None, decision

        reply = self._brain_manager.respond(command, self._history.messages(), on_status)
        answer = reply.text
        self._history.add_exchange(command, answer)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"brain={reply.provider} prompt_tokens={reply.prompt_tokens} "
                f"output_tokens={reply.completion_tokens if reply.completion_tokens is not None else 'unknown'}\n"
            )
        return answer, reply.prompt_tokens, decision

    def approve_pending(self, approved: bool) -> str:
        command = self.pending_approval
        self.pending_approval = None
        if command is None:
            raise RuntimeError("There is no pending action to approve.")
        if not approved:
            return "Action cancelled."
        if self._computer is None:
            raise RuntimeError("Computer controller is unavailable.")
        answer, _ = self._computer.execute_request(command, approved=True)
        return answer

    def close(self) -> None:
        self._brain_manager.close()
