"""Isolated, one-checkpoint Laya router probe; never used by FRIDAY startup."""

from __future__ import annotations

import ctypes
import gc
import json
import os
import time
from ctypes import wintypes
from typing import Any


MIN_FREE_MIB = 500
CHECKPOINT_WEIGHT_MIB = 843
LOAD_OVERHEAD_MIB = 1024
ROUTE_QUESTION = {
    "route": {
        "type": "choice",
        "instructions": "Choose the single best category for the user's request.",
        "criteria": {
            "GENERAL": "greetings, general knowledge, and explanations",
            "CODING": "writing, debugging, or changing computer code",
            "RESEARCH": "researching current information, evidence, or sources",
            "COMPUTER": "actions in desktop apps, browsers, or the computer",
            "MEMORY": "remembering or recalling user-provided details",
        },
    }
}
PROMPTS = (
    "hello Friday",
    "fix this Python code",
    "research latest information about X",
    "open Chrome",
    "remember that my project name is X",
)


class MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def available_system_mib() -> float:
    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx(
        ctypes.byref(status)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.ullAvailPhys / (1024 * 1024)


def process_working_set_mib() -> float:
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize / (1024 * 1024)


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def check_memory(phase: str) -> bool:
    available = available_system_mib()
    required = MIN_FREE_MIB + CHECKPOINT_WEIGHT_MIB + LOAD_OVERHEAD_MIB
    emit(
        "memory_guard",
        phase=phase,
        available_system_mib=round(available, 1),
        required_system_mib=required,
        checkpoint_weight_mib=CHECKPOINT_WEIGHT_MIB,
        minimum_free_mib=MIN_FREE_MIB,
    )
    return available >= required


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("This resource probe currently requires Windows.")
    if not check_memory("before_laya_import"):
        emit("skipped", reason="insufficient RAM headroom; checkpoint was not loaded")
        return 2

    from laya import Router

    if not check_memory("before_checkpoint_load"):
        emit("skipped", reason="insufficient RAM headroom; checkpoint was not loaded")
        return 2

    router = Router(max_loaded=1)
    try:
        for index, prompt in enumerate(PROMPTS):
            if index and available_system_mib() < MIN_FREE_MIB:
                emit("stopped", reason="free system RAM fell below 500 MiB")
                return 3

            started = time.perf_counter()
            result = router.predict(prompt, ROUTE_QUESTION, model="english")
            latency_ms = (time.perf_counter() - started) * 1000
            answer = result["answers"]["route"]
            emit(
                "prediction",
                prompt=prompt,
                route=answer.get("choice"),
                confidence=answer.get(
                    "answer_confidence", answer.get("confidence")
                ),
                latency_ms=round(latency_ms, 2),
                cold_load_included=index == 0,
            )
            if index == 0:
                emit(
                    "resident_with_qwen",
                    laya_process_working_set_mib=round(process_working_set_mib(), 1),
                    available_system_mib=round(available_system_mib(), 1),
                )
    finally:
        router.unload()
        gc.collect()
        emit(
            "after_unload",
            laya_process_working_set_mib=round(process_working_set_mib(), 1),
            available_system_mib=round(available_system_mib(), 1),
            unloaded=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
