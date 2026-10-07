"""Fast assertion-based checks for safe grounded computer actions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.capabilities.computer.grounded import TargetActionValidator
from friday.capabilities.registry import Risk
from friday.core import FridayCore
from friday.planner.executor import WorkerExecutor
from friday.planner.models import PlanStep, TaskPlan, TaskStatus, WorkerProfile
from friday.planner.validator import PlanValidator
from friday.tools.computer_actions import _visible_text_in_target, register_grounded_computer_tools
from friday.tools.models import RiskLevel
from friday.tools.registry import ToolRegistry
from friday.tools.vision import register_vision_tools
from friday.vision.manager import VisionManager
from friday.vision.models import OcrBlock, ScreenObservation, UiElement, VisionSource, utcnow
from friday.vision.targets import GroundedTarget, TargetResolution, resolve_target


WINDOW = 44001
BOUNDS = (100, 120, 640, 360)


def _observation(*, status="STATUS: READY", field="FRIDAY INPUT", timestamp=None):
    elements = (
        UiElement(status, "Text", "1001", VisionSource.UIA, (130, 150, 560, 40), True, True),
        UiElement("Settings", "Button", "1003", VisionSource.UIA, (170, 220, 150, 44), True, True),
        UiElement("Save Test", "Button", "1002", VisionSource.UIA, (330, 220, 150, 44), True, True),
        UiElement(field, "Edit", "1004", VisionSource.UIA, (500, 220, 205, 44), True, True),
        UiElement("Submit Test", "Button", "1005", VisionSource.UIA, (170, 280, 150, 44), True, True),
        UiElement("Delete Test", "Button", "1006", VisionSource.UIA, (330, 280, 150, 44), True, True),
        UiElement("Open", "Button", "1007", VisionSource.UIA, (500, 280, 80, 44), True, True),
        UiElement("Open", "Button", "1008", VisionSource.UIA, (600, 280, 80, 44), True, True),
    )
    return ScreenObservation(
        timestamp=timestamp or utcnow(), region=None, dimensions=(640, 360),
        active_window="FRIDAY Visual Action Acceptance", uia_elements=elements,
        capture_bounds=BOUNDS, window_bounds=BOUNDS, window_handle=WINDOW,
    )


class FakeVision:
    def __init__(self):
        self.current = _observation()
        self.cache: dict[str, tuple[GroundedTarget, ScreenObservation]] = {}
        self.observe_calls = 0
        self.persisted = 0
        self.remote_calls = 0
        self.memory_writes = 0
        self.vault_writes = 0

    def find_target(self, query, *, observation=None, **_kwargs):
        if query.casefold() == "open":
            return TargetResolution("AMBIGUOUS_TARGET")
        observed = observation or self.current
        result = resolve_target(query, observed)
        if result.status == "SUCCESS" and result.target is not None:
            self.cache[result.target.id] = (result.target, observed)
        return result

    def grounded_target(self, target_id, observation_id=None):
        cached = self.cache.get(target_id)
        if cached is None:
            return None
        target = cached[0]
        return target if observation_id is None or target.observation_timestamp == observation_id else None

    def observation_for_target(self, target_id):
        cached = self.cache.get(target_id)
        return cached[1] if cached else None

    def observe_window(self, _hwnd, *, ocr_mode="auto", persist_screenshot=False, **_kwargs):
        self.observe_calls += 1
        self.persisted += int(persist_screenshot)
        self.remote_calls += self.current.remote_calls
        return self.current

    def observe(self, *, persist_screenshot=False, **kwargs):
        return self.observe_window(WINDOW, persist_screenshot=persist_screenshot, **kwargs)

    def apply_action(self, action):
        if action.operation in {"CLICK", "DOUBLE_CLICK"}:
            x, y = action.parameters["x"], action.parameters["y"]
            if 170 <= x < 320 and 220 <= y < 264:
                status = "STATUS: SETTINGS CLICKED"
                self.current = _observation(status=status, field=self._field())
            elif 170 <= x < 320 and 280 <= y < 324:
                status = "STATUS: SUBMIT TEST CLICKED"
                self.current = _observation(status=status, field=self._field())
            elif 330 <= x < 480 and 220 <= y < 264:
                status = "STATUS: SAVE TEST CLICKED"
                self.current = _observation(status=status, field=self._field())
        elif action.operation == "TYPE_TEXT":
            self.current = _observation(status=self._status(), field=action.target)

    def _status(self):
        return next((item.name for item in self.current.uia_elements if item.identifier == "1001"), "STATUS: READY")

    def _field(self):
        return next((item.name for item in self.current.uia_elements if item.identifier == "1004"), "FRIDAY INPUT")


class MissingEditAfterFocusVision(FakeVision):
    def observe_window(self, hwnd, **kwargs):
        observation = super().observe_window(hwnd, **kwargs)
        return replace(observation, uia_elements=tuple(
            item for item in observation.uia_elements if item.kind != "Edit"
        ))


class FakeComputer:
    def __init__(self, vision):
        self.vision = vision
        self.clicks = 0
        self.calls = 0
        self.fail_after_click = False

    def execute(self, plan, *, approved=False):
        assert approved is True
        self.calls += 1
        for action in plan.actions:
            if action.operation in {"CLICK", "DOUBLE_CLICK"}:
                self.clicks += 2 if action.operation == "DOUBLE_CLICK" else 1
            self.vision.apply_action(action)
            if self.fail_after_click and action.operation == "CLICK":
                self.fail_after_click = False
                raise OSError("simulated post-input backend error")
        return "Executed through the COMPUTER test double.", Risk.SAFE


def _make_target(vision, label, *, timestamp=None, bounds=BOUNDS, center=None):
    observation = _observation(timestamp=timestamp)
    if bounds != BOUNDS:
        observation = replace(observation, window_bounds=bounds, capture_bounds=bounds)
    result = resolve_target(label, observation)
    if result.status != "SUCCESS" or result.target is None:
        raise AssertionError(f"Test fixture did not resolve {label}: {result.status}")
    target = result.target
    if center is not None:
        target = replace(target, center_x=center[0], center_y=center[1])
    vision.cache[target.id] = (target, observation)
    return target, observation


def _args(target):
    return {
        "target_id": target.id,
        "observation_id": target.observation_timestamp,
        "expected_window": WINDOW,
    }


def _registry(vision, computer):
    validator = TargetActionValidator(
        current_window_provider=lambda: (WINDOW, BOUNDS),
        point_window_provider=lambda _x, _y: WINDOW,
    )
    register = ToolRegistry()
    register_grounded_computer_tools(
        register, lambda: vision, lambda: computer, validator=validator,
        focus_verifier=lambda _hwnd, _x, _y: True,
    )
    return register


def main() -> int:
    checks: dict[str, str] = {}
    try:
        vision = FakeVision()
        computer = FakeComputer(vision)
        registry = _registry(vision, computer)
        register_vision_tools(registry, lambda: vision)

        unlabelled_edit = _observation()
        unlabelled_edit = replace(
            unlabelled_edit,
            uia_elements=tuple(replace(item, name="") if item.identifier == "1004" else item
                              for item in unlabelled_edit.uia_elements),
            ocr_blocks=(OcrBlock("FRIDAY INPUT", 0.99, (400, 100, 110, 24)),),
        )
        editable_fusion = resolve_target("FRIDAY INPUT", unlabelled_edit)
        assert editable_fusion.status == "SUCCESS" and editable_fusion.target is not None
        assert editable_fusion.target.source == "FUSED" and editable_fusion.target.control_type == "Edit"
        elsewhere = replace(
            unlabelled_edit,
            ocr_blocks=(OcrBlock("FRIDAY ACTION OK", 0.99, (5, 10, 110, 18)),),
        )
        assert not _visible_text_in_target(elsewhere, editable_fusion.target, "FRIDAY ACTION OK")
        checks["editable_ocr_uia_fusion"] = "PASS"

        settings, _ = _make_target(vision, "Settings")
        click = registry.execute("computer.click_target", _args(settings), caller="phase8a3_smoke")
        assert click.success and click.structured_data["status"] == "VERIFIED"
        assert computer.clicks == 1 and vision.observe_calls == 1
        assert click.structured_data["after_observation_id"] == vision.current.timestamp
        checks["validated_target_click"] = "PASS"
        checks["fresh_post_action_observation"] = "PASS"
        checks["verified_state_change"] = "PASS"

        save_query = registry.execute("computer.click_target", {"query": "Save Test"})
        assert save_query.success and save_query.structured_data["status"] == "VERIFIED"
        checks["safe_query_dispatch"] = "PASS"
        clicks_after_safe_actions = computer.clicks
        core = object.__new__(FridayCore)
        assert core._computer_tool("Click Settings") == ("computer.click_target", {"query": "Settings"})
        checks["deterministic_simple_action_route"] = "PASS"
        plan = TaskPlan("Click Settings", (
            PlanStep("ground", "Find the visible Settings target", "vision", "vision.find_target",
                     {"query": "Settings"}, profile=WorkerProfile.COMPUTER),
            PlanStep("click", "Click the locally grounded Settings target", "computer",
                     "computer.click_target", {"query": "Settings"}, dependencies=("ground",),
                     profile=WorkerProfile.COMPUTER),
        ))
        validated = PlanValidator(registry).validate(plan)
        assert validated.tools["ground"].canonical_name == "vision.find_target"
        assert validated.tools["click"].canonical_name == "computer.click_target"
        checks["planner_vision_and_computer_tools"] = "PASS"

        stale, _ = _make_target(vision, "Settings", timestamp=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())
        stale_result = registry.execute("computer.click_target", _args(stale))
        assert not stale_result.success and stale_result.error_code == "STALE_TARGET" and computer.clicks == clicks_after_safe_actions
        checks["stale_target_rejection"] = "PASS"

        refresh_target, _ = _make_target(
            vision, "Settings", timestamp=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        )
        refreshed = registry.execute(
            "computer.click_target", _args(refresh_target) | {"query": "Settings"},
        )
        assert refreshed.success and refreshed.structured_data["re_resolved"] is True
        checks["one_bounded_stale_reresolution"] = "PASS"
        clicks_after_safe_actions = computer.clicks

        wrong_window = _args(settings) | {"expected_window": WINDOW + 1}
        wrong_result = registry.execute("computer.click_target", wrong_window)
        assert not wrong_result.success and wrong_result.error_code == "WINDOW_CHANGED" and computer.clicks == clicks_after_safe_actions
        checks["wrong_window_rejection"] = "PASS"

        ambiguous = registry.execute("computer.click_target", {"query": "Open"}, approval_callback=lambda _r: True)
        assert not ambiguous.success and ambiguous.error_code == "AMBIGUOUS_TARGET" and computer.clicks == clicks_after_safe_actions
        checks["ambiguity_rejection"] = "PASS"

        outside, _ = _make_target(vision, "Settings", center=(BOUNDS[0] + BOUNDS[2] + 5, BOUNDS[1] + 5))
        out_result = registry.execute("computer.click_target", _args(outside))
        assert not out_result.success and out_result.error_code == "INVALID_TARGET" and computer.clicks == clicks_after_safe_actions
        checks["out_of_bounds_rejection"] = "PASS"

        submit, _ = _make_target(vision, "Submit Test")
        submit_plan = TaskPlan("Click Submit Test", (PlanStep(
            "submit", "Click the grounded Submit Test button", "computer",
            "computer.click_target", _args(submit), profile=WorkerProfile.COMPUTER,
        ),))
        worker = WorkerExecutor(registry)
        waiting = worker.execute(submit_plan)
        assert waiting.status == TaskStatus.WAITING_APPROVAL and computer.clicks == clicks_after_safe_actions
        checks["consequential_waits_for_approval"] = "PASS"
        approved = worker.approve(waiting.task_id, "YES")
        assert approved.status == TaskStatus.SUCCEEDED and computer.clicks == clicks_after_safe_actions + 1
        checks["consequential_approved_one_click"] = "PASS"

        delete, _ = _make_target(vision, "Delete Test")
        destructive = registry.execute("computer.click_target", _args(delete))
        assert destructive.requires_followup and computer.clicks == clicks_after_safe_actions + 1
        checks["destructive_waits_for_approval"] = "PASS"

        no_retry_target, _ = _make_target(vision, "Submit Test")
        computer.fail_after_click = True
        calls_before = computer.calls
        clicks_before = computer.clicks
        no_retry_worker = WorkerExecutor(registry)
        uncertain_run = no_retry_worker.execute(TaskPlan("Click Submit Test once", (PlanStep(
            "submit_once", "Click the grounded Submit Test button once", "computer",
            "computer.click_target", _args(no_retry_target), profile=WorkerProfile.COMPUTER,
        ),)), approval_callback=lambda _request: "YES")
        uncertain = uncertain_run.step_results[0].tool_result
        assert uncertain_run.status == TaskStatus.FAILED
        assert uncertain.error_code == "ACTION_EXECUTED_UNVERIFIED"
        assert computer.calls == calls_before + 1 and uncertain_run.step_results[0].attempt == 1
        assert computer.clicks == clicks_before + 1
        checks["no_consequential_retry"] = "PASS"
        checks["action_executed_unverified"] = "PASS"

        editable, _ = _make_target(vision, "FRIDAY INPUT")
        clicks_before = computer.clicks
        blocked_type = registry.execute(
            "computer.type_into_target", _args(settings) | {"text": "should not type"},
        )
        assert not blocked_type.success and blocked_type.error_code == "INVALID_TARGET"
        assert computer.clicks == clicks_before
        focus = registry.execute("computer.focus_target", _args(editable))
        assert focus.success and focus.structured_data["verification"] == "VERIFIED"
        checks["focus_target"] = "PASS"
        typed = registry.execute(
            "computer.type_into_target", _args(editable) | {"text": "FRIDAY ACTION OK"},
        )
        assert typed.success and typed.structured_data["verification"] == "VERIFIED"
        assert any(item.name == "FRIDAY ACTION OK" for item in vision.current.uia_elements)
        checks["focus_and_type"] = "PASS"
        checks["typing_verification"] = "PASS"

        partial_vision = MissingEditAfterFocusVision()
        partial_target, _ = _make_target(partial_vision, "FRIDAY INPUT")
        partial_computer = FakeComputer(partial_vision)
        partial_registry = _registry(partial_vision, partial_computer)
        partial_run = WorkerExecutor(partial_registry).execute(TaskPlan("Focus then type safely", (
            PlanStep("type", "Focus the grounded editable field and type", "computer",
                     "computer.type_into_target", _args(partial_target) | {"text": "do not retry"},
                     profile=WorkerProfile.COMPUTER),
        )))
        partial_result = partial_run.step_results[0].tool_result
        assert partial_result.error_code == "ACTION_EXECUTED_UNVERIFIED"
        assert partial_computer.clicks == 1 and partial_run.step_results[0].attempt == 1
        checks["focus_type_uncertainty_no_reinput"] = "PASS"

        definitions = {item.canonical_name: item for item in registry.list()}
        click_properties = definitions["computer.click_target"].input_schema["properties"]
        assert "x" not in click_properties and "y" not in click_properties
        assert "computer.click" not in definitions
        checks["raw_coordinate_tool_unavailable"] = "PASS"
        assert any(item.tool_name == "computer.click_target" for item in registry.receipts)
        checks["tool_registry_path"] = "PASS"
        assert any(item.tool_name == "computer.click_target" and item.risk_level == RiskLevel.CONSEQUENTIAL
                   and item.approval_result == "USER_APPROVED" for item in registry.receipts)
        checks["approval_policy_path"] = "PASS"
        assert computer.calls > 0
        checks["existing_computer_dispatch_path"] = "PASS"
        assert vision.remote_calls == 0
        checks["remote_calls"] = "0"
        assert vision.persisted == vision.memory_writes == vision.vault_writes == 0
        checks["persistence_and_memory_writes"] = "0"
        report = {"status": "PASS", "checks": checks,
                  "physical_clicks": computer.clicks,
                  "computer_dispatch_calls": computer.calls,
                  "fresh_observations": vision.observe_calls,
                  "remote_calls": vision.remote_calls,
                  "screenshots_persisted": vision.persisted,
                  "memory_writes": vision.memory_writes,
                  "obsidian_writes": vision.vault_writes}
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "FAIL", "checks": checks,
                          "error": f"{type(error).__name__}: {error}"}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
