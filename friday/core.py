"""Application service joining routing, the local brain, and conversation state."""

from __future__ import annotations

from friday.brain import LOG_PATH, LocalQwenBrain
from friday.conversation import ConversationHistory
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
        brain: LocalQwenBrain | None = None,
    ) -> None:
        self._router = router if router is not None else create_router()
        self._brain = brain if brain is not None else LocalQwenBrain(profile)
        self._history = ConversationHistory(max_turns=6)
        self._computer: ComputerController | None = None
        self.pending_approval: str | None = None

    def respond(self, command: str, on_status: Callable[[str], None] | None = None) -> tuple[str, int | None, RoutingDecision]:
        decision = self._router.route(command)
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

        answer, prompt_tokens, completion_tokens = self._brain.respond(command, self._history.messages())
        self._history.add_exchange(command, answer)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"prompt_tokens={prompt_tokens} "
                f"output_tokens={completion_tokens if completion_tokens is not None else 'unknown'}\n"
            )
        return answer, prompt_tokens, decision

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
