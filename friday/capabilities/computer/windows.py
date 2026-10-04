"""Visible-window and accessibility control through Windows UI Automation."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes

from friday.capabilities.computer.apps import normalize


class ControlResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ControlInfo:
    name: str
    control_type: str
    automation_id: str


class WindowController:
    def __init__(self) -> None:
        from pywinauto import Desktop

        self._desktop = Desktop(backend="uia")

    def visible_windows(self) -> list[str]:
        return [w.window_text() for w in self._desktop.windows(visible_only=True) if w.window_text().strip()]

    def _window(self, target: str):
        query = normalize(target).replace("vs code", "visual studio code")
        if not query:
            raise ControlResolutionError("A window name is required.")
        matches = [
            w for w in self._desktop.windows(visible_only=True)
            if query in normalize(w.window_text())
        ]
        if len(matches) > 1:
            user32 = ctypes.WinDLL("user32")
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            foreground = user32.GetForegroundWindow()
            active = [window for window in matches if window.handle == foreground]
            if len(active) == 1:
                return active[0]
        if len(matches) != 1:
            if matches:
                raise ControlResolutionError(f"I found {len(matches)} matching windows. Focus the one you mean first.")
            raise ControlResolutionError(f"I couldn't find a visible window for {target!r}.")
        return matches[0]

    def operate(self, operation: str, target: str) -> str:
        window = self._window(target)
        method = {
            "FOCUS_APP": "set_focus",
            "MINIMIZE_WINDOW": "minimize",
            "MAXIMIZE_WINDOW": "maximize",
            "RESTORE_WINDOW": "restore",
            "CLOSE_WINDOW": "close",
        }.get(operation)
        if method is None:
            raise ValueError(f"Unsupported window operation: {operation}")
        getattr(window, method)()
        return f"{operation.replace('_', ' ').title()}: {window.window_text()}"

    def inspect_controls(self, target: str, limit: int = 100) -> list[ControlInfo]:
        window = self._window(target)
        result: list[ControlInfo] = []
        for control in window.descendants():
            info = control.element_info
            if info.name or info.automation_id:
                result.append(ControlInfo(info.name or "", info.control_type or "", info.automation_id or ""))
            if len(result) >= limit:
                break
        return result

    def click_control(self, target: str, name: str = "", automation_id: str = "") -> str:
        if not name and not automation_id:
            raise ControlResolutionError("An accessible name or automation ID is required.")
        window = self._window(target)
        matches = [
            child for child in window.descendants()
            if (name and normalize(child.element_info.name or "") == normalize(name))
            or (automation_id and child.element_info.automation_id == automation_id)
        ]
        if len(matches) != 1:
            raise ControlResolutionError(
                "I can't reliably identify that control yet." if not matches
                else "I found multiple matching controls."
            )
        matches[0].click_input()
        return f"Clicked {name or automation_id}."
