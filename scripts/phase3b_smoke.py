"""Targeted Phase 3B checks; run from the interactive Windows session."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.brain_manager import BrainManager, BrainReply, CloudflareQwenProvider, LocalQwenProvider
from friday.capabilities.computer.windows import WindowController
from friday.core import FridayCore
from friday.lifecycle import LocalBrainLifecycleManager, LocalBrainState
from friday.profile import load_profile


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def self_ram_mib() -> float:
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return round(counters.WorkingSetSize / 1048576, 1)


def emit(name: str, **values: object) -> None:
    print(json.dumps({"test": name, **values}, ensure_ascii=False), flush=True)


class CountingBrainManager:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *_args, **_kwargs) -> BrainReply:
        self.calls += 1
        return BrainReply("unexpected model call", provider="unexpected")

    def close(self) -> None:
        pass


class FailingCloudProvider:
    name = "cloudflare-test"
    is_local = False

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def health(self) -> str:
        return "CONFIGURED"

    def chat(self, _messages) -> BrainReply:
        self.calls += 1
        raise URLError("simulated unavailable cloud endpoint")


class FakeLauncher:
    pid = 987654

    def poll(self):
        return None


def lifecycle_check() -> None:
    # The real server was present before FRIDAY started; this path must stay unowned.
    existing = LocalBrainLifecycleManager()
    existing.ensure_ready()
    preexisting = existing.state == LocalBrainState.READY and not existing.owns_server
    existing.close()
    still_healthy = existing.health()

    # Exercise owned start, READY, idle STOPPING, and OFF without touching a real PID.
    simulated = LocalBrainLifecycleManager(idle_seconds=1, startup_timeout=2)
    checks = iter((False, True))
    simulated.health = lambda: next(checks, True)
    simulated._launch = lambda: setattr(simulated, "_launcher", FakeLauncher())
    simulated._find_owned_server = lambda _pid: 987655
    stopped = threading.Event()

    def fake_stop() -> None:
        simulated._launcher = None
        simulated._server_pid = None
        stopped.set()

    simulated._stop_owned_locked = fake_stop
    simulated.ensure_ready()
    became_ready = simulated.state == LocalBrainState.READY and simulated.owns_server
    with simulated._condition:
        simulated._active_requests = 1
    time.sleep(1.2)
    busy_preserved = not stopped.is_set() and simulated.state == LocalBrainState.READY
    simulated.finish_request()
    idled = stopped.wait(4) and simulated.state == LocalBrainState.OFF
    emit("local_lifecycle", preexisting_ready_unowned=preexisting,
         preexisting_server_still_healthy=still_healthy,
         simulated_on_demand_start=became_ready, active_request_preserved=busy_preserved,
         simulated_idle_stop=idled,
         idle_seconds_default=os.environ.get("FRIDAY_LOCAL_BRAIN_IDLE_SECONDS", "300"))


def main() -> None:
    profile = load_profile()
    cloud = CloudflareQwenProvider()
    configured = cloud.available()
    emit("cloud_config", available=configured, profile_enabled=profile.cloud_brain_enabled)

    # M: measure this FRIDAY process before it asks either provider to load/use a model.
    emit("friday_process_before_inference", working_set_mib=self_ram_mib())

    local = LocalQwenProvider(profile)
    manager = BrainManager(profile, local_provider=local)
    reply = manager.respond("Who are you?", [])
    emit("no_credentials_local", provider=reply.provider, answer=reply.text,
         prompt_tokens=reply.prompt_tokens)

    fallback_profile = profile.__class__(**{**profile.__dict__, "cloud_brain_enabled": True})
    failing_cloud = FailingCloudProvider()
    fallback_manager = BrainManager(fallback_profile, cloud_provider=failing_cloud)
    fallback = fallback_manager.respond("Say exactly: local fallback works.", [])
    emit("simulated_cloud_failure", provider=fallback.provider, cloud_attempts=failing_cloud.calls,
         answer=fallback.text)

    spy = CountingBrainManager()
    core = FridayCore(profile, brain_manager=spy)
    answer, _, decision = core.respond("open notepad")
    visible = any("notepad" in title.casefold() for title in WindowController().visible_windows())
    emit("computer_no_llm", route=decision.route.value, answer=answer,
         notepad_visible=visible, model_calls=spy.calls)

    lifecycle_check()
    emit("local_qwen_process", working_set_mib=local_process_ram_mib())
    emit("friday_process_after_inference", working_set_mib=self_ram_mib())

    if configured and profile.cloud_brain_enabled:
        one = BrainManager(profile).respond("Reply exactly with: FRIDAY CLOUD OK", [])
        emit("real_cloud", provider=one.provider, answer=one.text)
    else:
        emit("real_cloud", result="CLOUD_CONFIG_REQUIRED")
    manager.close()
    fallback_manager.close()
    core.close()


def local_process_ram_mib() -> float | None:
    import subprocess

    command = "(Get-Process -Name llama-server -ErrorAction Stop | Measure-Object -Property WorkingSet64 -Sum).Sum"
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=15, check=True)
        return round(int(result.stdout.strip().splitlines()[-1]) / 1048576, 1)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


if __name__ == "__main__":
    main()
