"""One local Windows grounding pass using the existing COMPUTER app launcher."""

from __future__ import annotations

import sys
import time
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.capabilities.computer.controller import ComputerController
from friday.vision.evidence import active_window_identity, active_window_title
from friday.vision.manager import VisionManager
from friday.vision.targets import box_within, candidates_from_observation, validate_target


try:
    import psutil
except ImportError:
    psutil = None


def available_mb():
    if psutil:
        return round(psutil.virtual_memory().available / 1048576)
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    state = MEMORYSTATUSEX()
    state.dwLength = ctypes.sizeof(state)
    return round(state.ullAvailPhys / 1048576) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)) else None


samples = [available_mb()]
print("RAM_BEFORE_MB:", samples[-1])
if active_window_identity() is None:
    print("LOCAL_ACCEPTANCE_REQUIRED: no interactive foreground window")
    raise SystemExit(2)
computer = ComputerController()
try:
    if "notepad" not in (active_window_title() or "").casefold():
        result, _ = computer.execute_request("open notepad")
        print("COMPUTER_OPEN:", result)
        time.sleep(1.5)
    if "notepad" not in (active_window_title() or "").casefold():
        raise RuntimeError("Notepad did not become the interactive foreground window")
    manager = VisionManager()
    try:
        start = time.perf_counter()
        observed = manager.observe(ocr_mode="auto")
        observation_ms = round((time.perf_counter() - start) * 1000, 2)
        samples.append(available_mb())
        print("OBSERVATION_EVIDENCE:", len(observed.uia_elements), observed.dimensions,
              observed.capture_bounds, observed.window_bounds, observed.window_handle)
        print("UIA_BOX_SAMPLE:", [(e.kind, e.bbox, e.visible) for e in observed.uia_elements[:6]])
        uia = next((item for item in candidates_from_observation(observed)
                    if item.source == "UIA" and item.label and item.control_type), None)
        if uia is None:
            raise RuntimeError("No bounded UIA target in the active Notepad window")
        start = time.perf_counter()
        grounded = manager.find_target(uia.label, observation=observed)
        resolution_ms = round((time.perf_counter() - start) * 1000, 3)
        if grounded.status != "SUCCESS" or grounded.target is None:
            raise RuntimeError(f"UIA target resolution: {grounded.status}")
        target = grounded.target
        print("ACTIVE_WINDOW:", observed.active_window)
        print("LIVE_LABEL:", target.label)
        print("LIVE_SOURCE:", target.source)
        print("LIVE_BBOX:", target.bbox)
        print("LIVE_CENTER:", (target.center_x, target.center_y))
        print("CENTER_INSIDE:", box_within((target.center_x, target.center_y, 1, 1), target.bbox))
        print("WINDOW_OWNERSHIP:", validate_target(target))
        print("OBSERVATION_MS:", observation_ms)
        print("RESOLUTION_MS:", resolution_ms)
        print("AUTO_OCR_INVOKED:", observed.ocr_status.value == "SUCCESS")

        start = time.perf_counter()
        explicit = manager.observe(ocr_mode="always")
        print("EXPLICIT_OCR_MS:", round((time.perf_counter() - start) * 1000, 2))
        samples.append(available_mb())
        ocr = next((item for item in candidates_from_observation(explicit)
                    if item.source in ("OCR", "FUSED")), None)
        print("OCR_TARGET:", None if ocr is None else (ocr.label, ocr.source, ocr.bbox))
        print("REMOTE_CALLS:", observed.remote_calls + explicit.remote_calls)
        if not validate_target(target):
            raise RuntimeError("Active window changed or target became stale")
    finally:
        manager.close()
finally:
    samples.append(available_mb())
    print("MIN_AVAILABLE_RAM_MB:", min(v for v in samples if v is not None) if any(v is not None for v in samples) else None)
    print("RAM_AFTER_CLEANUP_MB:", samples[-1])
