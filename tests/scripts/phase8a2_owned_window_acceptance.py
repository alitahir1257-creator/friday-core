"""Owned-process Windows fixture and live Phase 8A.2 acceptance.

The child mode creates one native Win32 window with accessible STATIC and
BUTTON controls. The parent discovers it by its PID and exact title, then
uses FRIDAY's existing explicit-HWND capture, UIA, OCR, and grounding paths.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any


TITLE = "FRIDAY Vision Acceptance"
VISION_TEXT = "FRIDAY VISION TARGET TEST"
WINDOW_WIDTH = 620
WINDOW_HEIGHT = 230


class AcceptanceFailure(RuntimeError):
    def __init__(self, layer: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _fixture_window_class():
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", callback_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    return callback_type, WNDCLASSW


def _run_fixture() -> int:
    if os.name != "nt":
        return 2

    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.DefWindowProcW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.PostQuitMessage.argtypes = (ctypes.c_int,)
    callback_type, WNDCLASSW = _fixture_window_class()
    class_name = "FridayVisionAcceptanceFixture"
    WM_DESTROY = 0x0002
    WM_SETFONT = 0x0030
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    SS_CENTER = 0x00000001
    SS_CENTERIMAGE = 0x00000200
    SW_SHOWNOACTIVATE = 4
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = ctypes.c_void_p(-1)

    @callback_type
    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    user32.GetSysColorBrush.argtypes = (wintypes.INT,)
    user32.GetSysColorBrush.restype = wintypes.HBRUSH
    window_class = WNDCLASSW(
        0, window_proc, 0, 0, instance, None, None,
        user32.GetSysColorBrush(5), None, class_name,
    )
    user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
    user32.RegisterClassW.restype = wintypes.ATOM
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        return 3

    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    )
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.UpdateWindow.argtypes = (wintypes.HWND,)
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    gdi32.CreateFontW.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPCWSTR,
    )
    gdi32.CreateFontW.restype = wintypes.HFONT
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)

    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
    x = max(0, (screen_width - WINDOW_WIDTH) // 2)
    y = max(0, (screen_height - WINDOW_HEIGHT) // 3)
    hwnd = user32.CreateWindowExW(
        0, class_name, TITLE, WS_OVERLAPPEDWINDOW,
        x, y, WINDOW_WIDTH, WINDOW_HEIGHT,
        None, None, instance, None,
    )
    if not hwnd:
        return 4

    font = gdi32.CreateFontW(
        -20, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI",
    )
    button_font = gdi32.CreateFontW(
        -19, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI",
    )
    try:
        label = user32.CreateWindowExW(
            0, "STATIC", VISION_TEXT, WS_CHILD | WS_VISIBLE | SS_CENTER | SS_CENTERIMAGE,
            24, 28, 556, 48, hwnd, ctypes.c_void_p(1001), instance, None,
        )
        save_button = user32.CreateWindowExW(
            0, "BUTTON", "Save Test", WS_CHILD | WS_VISIBLE,
            142, 112, 148, 44, hwnd, ctypes.c_void_p(1002), instance, None,
        )
        settings_button = user32.CreateWindowExW(
            0, "BUTTON", "Settings", WS_CHILD | WS_VISIBLE,
            330, 112, 148, 44, hwnd, ctypes.c_void_p(1003), instance, None,
        )
        if not all((label, save_button, settings_button)):
            user32.DestroyWindow(hwnd)
            return 5
        user32.SendMessageW(label, WM_SETFONT, font, 1)
        user32.SendMessageW(save_button, WM_SETFONT, button_font, 1)
        user32.SendMessageW(settings_button, WM_SETFONT, button_font, 1)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, x, y, WINDOW_WIDTH, WINDOW_HEIGHT,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        user32.UpdateWindow(hwnd)
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        title_buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        own_matches = _matching_windows(os.getpid())
        fixture_report = {
            "pid": os.getpid(), "hwnd": int(hwnd), "title": title_buffer.value,
            "visible": bool(user32.IsWindowVisible(hwnd)), "matching_hwnds": own_matches,
        }
        print("FRIDAY_FIXTURE_READY=" + json.dumps(fixture_report), flush=True)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        return 0
    finally:
        if font:
            gdi32.DeleteObject(font)
        if button_font:
            gdi32.DeleteObject(button_font)


def _windows_for_pid(pid: int) -> list[dict[str, Any]]:
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    windows: list[dict[str, Any]] = []

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        windows.append({
            "hwnd": int(hwnd), "title": title.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
        })
        return True

    user32.EnumWindows(visit, 0)
    return windows


def _matching_windows(pid: int) -> list[int]:
    return [window["hwnd"] for window in _windows_for_pid(pid)
            if window["visible"] and window["title"] == TITLE]


def _window_is_live(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    user32 = _user32()
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    return bool(user32.IsWindow(hwnd))


def _forward_child_output(stream, messages: queue.Queue[str]) -> None:
    if stream is not None:
        for line in stream:
            messages.put(line.rstrip())


def _ram_available_mb() -> int | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MEMORYSTATUSEX),)
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    state = MEMORYSTATUSEX()
    state.dwLength = ctypes.sizeof(state)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        return None
    return round(state.ullAvailPhys / (1024 * 1024))


def _process_working_set_mb(pid: int) -> int | None:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return round(counters.WorkingSetSize / (1024 * 1024))
    finally:
        kernel32.CloseHandle(handle)


def _main_acceptance() -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "BLOCKED",
        "fixture_pid": None,
        "fixture_launch_time": None,
        "fixture_hwnd": None,
        "fixture_title": TITLE,
        "fixture_owned_windows_seen": [],
        "fixture_discovery": None,
        "fixture_bounds": None,
        "screenshot_dimensions": None,
        "hwnd_belongs_to_fixture_pid": "FAIL",
        "explicit_window_observation": "FAIL",
        "screenshot": "FAIL",
        "screenshot_latency_ms": None,
        "uia_observation": "FAIL",
        "uia_latency_ms": None,
        "settings": None,
        "save_test": None,
        "ocr_target": None,
        "ocr_detected_text": None,
        "ocr_latency_ms": None,
        "ocr_blocks": [],
        "uia_elements": [],
        "ocr_text_recovered": "FAIL",
        "window_ownership": "FAIL",
        "fusion_dedup": "NOT_APPLICABLE",
        "ambiguity": "NOT_RUN",
        "safe_action": "NOT_RUN",
        "remote_calls": 0,
        "screenshot_persistence": "NO",
        "ocr_auto_memory": "NO",
        "target_auto_memory": "NO",
        "obsidian_writes": 0,
        "starting_available_ram_mb": _ram_available_mb(),
        "fixture_working_set_mb": None,
        "minimum_available_ram_mb": None,
        "ram_after_cleanup_mb": None,
        "fixture_cleanup": "FAIL",
        "genuine_production_defect": "NO",
        "confirmed_defect_layer": None,
        "failure_layer": None,
        "failure_detail": None,
        "additional_dependency_required": "NO",
    }
    samples = [result["starting_available_ram_mb"]]
    process = None
    reader = None
    manager = None
    stage = "FIXTURE"
    fixture_hwnd = None

    try:
        if os.name != "nt":
            raise AcceptanceFailure("FIXTURE", "Windows is required for the owned-window fixture.")

        from friday.capabilities.computer.windows import WindowController
        from friday.vision.capture import ScreenCapture
        from friday.vision.evidence import explicit_window_info
        from friday.vision.manager import VisionManager
        from friday.vision.ocr import OcrEngine
        from friday.vision.targets import (
            box_within, candidates_from_observation, local_to_screen, normalize_label,
        )

        project_root = Path(__file__).resolve().parents[2]

        launch_time = datetime.now(timezone.utc).isoformat()
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpDesktop = r"winsta0\default"
        # The uv venv launcher reports a shim PID; use the base interpreter so
        # Popen.pid is the process that owns the actual window.
        fixture_python = getattr(sys, "_base_executable", None) or sys.executable
        process = subprocess.Popen(
            [fixture_python, str(Path(__file__).resolve()), "--fixture"],
            cwd=project_root,
            shell=False,
            close_fds=True,
            startupinfo=startupinfo,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        result["fixture_pid"] = process.pid
        result["fixture_launch_time"] = launch_time
        child_messages: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(
            target=_forward_child_output, args=(process.stdout, child_messages), daemon=True,
        )
        reader.start()

        deadline = time.monotonic() + 10.0
        matches: list[int] = []
        fixture_report = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            matches = _matching_windows(process.pid)
            try:
                line = child_messages.get_nowait()
                if line.startswith("FRIDAY_FIXTURE_READY="):
                    fixture_report = json.loads(line.partition("=")[2])
            except queue.Empty:
                pass
            if matches or fixture_report is not None:
                break
            time.sleep(0.1)
        if fixture_report is not None:
            child_matches = fixture_report.get("matching_hwnds", [])
            if (fixture_report.get("pid") != process.pid
                    or fixture_report.get("title") != TITLE
                    or not fixture_report.get("visible")
                    or len(child_matches) != 1
                    or child_matches[0] != fixture_report.get("hwnd")):
                raise AcceptanceFailure(
                    "FIXTURE", f"Fixture-process PID/title enumeration was not unique and valid: {fixture_report!r}.",
                )
            if matches and matches != child_matches:
                raise AcceptanceFailure("FIXTURE", "Parent and fixture HWND enumerations disagreed.")
            result["fixture_discovery"] = "PID and exact title; unique EnumWindows match in owned process"
            matches = child_matches
        elif matches:
            result["fixture_discovery"] = "PID and exact title; unique visible EnumWindows match"
        if not matches:
            result["status"] = "FIXTURE_WINDOW_UNAVAILABLE"
            result["fixture_owned_windows_seen"] = _windows_for_pid(process.pid)
            detail = (f"Fixture exited with code {process.returncode}." if process.poll() is not None
                      else "No visible exact-title window owned by the fixture PID.")
            raise AcceptanceFailure("FIXTURE", detail)
        if len(matches) != 1:
            raise AcceptanceFailure("FIXTURE", f"Expected one matching HWND; found {len(matches)}.")

        fixture_hwnd = matches[0]
        result["fixture_hwnd"] = fixture_hwnd
        title, bounds = explicit_window_info(fixture_hwnd)
        owner = wintypes.DWORD()
        user32 = _user32()
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId(fixture_hwnd, ctypes.byref(owner))
        belongs = (
            owner.value == process.pid and title == TITLE
            and user32.IsWindow(fixture_hwnd)
            and user32.IsWindowVisible(fixture_hwnd)
            and bounds[2] > 0 and bounds[3] > 0
        )
        result["fixture_bounds"] = bounds
        result["hwnd_belongs_to_fixture_pid"] = "PASS" if belongs else "FAIL"
        if not belongs:
            raise AcceptanceFailure("FIXTURE", "HWND ownership or visible-window validation failed.")

        result["fixture_working_set_mb"] = _process_working_set_mb(process.pid)
        samples.append(_ram_available_mb())

        class TimedOcrEngine(OcrEngine):
            elapsed_ms: float | None = None

            def read(self, png_bytes):
                started = time.perf_counter()
                try:
                    return super().read(png_bytes)
                finally:
                    self.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        class TimedWindowController:
            def __init__(self) -> None:
                self.controller = WindowController()
                self.elapsed_ms: float | None = None

            def inspect_controls_for_handle(self, hwnd, limit=100):
                started = time.perf_counter()
                try:
                    return self.controller.inspect_controls_for_handle(hwnd, limit=limit)
                finally:
                    self.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        capture = ScreenCapture()
        ocr_engine = TimedOcrEngine()
        manager = VisionManager(capture=capture, ocr=ocr_engine)
        controller = TimedWindowController()

        stage = "CAPTURE"
        screenshot_started = time.perf_counter()
        image = capture.capture_window(fixture_hwnd, persist=False)
        result["screenshot_latency_ms"] = round((time.perf_counter() - screenshot_started) * 1000, 2)
        screenshot_valid = (
            bool(image.png_bytes)
            and image.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            and image.width == bounds[2] and image.height == bounds[3]
            and not image.metadata.persisted and image.metadata.temp_path is None
        )
        result["screenshot_dimensions"] = (image.width, image.height)
        result["screenshot_persistence"] = "NO" if (
            not image.metadata.persisted and image.metadata.temp_path is None
        ) else "YES"
        if not screenshot_valid:
            raise AcceptanceFailure("CAPTURE", "Screenshot was empty, mis-sized, or persisted.")
        result["screenshot"] = "PASS"

        stage = "UIA"
        observation_started = time.perf_counter()
        uia_observation = manager.observe_window(
            fixture_hwnd, ocr_mode="never", uia_controller=controller,
        )
        result["uia_latency_ms"] = controller.elapsed_ms
        result["explicit_window_observation"] = "PASS" if (
            uia_observation.status == "SUCCESS"
            and uia_observation.active_window == TITLE
            and uia_observation.window_handle == fixture_hwnd
            and uia_observation.window_bounds == bounds
            and uia_observation.capture_bounds == bounds
            and uia_observation.dimensions == (bounds[2], bounds[3])
        ) else "FAIL"
        result["uia_observation_latency_total_ms"] = round(
            (time.perf_counter() - observation_started) * 1000, 2,
        )
        if result["explicit_window_observation"] != "PASS":
            raise AcceptanceFailure("CAPTURE", "Explicit-HWND observation did not preserve window bounds.")
        if uia_observation.status != "SUCCESS" or not uia_observation.uia_elements:
            raise AcceptanceFailure("UIA", "Explicit window observation returned no UIA elements.")
        result["uia_elements"] = [
            {"name": item.name, "control_type": item.kind, "bbox": item.bbox}
            for item in uia_observation.uia_elements
        ]
        result["uia_observation"] = "PASS"

        stage = "TARGET_RESOLVER"
        targets: dict[str, Any] = {}
        for query in ("Settings", "Save Test"):
            started = time.perf_counter()
            resolution = manager.find_target(query, observation=uia_observation)
            targets[query] = resolution
            result.setdefault("target_resolution_latency_ms", {})[query] = round(
                (time.perf_counter() - started) * 1000, 3,
            )
            if resolution.status != "SUCCESS" or resolution.target is None:
                if not any(query.casefold() in item.name.casefold() for item in uia_observation.uia_elements):
                    raise AcceptanceFailure("UIA", f"UIA did not expose {query!r}.")
                raise AcceptanceFailure("TARGET_RESOLVER", f"{query} resolution: {resolution.status}.")
            target = resolution.target
            inside = (
                box_within(target.bbox, bounds)
                and box_within((target.center_x, target.center_y, 1, 1), target.bbox)
            )
            result["settings" if query == "Settings" else "save_test"] = {
                "status": resolution.status,
                "source": target.source,
                "label": target.label,
                "bbox": target.bbox,
                "center": (target.center_x, target.center_y),
                "center_inside_bbox": "PASS" if inside else "FAIL",
                "control_type": target.control_type,
                "inside_window": box_within(target.bbox, bounds),
            }
            if target.source != "UIA":
                raise AcceptanceFailure("TARGET_RESOLVER", f"{query} did not resolve from UIA.")
            if not inside:
                raise AcceptanceFailure("COORDINATES", f"{query} center is outside its bounding box.")

        stage = "OCR"
        ocr_observation = manager.observe_window(
            fixture_hwnd, ocr_mode="always", uia_controller=controller,
        )
        result["ocr_latency_ms"] = ocr_engine.elapsed_ms
        result["remote_calls"] = uia_observation.remote_calls + ocr_observation.remote_calls
        if result["remote_calls"] != 0:
            raise AcceptanceFailure("PRIVACY", "Vision observation made a remote model call.")
        result["ocr_blocks"] = [
            {"text": block.text, "confidence": block.confidence, "bbox": block.bbox}
            for block in ocr_observation.ocr_blocks
        ]
        all_targets = [
            target
            for observation in (uia_observation, ocr_observation)
            for target in candidates_from_observation(observation)
        ]
        ownership_violations = [
            {"label": target.label, "bbox": target.bbox}
            for target in all_targets
            if target.window_handle != fixture_hwnd or target.window_bounds != bounds
            or not box_within(target.bbox, bounds)
        ]
        result["ownership_violations"] = ownership_violations
        result["window_ownership"] = "PASS" if not ownership_violations else "FAIL"
        if ownership_violations:
            raise AcceptanceFailure("COORDINATES", "A returned target escaped the fixture window bounds.")
        if ocr_observation.status != "SUCCESS" or ocr_observation.ocr_status.value != "SUCCESS":
            raise AcceptanceFailure("OCR", f"OCR observation status: {ocr_observation.ocr_status.value}.")
        compact = lambda text: normalize_label(text).replace(" ", "")
        exact_blocks = [block for block in ocr_observation.ocr_blocks
                        if compact(block.text) == compact(VISION_TEXT)]
        if not exact_blocks:
            raise AcceptanceFailure("OCR", "RapidOCR did not recover the exact fixture text.")
        if len(exact_blocks) != 1:
            raise AcceptanceFailure("OCR", f"Expected one exact OCR block; found {len(exact_blocks)}.")

        stage = "TARGET_RESOLVER"
        ocr_resolution_started = time.perf_counter()
        ocr_resolution = manager.find_target(VISION_TEXT, observation=ocr_observation)
        result.setdefault("target_resolution_latency_ms", {})[VISION_TEXT] = round(
            (time.perf_counter() - ocr_resolution_started) * 1000, 3,
        )
        if ocr_resolution.status != "SUCCESS" or ocr_resolution.target is None:
            raise AcceptanceFailure("TARGET_RESOLVER", f"OCR target resolution: {ocr_resolution.status}.")
        ocr_target = ocr_resolution.target
        if ocr_target.source not in ("OCR", "FUSED"):
            raise AcceptanceFailure("TARGET_RESOLVER", f"Unexpected OCR target source: {ocr_target.source}.")

        ocr_block = exact_blocks[0]
        result["ocr_detected_text"] = ocr_block.text
        if normalize_label(ocr_block.text) != normalize_label(VISION_TEXT):
            result["genuine_production_defect"] = "YES"
            result["confirmed_defect_layer"] = "TARGET_RESOLVER"
        ocr_bbox = local_to_screen(
            ocr_block.bbox, ocr_observation.capture_bounds[:2], ocr_observation.dimensions,
        )
        if ocr_bbox is None or not box_within(ocr_bbox, bounds):
            raise AcceptanceFailure("COORDINATES", "OCR box could not be converted to screen coordinates.")
        ocr_center = (ocr_bbox[0] + ocr_bbox[2] // 2, ocr_bbox[1] + ocr_bbox[3] // 2)
        ocr_center_inside = box_within((*ocr_center, 1, 1), ocr_bbox)
        result["ocr_text_recovered"] = "PASS"
        result["ocr_target"] = {
            "status": ocr_resolution.status,
            "source": ocr_target.source,
            "label": ocr_target.label,
            "bbox": ocr_bbox,
            "confidence": ocr_block.confidence,
            "center": ocr_center,
            "center_inside_bbox": "PASS" if ocr_center_inside else "FAIL",
            "target_bbox": ocr_target.bbox,
            "target_center": (ocr_target.center_x, ocr_target.center_y),
        }
        if not ocr_center_inside:
            raise AcceptanceFailure("COORDINATES", "OCR center is outside its bounding box.")

        stage = "FUSION"
        exact_uia = [
            item for item in ocr_observation.uia_elements
            if normalize_label(item.name) == normalize_label(VISION_TEXT)
        ]
        if exact_uia:
            matching_targets = [
                target for target in candidates_from_observation(ocr_observation)
                if target.normalized_label == ocr_target.normalized_label
            ]
            result["fusion_dedup"] = "PASS" if (
                len(matching_targets) == 1 and matching_targets[0].source == "FUSED"
                and matching_targets[0].metadata.get("provenance") == ["UIA", "OCR"]
            ) else "FAIL"
            if result["fusion_dedup"] == "FAIL":
                raise AcceptanceFailure("FUSION", "Co-located UIA/OCR evidence was not fused once with provenance.")
        else:
            result["fusion_dedup"] = "NOT_APPLICABLE"

        stage = "COORDINATES"
        from friday.vision.targets import validate_target
        for resolution in (*targets.values(), ocr_resolution):
            if not validate_target(resolution.target, current_window=(fixture_hwnd, bounds)):
                raise AcceptanceFailure("COORDINATES", "Grounded target failed explicit-HWND validation.")

        result["screenshot_persistence"] = "NO"
        result["ocr_auto_memory"] = "NO"
        result["target_auto_memory"] = "NO"
        result["obsidian_writes"] = 0
        result["status"] = "PASS"
        samples.append(_ram_available_mb())
    except AcceptanceFailure as error:
        result["failure_layer"] = error.layer
        result["failure_detail"] = str(error)
        if result["status"] != "FIXTURE_WINDOW_UNAVAILABLE":
            result["status"] = "BLOCKED"
        if error.layer in {"CAPTURE", "UIA", "OCR", "COORDINATES", "TARGET_RESOLVER", "FUSION"}:
            result["genuine_production_defect"] = "YES"
    except Exception as error:
        result["failure_layer"] = stage
        result["failure_detail"] = f"{type(error).__name__}: {error}"
        if result["status"] != "FIXTURE_WINDOW_UNAVAILABLE" and result["fixture_hwnd"]:
            if stage in {"CAPTURE", "UIA", "OCR", "COORDINATES", "TARGET_RESOLVER", "FUSION"}:
                result["genuine_production_defect"] = "YES"
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception as error:
                if result["status"] == "PASS":
                    result["status"] = "BLOCKED"
                    result["failure_layer"] = "CLEANUP"
                    result["failure_detail"] = f"Manager cleanup failed: {type(error).__name__}"
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            gone = process.poll() is not None and not _window_is_live(fixture_hwnd)
            deadline = time.monotonic() + 3
            while (process.poll() is not None and time.monotonic() < deadline
                   and (_window_is_live(fixture_hwnd) or _matching_windows(process.pid))):
                time.sleep(0.1)
            gone = (process.poll() is not None and not _window_is_live(fixture_hwnd)
                    and not _matching_windows(process.pid))
            result["fixture_cleanup"] = "PASS" if gone else "FAIL"
            if not gone and result["status"] == "PASS":
                result["status"] = "BLOCKED"
                result["failure_layer"] = "CLEANUP"
                result["failure_detail"] = "Owned fixture process or its title window remained after termination."
            if process.stdout is not None:
                process.stdout.close()
            if reader is not None:
                reader.join(timeout=1)
        result["ram_after_cleanup_mb"] = _ram_available_mb()
        samples.append(result["ram_after_cleanup_mb"])
        available_samples = [sample for sample in samples if sample is not None]
        result["minimum_available_ram_mb"] = min(available_samples) if available_samples else None

    return result


if __name__ == "__main__":
    if "--fixture" in sys.argv:
        raise SystemExit(_run_fixture())

    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    report = _main_acceptance()
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "PASS" else 2)
