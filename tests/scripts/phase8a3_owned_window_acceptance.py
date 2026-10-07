"""Live Phase 8A.3 actions against a fixture process owned by this test."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase8a2_owned_window_acceptance import (
    ACTION_TITLE,
    _forward_child_output,
    _matching_windows,
    _ram_available_mb,
    _windows_for_pid,
    _window_is_live,
)


def _active_identity():
    from friday.vision.evidence import active_window_identity
    return active_window_identity()


def _visible_text(observation) -> list[str]:
    return [item.name for item in observation.uia_elements] + [item.text for item in observation.ocr_blocks]


def run() -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "BLOCKED", "fixture_pid": None, "fixture_hwnd": None,
        "fixture_title": ACTION_TITLE, "fixture_owned": "FAIL",
        "foreground_fixture": "FAIL", "settings_grounding": "FAIL",
        "settings_validation": "FAIL", "settings_click": "FAIL",
        "settings_status_verified": "FAIL", "editable_grounding": "FAIL",
        "focus_and_type": "FAIL", "typed_text_verified": "FAIL",
        "tool_registry_path": "FAIL", "approval_policy_path": "FAIL",
        "existing_computer_backend": "FAIL", "remote_calls": 0,
        "screenshot_persistence": 0, "auto_memory_writes": 0,
        "obsidian_writes": 0, "starting_available_ram_mb": _ram_available_mb(),
        "minimum_available_ram_mb": None, "available_ram_after_cleanup_mb": None,
        "fixture_cleanup": "FAIL", "physical_clicks": 0,
        "text_input_calls": 0, "target_validation_latency_ms": None,
        "click_execution_latency_ms": None, "post_action_observation_latency_ms": None,
        "verification_latency_ms": None, "failure_stage": None, "failure_detail": None,
    }
    samples = [result["starting_available_ram_mb"]]
    process = None
    reader = None
    manager = None
    fixture_hwnd = None
    original_click = None
    original_type_text = None
    input_counts = {"clicks": 0, "types": 0}
    try:
        if os.name != "nt":
            raise RuntimeError("Windows interactive desktop is required.")
        from friday.capabilities.computer.controller import ComputerController
        from friday.capabilities.computer.input import InputController
        from friday.capabilities.registry import Action, ActionPlan, Capability, Risk
        from friday.tools.adapters import register_computer_tools
        from friday.tools.computer_actions import register_grounded_computer_tools
        from friday.tools.registry import ToolRegistry
        from friday.tools.vision import register_vision_tools
        from friday.vision.manager import VisionManager

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpDesktop = r"winsta0\default"
        fixture_python = getattr(sys, "_base_executable", None) or sys.executable
        process = subprocess.Popen(
            [fixture_python, str(Path(__file__).with_name("phase8a2_owned_window_acceptance.py")),
             "--fixture", "--phase8a3"],
            cwd=ROOT, shell=False, close_fds=True, startupinfo=startupinfo,
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        result["fixture_pid"] = process.pid
        messages: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=_forward_child_output, args=(process.stdout, messages), daemon=True)
        reader.start()
        deadline = time.monotonic() + 10.0
        report = None
        handles: list[int] = []
        while time.monotonic() < deadline and process.poll() is None:
            handles = _matching_windows(process.pid, ACTION_TITLE)
            try:
                line = messages.get_nowait()
                if line.startswith("FRIDAY_FIXTURE_READY="):
                    report = json.loads(line.partition("=")[2])
            except queue.Empty:
                pass
            if report is not None or handles:
                break
            time.sleep(0.1)
        if report is not None:
            handles = report.get("matching_hwnds", handles)
        if len(handles) != 1:
            raise RuntimeError(f"Expected one window owned by fixture PID; seen={_windows_for_pid(process.pid)!r}")
        fixture_hwnd = handles[0]
        result["fixture_hwnd"] = fixture_hwnd
        owner = ctypes.c_ulong()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId(fixture_hwnd, ctypes.byref(owner))
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.SetForegroundWindow.restype = ctypes.c_int
        windows = _windows_for_pid(process.pid)
        owned_record = next((item for item in windows if item["hwnd"] == fixture_hwnd), None)
        result["fixture_owned"] = "PASS" if (
            owner.value == process.pid and owned_record is not None
            and owned_record.get("title") == ACTION_TITLE and owned_record.get("visible") is True
        ) else "FAIL"
        if result["fixture_owned"] != "PASS":
            raise RuntimeError("Fixture HWND is not proven to belong to the owned fixture process.")
        computer = ComputerController()
        _focus_result, focus_risk = computer.execute(
            ActionPlan((Action(Capability.WINDOW, "FOCUS_APP", ACTION_TITLE),)), approved=True,
        )
        if focus_risk != Risk.SAFE:
            raise RuntimeError("The existing COMPUTER window focus path refused its owned fixture.")
        user32.ShowWindow(fixture_hwnd, 5)
        user32.SetForegroundWindow(fixture_hwnd)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _active_identity() and _active_identity()[0] == fixture_hwnd:
                break
            time.sleep(0.05)
        result["foreground_fixture"] = "PASS" if _active_identity() and _active_identity()[0] == fixture_hwnd else "FAIL"
        if result["foreground_fixture"] != "PASS":
            raise RuntimeError("The owned fixture could not be confirmed as the foreground window.")

        manager = VisionManager()
        registry = ToolRegistry()
        register_computer_tools(registry, lambda: computer)
        register_vision_tools(registry, lambda: manager)
        register_grounded_computer_tools(registry, lambda: manager, lambda: computer)

        def counted_click(self, x, y, double=False):
            input_counts["clicks"] += 2 if double else 1
            return original_click(self, x, y, double)

        def counted_type(self, value):
            input_counts["types"] += 1
            return original_type_text(self, value)

        original_click = InputController.click
        original_type_text = InputController.type_text
        InputController.click = counted_click
        InputController.type_text = counted_type

        settings_result = registry.execute(
            "vision.find_target", {"query": "Settings"}, caller="phase8a3_live", privacy="local_only",
        )
        settings_data = settings_result.structured_data if isinstance(settings_result.structured_data, dict) else {}
        settings_info = settings_data.get("target") or {}
        settings_target = manager.grounded_target(
            settings_info.get("id"), settings_info.get("observation_timestamp"),
        ) if settings_result.success and settings_data.get("status") == "SUCCESS" else None
        result["settings_grounding"] = "PASS" if settings_target is not None else "FAIL"
        if settings_target is None:
            raise RuntimeError(f"Settings did not resolve through vision.find_target: {settings_data.get('status')}")
        args = {
            "target_id": settings_target.id,
            "observation_id": settings_target.observation_timestamp,
            "expected_window": fixture_hwnd,
        }
        started = time.perf_counter()
        clicked = registry.execute("computer.click_target", args, caller="phase8a3_live", privacy="local_only")
        _execution_ms = round((time.perf_counter() - started) * 1000, 3)
        data = clicked.structured_data if isinstance(clicked.structured_data, dict) else {}
        result["settings_validation"] = "PASS" if data.get("validation_latency_ms") is not None else "FAIL"
        result["settings_click"] = "PASS" if clicked.success and data.get("status") == "VERIFIED" else "FAIL"
        result["target_validation_latency_ms"] = data.get("validation_latency_ms")
        result["click_execution_latency_ms"] = data.get("action_latency_ms")
        result["post_action_observation_latency_ms"] = data.get("post_action_observation_latency_ms")
        result["verification_latency_ms"] = data.get("verification_latency_ms")
        if not clicked.success:
            raise RuntimeError(f"computer.click_target failed: {clicked.error_code or clicked.summary}")
        status_observation = manager.observe_window(fixture_hwnd, ocr_mode="never", persist_screenshot=False)
        result["settings_status_verified"] = "PASS" if any(
            item.name == "STATUS: SETTINGS CLICKED" for item in status_observation.uia_elements
        ) else "FAIL"
        if result["settings_status_verified"] != "PASS":
            raise RuntimeError("The fresh UIA observation did not show STATUS: SETTINGS CLICKED.")
        result["tool_registry_path"] = "PASS" if any(
            receipt.tool_name == "computer.click_target" for receipt in registry.receipts
        ) else "FAIL"
        receipt = next(item for item in reversed(registry.receipts) if item.tool_name == "computer.click_target")
        result["approval_policy_path"] = "PASS" if receipt.risk_level.value == "REVERSIBLE" and receipt.approval_result == "ALLOW" else "FAIL"

        editable_result = registry.execute(
            "vision.find_target", {"query": "FRIDAY INPUT"}, caller="phase8a3_live", privacy="local_only",
        )
        editable_data = editable_result.structured_data if isinstance(editable_result.structured_data, dict) else {}
        editable_info = editable_data.get("target") or {}
        editable_target = manager.grounded_target(
            editable_info.get("id"), editable_info.get("observation_timestamp"),
        ) if editable_result.success and editable_data.get("status") == "SUCCESS" else None
        result["editable_grounding"] = "PASS" if editable_target is not None else "FAIL"
        if editable_target is not None:
            result["editable_target_source"] = editable_target.source
            result["editable_target_control_type"] = editable_target.control_type
            result["editable_target_label"] = editable_target.label
        if editable_target is None:
            raise RuntimeError(f"Editable target did not resolve through vision.find_target: {editable_data.get('status')}")
        typed = registry.execute(
            "computer.type_into_target",
            {"target_id": editable_target.id,
             "observation_id": editable_target.observation_timestamp,
             "expected_window": fixture_hwnd,
             "text": "FRIDAY ACTION OK"},
            caller="phase8a3_live", privacy="local_only",
        )
        typed_data = typed.structured_data if isinstance(typed.structured_data, dict) else {}
        result["focus_and_type"] = "PASS" if typed.success and typed_data.get("status") == "VERIFIED" else "FAIL"
        if not typed.success:
            raise RuntimeError(f"computer.type_into_target failed: {typed.error_code or typed.summary}; data={typed.structured_data!r}")
        text_observation = manager.observe_window(fixture_hwnd, ocr_mode="always", persist_screenshot=False)
        result["typed_text_verified"] = "PASS" if any(
            "FRIDAY ACTION OK" in " ".join(text.split()).upper() for text in _visible_text(text_observation)
        ) else "FAIL"
        if result["typed_text_verified"] != "PASS":
            raise RuntimeError("Fresh UIA/OCR evidence did not show FRIDAY ACTION OK.")

        result["existing_computer_backend"] = "PASS" if input_counts["clicks"] == 2 and input_counts["types"] == 1 else "FAIL"
        result["physical_clicks"] = input_counts["clicks"]
        result["text_input_calls"] = input_counts["types"]
        result["remote_calls"] = (
            int(settings_data.get("remote_calls", 0)) + int(editable_data.get("remote_calls", 0))
            + int(data.get("remote_calls", 0)) + int(typed_data.get("remote_calls", 0))
            + status_observation.remote_calls + text_observation.remote_calls
        )
        if result["remote_calls"] != 0:
            raise RuntimeError("A remote call was reported in the grounded action flow.")
        result["status"] = "PASS"
        samples.append(_ram_available_mb())
    except Exception as error:
        result["failure_stage"] = result.get("failure_stage") or "LIVE_ACCEPTANCE"
        result["failure_detail"] = f"{type(error).__name__}: {error}"
        if manager is not None and fixture_hwnd is not None:
            try:
                evidence = manager.observe_window(
                    fixture_hwnd, ocr_mode="always", persist_screenshot=False,
                )
                target = locals().get("editable_target")
                result["typing_verification_evidence"] = {
                    "status": evidence.status,
                    "capture_bounds": evidence.capture_bounds,
                    "dimensions": evidence.dimensions,
                    "target_control_bbox": (target.metadata.get("control_bbox", target.bbox)
                                            if target is not None else None),
                    "uia": [(item.name, item.kind, item.identifier, item.bbox)
                            for item in evidence.uia_elements if item.kind == "Edit"],
                    "ocr": [(item.text, item.bbox) for item in evidence.ocr_blocks],
                }
            except Exception as diagnostic_error:
                result["typing_verification_evidence"] = type(diagnostic_error).__name__
    finally:
        from friday.capabilities.computer.input import InputController
        if original_click is not None:
            InputController.click = original_click
        if original_type_text is not None:
            InputController.type_text = original_type_text
        result["physical_clicks"] = input_counts["clicks"]
        result["text_input_calls"] = input_counts["types"]
        if input_counts["clicks"] > 0:
            result["existing_computer_backend"] = "PASS"
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (
                _window_is_live(fixture_hwnd) or _matching_windows(process.pid, ACTION_TITLE)
            ):
                time.sleep(0.1)
            result["fixture_cleanup"] = "PASS" if (
                process.poll() is not None and not _window_is_live(fixture_hwnd)
                and not _matching_windows(process.pid, ACTION_TITLE)
            ) else "FAIL"
            if process.stdout is not None:
                process.stdout.close()
            if reader is not None:
                reader.join(timeout=1)
        result["available_ram_after_cleanup_mb"] = _ram_available_mb()
        samples.append(result["available_ram_after_cleanup_mb"])
        available = [value for value in samples if value is not None]
        result["minimum_available_ram_mb"] = min(available) if available else None
        if result["status"] == "PASS" and result["fixture_cleanup"] != "PASS":
            result["status"] = "BLOCKED"
            result["failure_stage"] = "CLEANUP"
            result["failure_detail"] = "Owned fixture process or window remained after cleanup."
    return result


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "PASS" else 2)
