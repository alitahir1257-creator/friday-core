"""Validated execution of actions against locally grounded screen targets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from friday.capabilities.registry import Action, ActionPlan, Capability, Risk
from friday.vision.evidence import active_window_identity
from friday.vision.targets import GroundedTarget, box_within


@dataclass(frozen=True)
class GroundedActionRequest:
    action: str
    target: GroundedTarget
    expected_window: int
    observation_id: str
    text: str | None = None
    caller: str = "user"
    privacy: str = "local_only"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetActionValidation:
    status: str
    coordinate: tuple[int, int] | None = None

    @property
    def valid(self) -> bool:
        return self.status == "VALID"


def _window_at_point(x: int, y: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.WindowFromPoint.argtypes = (Point,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    child = user32.WindowFromPoint(Point(x, y))
    root = user32.GetAncestor(child, 2) if child else None
    return int(root or child or 0) or None


def focused_control_contains(hwnd: int, x: int, y: int) -> bool:
    """Check that the foreground GUI thread focused the child at the target point."""
    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class GuiThreadInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", Rect)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(GuiThreadInfo))
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(Rect))
    process_id = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not thread_id:
        return False
    info = GuiThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) or not info.hwndFocus:
        return False
    focused = int(info.hwndFocus)
    root = user32.GetAncestor(info.hwndFocus, 2)
    if int(root or info.hwndFocus) != hwnd:
        return False
    rect = Rect()
    if not user32.GetWindowRect(info.hwndFocus, ctypes.byref(rect)):
        return False
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


class TargetActionValidator:
    """Fail-closed boundary before any COMPUTER input event is generated."""

    def __init__(
        self,
        *,
        max_age_seconds: float = 3.0,
        current_window_provider: Callable[[], tuple[int, tuple[int, int, int, int]] | None] | None = None,
        point_window_provider: Callable[[int, int], int | None] | None = None,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.current_window_provider = current_window_provider or active_window_identity
        self.point_window_provider = point_window_provider or _window_at_point

    def validate(
        self,
        request: GroundedActionRequest,
        *,
        now: datetime | None = None,
    ) -> TargetActionValidation:
        if not isinstance(request, GroundedActionRequest):
            return TargetActionValidation("INVALID_TARGET")
        target = getattr(request, "target", None)
        if not isinstance(target, GroundedTarget):
            return TargetActionValidation("INVALID_TARGET")
        if request.action not in {"CLICK", "DOUBLE_CLICK", "FOCUS", "FOCUS_AND_TYPE"}:
            return TargetActionValidation("INVALID_TARGET")
        if request.action == "FOCUS_AND_TYPE":
            if not isinstance(request.text, str) or not request.text or len(request.text) > 10000:
                return TargetActionValidation("INVALID_TARGET")
            if (target.source not in {"UIA", "FUSED"}
                    or (target.control_type or "").casefold() not in {"edit", "combobox", "document"}):
                return TargetActionValidation("INVALID_TARGET")
        elif request.text is not None:
            return TargetActionValidation("INVALID_TARGET")
        if (type(request.expected_window) is not int or request.expected_window <= 0
                or type(target.window_handle) is not int
                or request.expected_window != target.window_handle):
            return TargetActionValidation("WINDOW_CHANGED")
        if request.observation_id != target.observation_timestamp:
            return TargetActionValidation("STALE_TARGET")
        if not target.visible or target.enabled is False:
            return TargetActionValidation("TARGET_NOT_VISIBLE" if not target.visible else "INVALID_TARGET")
        try:
            observed = datetime.fromisoformat(target.observation_timestamp)
            current_time = now or datetime.now(timezone.utc)
            if observed.tzinfo is None or current_time.tzinfo is None:
                return TargetActionValidation("INVALID_TARGET")
            age = (current_time - observed).total_seconds()
        except (AttributeError, TypeError, ValueError):
            return TargetActionValidation("INVALID_TARGET")
        if age < 0 or age > self.max_age_seconds:
            return TargetActionValidation("STALE_TARGET")
        bounds = target.window_bounds
        try:
            valid_geometry = (
                isinstance(bounds, tuple) and len(bounds) == 4
                and isinstance(target.bbox, tuple) and len(target.bbox) == 4
                and box_within(target.bbox, bounds)
                and type(target.center_x) is int and type(target.center_y) is int
                and box_within((target.center_x, target.center_y, 1, 1), target.bbox)
            )
        except (TypeError, ValueError):
            valid_geometry = False
        if not valid_geometry:
            return TargetActionValidation("INVALID_TARGET")
        try:
            current = self.current_window_provider()
            if current is None or current[0] != request.expected_window or current[1] != bounds:
                return TargetActionValidation("WINDOW_CHANGED")
            x, y = target.center_x, target.center_y
            if self.point_window_provider(x, y) != request.expected_window:
                return TargetActionValidation("WINDOW_CHANGED")
        except Exception:
            return TargetActionValidation("WINDOW_CHANGED")
        return TargetActionValidation("VALID", (target.center_x, target.center_y))


class GroundedActionError(RuntimeError):
    def __init__(self, code: str, data: Mapping[str, Any], message: str | None = None) -> None:
        super().__init__(message or code.replace("_", " ").title())
        self.code = code
        self.structured_data = dict(data)


def run_computer_action(controller, operation: str, *, x: int, y: int, text: str | None = None) -> None:
    """Use ComputerController's existing policy/dispatcher/input backend.

    The ToolRegistry has already approved the semantic grounded action. The
    legacy COMPUTER policy marks raw input coordinates confirmation-only, so
    its internal translation is explicitly authorized only after that gate.
    """
    if operation in {"TYPE_TEXT", "FOCUS_AND_TYPE"}:
        actions = ((Action(Capability.INPUT, "CLICK", parameters={"x": x, "y": y}),)
                   if operation == "FOCUS_AND_TYPE" else ())
        actions += (
            Action(Capability.INPUT, "PRESS_SHORTCUT", "CTRL+A"),
            Action(Capability.INPUT, "TYPE_TEXT", text or ""),
        )
    elif operation == "FOCUS":
        actions = (Action(Capability.INPUT, "CLICK", parameters={"x": x, "y": y}),)
    else:
        actions = (Action(Capability.INPUT, operation, parameters={"x": x, "y": y}),)
    _result, risk = controller.execute(ActionPlan(actions), approved=True)
    if risk != Risk.SAFE:
        raise RuntimeError("The existing COMPUTER dispatcher refused grounded input.")
