"""Application service joining routing, the local brain, and conversation state."""

from __future__ import annotations

from friday.brain import LOG_PATH, LocalQwenBrain
from friday.conversation import ConversationHistory
from friday.profile import AssistantProfile
from friday.router import RoutingDecision, Router, create_router


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

    def respond(self, command: str) -> tuple[str, int | None, RoutingDecision]:
        decision = self._router.route(command)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"route={decision.route.value} source={decision.source} "
                f"confidence={decision.confidence:.2f} reason={decision.reason}\n"
            )

        answer, prompt_tokens, completion_tokens = self._brain.respond(
            command, self._history.messages()
        )
        self._history.add_exchange(command, answer)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"prompt_tokens={prompt_tokens} "
                f"output_tokens={completion_tokens if completion_tokens is not None else 'unknown'}\n"
            )
        return answer, prompt_tokens, decision
