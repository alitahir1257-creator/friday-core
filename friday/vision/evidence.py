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


def explicit_window_info(hwnd: int) -> tuple[str, tuple[int, int, int, int]]:
    """Return a visible top-level window's title and screen bounds by HWND."""
    if type(hwnd) is not int or hwnd <= 0:
        raise EvidenceError("A valid explicit window handle is required.")
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        handle = wintypes.HWND(hwnd)
        root = user32.GetAncestor(handle, 2)
        if (not user32.IsWindow(handle) or not user32.IsWindowVisible(handle)
                or int(root or 0) != hwnd):
            raise EvidenceError("The explicit window handle is stale or not a visible top-level window.")
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            raise EvidenceError("The explicit window bounds are unavailable.")
        bounds = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        if bounds[2] <= 0 or bounds[3] <= 0:
            raise EvidenceError("The explicit window bounds are invalid.")
        length = user32.GetWindowTextLengthW(handle)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            title = buffer.value.strip()
        return title, bounds
    except EvidenceError:
        raise
    except Exception as error:
        raise EvidenceError(f"Explicit window inspection failed: {type(error).__name__}.") from error


def resolve_visible_window_title(title: str) -> int | None:
    """Resolve only one exact visible title; ambiguous titles fail closed."""
    if not isinstance(title, str) or not title.strip():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        user32.EnumWindows.restype = wintypes.BOOL
        matches: list[int] = []

        @callback_type
        def visit(hwnd, _):
            try:
                window_title, _bounds = explicit_window_info(int(hwnd))
                if window_title.casefold() == title.strip().casefold():
                    matches.append(int(hwnd))
            except EvidenceError:
                pass
            return True

        user32.EnumWindows(visit, 0)
        return matches[0] if len(matches) == 1 else None
    except Exception as error:
        raise EvidenceError(f"Window title resolution failed: {type(error).__name__}.") from error


def active_window_title() -> str | None:
    """Foreground window title, or None when unavailable (non-Windows, etc.)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
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


def active_window_identity() -> tuple[int, tuple[int, int, int, int]] | None:
    """Foreground HWND and its screen rectangle; no UIA or screenshot needed."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        _title, bounds = explicit_window_info(int(hwnd))
        return int(hwnd), bounds
    except Exception:
        return None


def collect_uia(window_target: str | None = None, limit: int = 100,
                controller=None, window_handle: int | None = None) -> tuple[UiElement, ...]:
    """Read UIA controls via the existing WindowController.

    ``controller`` may be injected (tests, embedding); otherwise one is built
    lazily. Raises EvidenceError instead of leaking backend tracebacks.
    """
    if type(limit) is not int or limit < 1 or limit > 1000:
        raise EvidenceError("UIA limit must be between 1 and 1000.")
    target = window_target or active_window_title()
    if window_handle is None and not target:
        raise EvidenceError("No window target and no active window found.")
    try:
        if controller is None:
            from friday.capabilities.computer.windows import WindowController

            controller = WindowController()
        if window_handle is None:
            controls = controller.inspect_controls(target, limit=limit)
        else:
            inspect_handle = getattr(controller, "inspect_controls_for_handle", None)
            if inspect_handle is None:
                raise EvidenceError("The UIA controller does not support explicit window handles.")
            controls = inspect_handle(window_handle, limit=limit)
    except EvidenceError:
        raise
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
                                  source=VisionSource.UIA,
                                  bbox=getattr(control, "bbox", None),
                                  visible=getattr(control, "visible", True),
                                  enabled=getattr(control, "enabled", None)))
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
