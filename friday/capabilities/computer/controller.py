"""Single dispatcher for structured computer actions."""

from __future__ import annotations

from friday.capabilities.registry import Action, ActionPlan, Capability, Risk
from friday.capabilities.computer.apps import AppResolver
from friday.capabilities.computer.browser import BrowserController
from friday.capabilities.computer.input import InputController
from friday.capabilities.computer.parser import DeterministicComputerPlanner
from friday.capabilities.computer.policy import ApprovalPolicy
from friday.capabilities.computer.windows import WindowController


class ComputerController:
    def __init__(self) -> None:
        self.planner = DeterministicComputerPlanner()
        self.policy = ApprovalPolicy()
        self.apps = AppResolver()
        self.browser = BrowserController()
        self._windows: WindowController | None = None
        self._input: InputController | None = None

    def _window_controller(self) -> WindowController:
        if self._windows is None:
            self._windows = WindowController()
        return self._windows

    def _input_controller(self) -> InputController:
        if self._input is None:
            self._input = InputController()
        return self._input

    def execute_request(self, request: str, approved: bool = False) -> tuple[str, Risk]:
        request_risk = self.policy.classify_request(request)
        if request_risk == Risk.BLOCKED:
            return "I can't perform destructive file or data actions.", Risk.BLOCKED
        plan = self.planner.plan(request)
        if plan is None:
            return "I can't reliably turn that computer request into an action yet.", Risk.BLOCKED
        return self.execute(plan, approved=approved, request_risk=request_risk)

    def execute(self, plan: ActionPlan, approved: bool = False, request_risk: Risk = Risk.SAFE) -> tuple[str, Risk]:
        if not plan.actions or len(plan.actions) > 10:
            raise ValueError("An action plan must contain one to ten actions.")
        levels = [self.policy.classify(action) for action in plan.actions]
        if Risk.BLOCKED in levels:
            return "That computer action is blocked by FRIDAY's safety policy.", Risk.BLOCKED
        if request_risk == Risk.CONFIRM or Risk.CONFIRM in levels:
            if not approved:
                return "Approval required before this computer action.", Risk.CONFIRM
        results = [self._run(action) for action in plan.actions]
        return "\n".join(results), Risk.SAFE

    def _run(self, action: Action) -> str:
        op = action.operation.upper()
        if action.capability == Capability.COMPUTER and op == "OPEN_APP":
            app = self.apps.launch(action.target)
            return f"Opened {app.name}."
        if action.capability == Capability.WINDOW:
            if op == "ENUMERATE_WINDOWS":
                return "\n".join(self._window_controller().visible_windows())
            if op == "INSPECT_CONTROLS":
                return "\n".join(str(item) for item in self._window_controller().inspect_controls(action.target))
            if op == "CLICK_CONTROL":
                return self._window_controller().click_control(
                    action.target, str(action.parameters.get("name", "")),
                    str(action.parameters.get("automation_id", "")),
                )
            return self._window_controller().operate(op, action.target)
        if action.capability == Capability.INPUT:
            controller = self._input_controller()
            if op == "TYPE_TEXT":
                return controller.type_text(action.target)
            if op == "PRESS_KEY":
                return controller.press_key(action.target)
            if op == "PRESS_SHORTCUT":
                return controller.press_shortcut(action.target)
            if op in {"CLICK", "DOUBLE_CLICK"}:
                return controller.click(int(action.parameters["x"]), int(action.parameters["y"]), op == "DOUBLE_CLICK")
            if op == "SCROLL":
                return controller.scroll(int(action.parameters.get("steps", 1)))
        if action.capability == Capability.BROWSER:
            if op == "NAVIGATE":
                return self.browser.navigate(action.target)
            if op == "NEW_TAB":
                return self.browser.new_tab()
            if op in {"BACK", "FORWARD", "REFRESH"}:
                return self.browser.history(op)
            if op == "INSPECT_PAGE":
                return "\n".join(str(item) for item in self.browser.inspect_page())
            if op == "READ_VISIBLE_TEXT":
                return self.browser.visible_text()
            if op == "CLICK_ELEMENT":
                return self.browser.click_element(str(action.parameters["role"]), str(action.parameters["name"]))
            if op == "TYPE_ELEMENT":
                return self.browser.type_element(str(action.parameters["role"]), str(action.parameters["name"]), str(action.parameters["text"]))
        raise ValueError(f"Unsupported capability operation: {action.capability.value}/{op}")
