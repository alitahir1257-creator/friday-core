"""UIA/DOM evidence collection reusing existing FRIDAY controllers.

UIA and DOM stay preferred: OCR is a fallback/supplement, never a
replacement for reliable accessibility or DOM evidence. This module adds no
new control paths; it only reads through the existing WindowController and
BrowserController behind lazy imports, so importing it is safe on any
platform and costs nothing until used.
"""

from __future__ import annotations

from friday.vision.models import UiElement, VisionSource


class EvidenceError(RuntimeError):
    """Deterministic evidence-collection failure."""


def active_window_title() -> str | None:
    """Foreground window title, or None when unavailable (non-Windows, etc.)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        return title or None
    except Exception:
        return None


def collect_uia(window_target: str | None = None, limit: int = 100,
                controller=None) -> tuple[UiElement, ...]:
    """Read UIA controls via the existing WindowController.

    ``controller`` may be injected (tests, embedding); otherwise one is built
    lazily. Raises EvidenceError instead of leaking backend tracebacks.
    """
    if type(limit) is not int or limit < 1 or limit > 1000:
        raise EvidenceError("UIA limit must be between 1 and 1000.")
    target = window_target or active_window_title()
    if not target:
        raise EvidenceError("No window target and no active window found.")
    try:
        if controller is None:
            from friday.capabilities.computer.windows import WindowController

            controller = WindowController()
        controls = controller.inspect_controls(target, limit=limit)
    except Exception as error:
        raise EvidenceError(f"UIA inspection failed: {type(error).__name__}.") from error
    elements: list[UiElement] = []
    for control in controls:
        name = str(getattr(control, "name", "") or "")
        kind = str(getattr(control, "control_type", "") or "")
        identifier = str(getattr(control, "automation_id", "") or "")
        if not name and not identifier:
            continue
        elements.append(UiElement(name=name, kind=kind, identifier=identifier,
                                  source=VisionSource.UIA))
    return tuple(elements)


def collect_dom(limit: int = 80, browser=None) -> tuple[UiElement, ...]:
    """Read page controls via the existing BrowserController.

    ``browser`` may be injected; otherwise one is built lazily. Never starts
    inference, never navigates: pure inspection of the current page.
    """
    if type(limit) is not int or limit < 1 or limit > 1000:
        raise EvidenceError("DOM limit must be between 1 and 1000.")
    try:
        if browser is None:
            from friday.capabilities.computer.browser import BrowserController

            browser = BrowserController()
        nodes = browser.inspect_page(limit=limit)
    except Exception as error:
        raise EvidenceError(f"DOM inspection failed: {type(error).__name__}.") from error
    elements: list[UiElement] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name", "") or "")
        role = str(node.get("role", "") or "")
        if not name:
            continue
        elements.append(UiElement(name=name, kind=role, identifier="",
                                  source=VisionSource.DOM))
    return tuple(elements)
