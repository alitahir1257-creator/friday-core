"""ToolRegistry adapters for safe actions on locally grounded targets."""

from __future__ import annotations

from collections.abc import Callable
import re
import time
from typing import Any

from friday.capabilities.computer.controller import ComputerController
from friday.capabilities.computer.grounded import (
    GroundedActionError,
    GroundedActionRequest,
    TargetActionValidator,
    focused_control_contains,
    run_computer_action,
)
from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry
from friday.vision.manager import VisionManager
from friday.vision.targets import GroundedTarget, box_within, local_to_screen, normalize_label


_PRIVACY = frozenset({"public", "private", "local_only"})
_DESTRUCTIVE = re.compile(r"\b(delete|remove|erase|uninstall|format|reset|wipe)\b", re.I)
_CONSEQUENTIAL = re.compile(
    r"\b(send|submit|publish|purchase|buy|pay|payment|transfer|confirm|order|commit|merge|install)\b", re.I,
)
_SENSITIVE = re.compile(r"\b(password|passcode|secret|token|credential|card|cvv)\b", re.I)
_RISK_ORDER = {
    RiskLevel.READ_ONLY: 0, RiskLevel.REVERSIBLE: 1, RiskLevel.SENSITIVE: 2,
    RiskLevel.CONSEQUENTIAL: 3, RiskLevel.DESTRUCTIVE: 4,
}


def _semantic_risk(target: GroundedTarget | str | None, action: str,
                   configured: RiskLevel = RiskLevel.REVERSIBLE) -> RiskLevel:
    hint = RiskLevel.CONSEQUENTIAL
    label = target.label if isinstance(target, GroundedTarget) else target
    if isinstance(label, str) and label.strip():
        hint = configured
        if _DESTRUCTIVE.search(label):
            hint = RiskLevel.DESTRUCTIVE
        elif _CONSEQUENTIAL.search(label) or (action == "FOCUS_AND_TYPE" and _SENSITIVE.search(label)):
            hint = RiskLevel.CONSEQUENTIAL
    return max((configured, hint), key=_RISK_ORDER.__getitem__)


def _signature(observation) -> tuple:
    elements = tuple((item.name, item.kind, item.identifier, item.bbox, item.visible, item.enabled)
                     for item in observation.uia_elements)
    ocr = tuple(normalize_label(item.text) for item in observation.ocr_blocks)
    return observation.active_window, observation.window_handle, observation.window_bounds, elements, ocr


def _visible_text_in_target(observation, target: GroundedTarget, expected: str) -> bool:
    wanted = " ".join(expected.split()).casefold()
    if not wanted:
        return False
    identifier = target.metadata.get("automation_id")
    bounds = target.metadata.get("control_bbox", target.bbox)
    for element in observation.uia_elements:
        same_control = ((identifier and element.identifier == identifier)
                        or element.bbox == bounds)
        if (same_control and element.visible and element.enabled is not False
                and wanted in " ".join(element.name.split()).casefold()):
            return True
    if observation.capture_bounds is None or observation.dimensions is None:
        return False
    origin = observation.capture_bounds[:2]
    for block in observation.ocr_blocks:
        box = local_to_screen(block.bbox, origin, observation.dimensions)
        if box is None:
            continue
        center = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        if (box_within((center[0], center[1], 1, 1), bounds)
                and wanted in " ".join(block.text.split()).casefold()):
            return True
    return False


def _target_control_remains(target: GroundedTarget, observation) -> bool:
    identifier = target.metadata.get("automation_id")
    bounds = target.metadata.get("control_bbox", target.bbox)
    matches = [
        element for element in observation.uia_elements
        if element.visible and element.enabled is not False
        and (element.kind or "").casefold() == (target.control_type or "").casefold()
        and ((identifier and element.identifier == identifier) or element.bbox == bounds)
    ]
    return len(matches) == 1


class GroundedComputerActions:
    def __init__(self, vision_provider: Callable[[], VisionManager],
                 computer_provider: Callable[[], ComputerController],
                 validator: TargetActionValidator | None = None,
                 focus_verifier: Callable[[int, int, int], bool] | None = None) -> None:
        self.vision_provider = vision_provider
        self.computer_provider = computer_provider
        self.validator = validator or TargetActionValidator()
        self.focus_verifier = focus_verifier or focused_control_contains

    def risk(self, action: str, arguments: dict[str, Any], _context: ToolInvocationContext) -> RiskLevel:
        if self.vision_provider is None or not isinstance(arguments, dict):
            return RiskLevel.CONSEQUENTIAL
        target_id = arguments.get("target_id")
        target = self.vision_provider().grounded_target(target_id) if isinstance(target_id, str) else None
        if target is None:
            target = arguments.get("query") if isinstance(arguments.get("query"), str) else None
        return _semantic_risk(target, action)

    def execute(self, action: str, arguments: dict[str, Any],
                context: ToolInvocationContext) -> dict[str, Any]:
        vision = self.vision_provider()
        target, before = self._resolve(vision, arguments)
        if target is None or before is None:
            raise GroundedActionError("INVALID_TARGET", {"status": "INVALID_TARGET"})

        requested_query = arguments.get("query")
        if requested_query is not None and normalize_label(requested_query) != target.normalized_label:
            raise GroundedActionError("INVALID_TARGET", {"status": "INVALID_TARGET"})
        expected_window = arguments.get("expected_window", target.window_handle)
        if type(expected_window) is not int:
            raise GroundedActionError("WINDOW_CHANGED", {"status": "WINDOW_CHANGED"})
        text = arguments.get("text") if action == "FOCUS_AND_TYPE" else None
        request = GroundedActionRequest(
            action=action, target=target, expected_window=expected_window,
            observation_id=arguments.get("observation_id", target.observation_timestamp),
            text=text, caller=context.caller, privacy=context.privacy,
            metadata={"expected_window_change": arguments.get("expect_window_change", False)},
        )

        validation_started = time.perf_counter()
        validation = self.validator.validate(request)
        re_resolved = False
        if validation.status == "STALE_TARGET" and requested_query:
            # One exact-label refresh is permitted when the caller supplied the
            # original label. A stale target ID by itself always fails closed.
            resolution = vision.find_target(requested_query, window_handle=target.window_handle)
            re_resolved = True
            if resolution.status != "SUCCESS" or resolution.target is None:
                code = "AMBIGUOUS_TARGET" if resolution.status == "AMBIGUOUS_TARGET" else "STALE_TARGET"
                raise GroundedActionError(code, {"status": code, "re_resolved": True})
            target = resolution.target
            before = vision.observation_for_target(target.id)
            request = GroundedActionRequest(
                action=action, target=target, expected_window=expected_window,
                observation_id=target.observation_timestamp, text=text,
                caller=context.caller, privacy=context.privacy,
                metadata=request.metadata,
            )
            validation = self.validator.validate(request)
        validation_ms = round((time.perf_counter() - validation_started) * 1000, 3)
        if not validation.valid:
            raise GroundedActionError(validation.status, {
                "status": validation.status, "target_id": target.id,
                "validation_latency_ms": validation_ms, "re_resolved": re_resolved,
            })

        x, y = validation.coordinate
        action_started = time.perf_counter()
        performed = False
        observation_ms = 0.0
        try:
            if action == "FOCUS_AND_TYPE":
                performed = True
                run_computer_action(self.computer_provider(), "FOCUS", x=x, y=y)
                focus_observation, observation_ms = self._fresh_observation(vision, expected_window)
                if (focus_observation is None
                        or not self.focus_verifier(expected_window, x, y)):
                    raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                        "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                        "focus_verified": False, "typing_started": False,
                        "validation_latency_ms": validation_ms,
                        "action_latency_ms": round((time.perf_counter() - action_started) * 1000, 3),
                        "post_action_observation_latency_ms": observation_ms,
                    })
                if not _target_control_remains(target, focus_observation):
                    raise GroundedActionError(
                        "ACTION_EXECUTED_UNVERIFIED",
                        {"status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                         "focus_verified": True, "typing_started": False},
                    )
                request = GroundedActionRequest(
                    action=action, target=target, expected_window=expected_window,
                    observation_id=target.observation_timestamp, text=text,
                    caller=context.caller, privacy=context.privacy,
                )
                validation = self.validator.validate(request)
                if not validation.valid:
                    raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                        "status": "ACTION_EXECUTED_UNVERIFIED", "failure_status": validation.status,
                        "focus_verified": True, "typing_started": False,
                    })
                x, y = validation.coordinate
                performed = True
                run_computer_action(self.computer_provider(), "TYPE_TEXT", x=x, y=y, text=text)
                action_ms = round((time.perf_counter() - action_started) * 1000, 3)
                post, post_ms = self._fresh_observation(vision, expected_window)
                observation_ms += post_ms
                if post is None:
                    raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                        "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                        "focus_verified": True, "typing_started": True,
                        "validation_latency_ms": validation_ms, "action_latency_ms": action_ms,
                        "post_action_observation_latency_ms": observation_ms,
                    })
                if not _visible_text_in_target(post, target, text or ""):
                    ocr_started = time.perf_counter()
                    post = vision.observe_window(expected_window, ocr_mode="always", persist_screenshot=False)
                    observation_ms += round((time.perf_counter() - ocr_started) * 1000, 3)
                verification_started = time.perf_counter()
                verified = (post.status == "SUCCESS" and post.remote_calls == 0
                            and _visible_text_in_target(post, target, text or ""))
                verification_ms = round((time.perf_counter() - verification_started) * 1000, 3)
                if not verified:
                    raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                        "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                        "focus_verified": True, "typing_started": True,
                        "validation_latency_ms": validation_ms, "action_latency_ms": action_ms,
                        "post_action_observation_latency_ms": observation_ms,
                        "verification_latency_ms": max(0.0, verification_ms),
                    })
                return self._success(action, target, before, post, validation_ms, action_ms,
                                     observation_ms, max(0.0, verification_ms), re_resolved)

            performed = True
            run_computer_action(self.computer_provider(), action, x=x, y=y)
            action_ms = round((time.perf_counter() - action_started) * 1000, 3)
            post, observation_ms = self._fresh_observation(vision, expected_window)
            if post is None or post.remote_calls != 0:
                raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                    "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                    "validation_latency_ms": validation_ms, "action_latency_ms": action_ms,
                    "post_action_observation_latency_ms": observation_ms,
                    "verification_latency_ms": 0.0,
                })
            verification_started = time.perf_counter()
            if action == "FOCUS":
                verified = self.focus_verifier(expected_window, x, y)
            else:
                verified = _signature(before) != _signature(post)
            foreground = self.validator.current_window_provider()
            verification_ms = round((time.perf_counter() - verification_started) * 1000, 3)
            if foreground is not None and foreground[0] != expected_window:
                expected_change = bool(request.metadata.get("expected_window_change"))
                status = "WINDOW_CHANGED_EXPECTED" if expected_change else "WINDOW_CHANGED_UNEXPECTED"
                if expected_change:
                    return self._success(action, target, before, post, validation_ms, action_ms,
                                         observation_ms, max(0.0, verification_ms), re_resolved,
                                         status=status)
                raise GroundedActionError(status, {
                    "status": status, "validation_latency_ms": validation_ms,
                    "action_latency_ms": action_ms,
                    "post_action_observation_latency_ms": observation_ms,
                    "verification_latency_ms": max(0.0, verification_ms),
                })
            if not verified:
                raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                    "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                    "validation_latency_ms": validation_ms, "action_latency_ms": action_ms,
                    "post_action_observation_latency_ms": observation_ms,
                    "verification_latency_ms": max(0.0, verification_ms),
                })
            return self._success(action, target, before, post, validation_ms, action_ms,
                                 observation_ms, max(0.0, verification_ms), re_resolved)
        except GroundedActionError:
            raise
        except Exception as error:
            if performed:
                raise GroundedActionError("ACTION_EXECUTED_UNVERIFIED", {
                    "status": "ACTION_EXECUTED_UNVERIFIED", "action": action,
                    "validation_latency_ms": validation_ms,
                    "action_latency_ms": round((time.perf_counter() - action_started) * 1000, 3),
                    "post_action_observation_latency_ms": observation_ms,
                    "failure": type(error).__name__,
                }) from error
            raise GroundedActionError("INVALID_TARGET", {"status": "INVALID_TARGET"}) from error

    def _resolve(self, vision, arguments: dict[str, Any]):
        target_id = arguments.get("target_id")
        if isinstance(target_id, str):
            target = vision.grounded_target(target_id)
            if target is None:
                raise GroundedActionError("INVALID_TARGET", {"status": "INVALID_TARGET"})
            observation_id = arguments.get("observation_id")
            if observation_id != target.observation_timestamp:
                raise GroundedActionError("STALE_TARGET", {"status": "STALE_TARGET"})
            return target, vision.observation_for_target(target_id)
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise GroundedActionError("INVALID_TARGET", {"status": "INVALID_TARGET"})
        resolution = vision.find_target(query)
        if resolution.status != "SUCCESS" or resolution.target is None:
            raise GroundedActionError(resolution.status, {"status": resolution.status})
        return resolution.target, vision.observation_for_target(resolution.target.id)

    def _fresh_observation(self, vision, expected_window: int):
        started = time.perf_counter()
        foreground = self.validator.current_window_provider()
        if foreground is not None and foreground[0] == expected_window:
            observation = vision.observe_window(expected_window, ocr_mode="auto", persist_screenshot=False)
        else:
            observation = vision.observe(ocr_mode="auto", persist_screenshot=False)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        return (observation if observation.status == "SUCCESS" else None), elapsed

    @staticmethod
    def _success(action, target, before, post, validation_ms, action_ms,
                 observation_ms, verification_ms, re_resolved, status="VERIFIED"):
        return {
            "status": status,
            "verification": status,
            "action": action,
            "target_id": target.id,
            "window_handle": target.window_handle,
            "before_observation_id": before.timestamp,
            "after_observation_id": post.timestamp,
            "validation_latency_ms": validation_ms,
            "action_latency_ms": action_ms,
            "post_action_observation_latency_ms": observation_ms,
            "verification_latency_ms": verification_ms,
            "re_resolved": re_resolved,
            "remote_calls": post.remote_calls,
            "screenshot_persisted": False,
        }


def register_grounded_computer_tools(
    registry: ToolRegistry,
    vision_provider: Callable[[], VisionManager],
    computer_provider: Callable[[], ComputerController],
    *,
    validator: TargetActionValidator | None = None,
    focus_verifier: Callable[[int, int, int], bool] | None = None,
) -> None:
    service = GroundedComputerActions(vision_provider, computer_provider, validator, focus_verifier)
    target_properties = {
        "target_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "observation_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "expected_window": {"type": "integer", "minimum": 1},
        "query": {"type": "string", "minLength": 1, "maxLength": 260},
        "expect_window_change": {"type": "boolean"},
    }

    def register(name: str, action: str, description: str, *, text: bool = False) -> None:
        properties = dict(target_properties)
        required: list[str] = []
        if text:
            properties["text"] = {"type": "string", "minLength": 1, "maxLength": 10000}
            required.append("text")
        registry.register(ToolDefinition(
            name=name, namespace="computer", description=description,
            input_schema={"type": "object", "properties": properties,
                          "required": required, "additionalProperties": False},
            handler=lambda args, context, action=action: service.execute(action, args, context),
            capabilities=frozenset({"computer", "input", "vision"}),
            risk_level=RiskLevel.REVERSIBLE,
            risk_resolver=lambda args, context, action=action: service.risk(action, args, context),
            privacy_support=_PRIVACY, source="native", side_effects="local_input",
            transmits_data=False, timeout_seconds=30.0,
        ))

    register("click_target", "CLICK", "Click the center of a locally grounded visible UI target.")
    register("double_click_target", "DOUBLE_CLICK", "Double-click a locally grounded visible UI target.")
    register("focus_target", "FOCUS", "Focus a locally grounded visible UI target.")
    register("type_into_target", "FOCUS_AND_TYPE", "Focus a locally grounded editable UI target and type literal text.", text=True)
