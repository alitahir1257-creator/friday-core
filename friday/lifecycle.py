"""On-demand, ownership-aware lifecycle for the local llama.cpp server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any

from friday.brain import (
    LAUNCHER_PATH,
    LOCAL_BRAIN_HOST,
    LOCAL_BRAIN_PORT,
    LOG_PATH,
    SERVER_START_TIMEOUT_SECONDS,
    _get_models,
)


class LocalBrainState(str, Enum):
    OFF = "OFF"
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class LocalBrainLifecycleManager:
    """Only stop the llama.cpp process launched and retained by this manager."""

    def __init__(self, idle_seconds: int | None = None, startup_timeout: int = SERVER_START_TIMEOUT_SECONDS) -> None:
        try:
            configured_idle = int(os.environ.get("FRIDAY_LOCAL_BRAIN_IDLE_SECONDS", "300"))
        except ValueError:
            configured_idle = 300
        self.idle_seconds = max(1, idle_seconds if idle_seconds is not None else configured_idle)
        self.startup_timeout = max(1, startup_timeout)
        self.state = LocalBrainState.OFF
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._launcher: subprocess.Popen[bytes] | None = None
        self._server_pid: int | None = None
        self._server_executable: str | None = None
        self._model_path: str | None = None
        self._last_used = 0.0
        self._active_requests = 0
        self._idle_thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def owns_server(self) -> bool:
        with self._lock:
            return self._launcher is not None and self._server_pid is not None

    def health(self) -> bool:
        try:
            _get_models()
            return True
        except Exception:
            return False

    def ensure_ready(self) -> None:
        with self._condition:
            if self.health():
                self.state = LocalBrainState.READY
                self._last_used = time.monotonic()
                self._condition.notify_all()
                return

            self.state = LocalBrainState.STARTING
            self.last_error = None
            try:
                self._launch()
                deadline = time.monotonic() + self.startup_timeout
                while time.monotonic() < deadline:
                    if self._launcher is None or self._launcher.poll() is not None:
                        raise RuntimeError("The FRIDAY local brain process exited before the server became ready.")
                    if self.health():
                        server_pid = self._find_owned_server(self._launcher.pid)
                        if server_pid != self._launcher.pid:
                            raise RuntimeError("The local server became ready, but FRIDAY could not verify ownership.")
                        self._server_pid = server_pid
                        self._last_used = time.monotonic()
                        self.state = LocalBrainState.READY
                        self._start_idle_monitor()
                        return
                    time.sleep(1)
                raise TimeoutError(f"llama.cpp did not become ready within {self.startup_timeout} seconds.")
            except Exception as error:
                self.last_error = type(error).__name__
                self.state = LocalBrainState.ERROR
                try:
                    self._stop_owned_locked()
                except Exception:
                    pass
                raise RuntimeError(f"Local Qwen startup failed: {type(error).__name__}.") from error

    def begin_request(self) -> None:
        with self._condition:
            self.ensure_ready()
            self._active_requests += 1
            self._last_used = time.monotonic()
            self._condition.notify_all()

    def finish_request(self) -> None:
        with self._condition:
            self._active_requests = max(0, self._active_requests - 1)
            if self._active_requests == 0:
                self._last_used = time.monotonic()
            self._condition.notify_all()

    def _launch(self) -> None:
        if os.name != "nt":
            raise RuntimeError("The configured local Qwen launcher requires Windows.")
        if not LAUNCHER_PATH.is_file():
            raise RuntimeError("The configured local Qwen launcher was not found.")
        launcher_text = LAUNCHER_PATH.read_text(encoding="utf-8-sig")
        self._server_executable = self._launcher_value(launcher_text, "server")
        self._model_path = self._launcher_value(launcher_text, "model")
        if not Path(self._server_executable).is_file() or not Path(self._model_path).is_file():
            raise RuntimeError("The configured local Qwen executable or model was not found.")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with LOG_PATH.open("ab") as server_log:
            self._launcher = subprocess.Popen(
                [
                    self._server_executable,
                    "--model", self._model_path,
                    "--alias", "Qwen3.5-4B",
                    "--host", LOCAL_BRAIN_HOST,
                    "--port", str(LOCAL_BRAIN_PORT),
                    "--ctx-size", "4096",
                    "--threads", "4",
                    "--parallel", "1",
                    "--n-gpu-layers", "0",
                    "--jinja",
                    "--reasoning", "off",
                    "--cors-origins", "localhost",
                    "--no-webui",
                ],
                cwd=LAUNCHER_PATH.parent,
                stdin=subprocess.DEVNULL,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                close_fds=True,
            )

    def _find_owned_server(self, launcher_pid: int) -> int | None:
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(launcher_pid)}\"; "
            "if ($p) { $p | Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress }"
        )
        completed = subprocess.run(
            [
                str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                "-NoProfile",
                "-Command",
                script,
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
        row: Any = json.loads(completed.stdout or "null")
        if not isinstance(row, dict):
            return None
        command_line = row.get("CommandLine", "")
        expected_port = re.compile(rf"--port\s+{LOCAL_BRAIN_PORT}(?:\s|$)", re.IGNORECASE)
        expected_host = re.compile(r"--host\s+127\.0\.0\.1(?:\s|$)", re.IGNORECASE)
        if (
            int(row.get("ProcessId", -1)) == launcher_pid
            and row.get("Name", "").casefold() == "llama-server.exe"
            and self._server_executable
            and os.path.normcase(row.get("ExecutablePath", "")) == os.path.normcase(self._server_executable)
            and self._model_path
            and self._model_path.casefold() in command_line.casefold()
            and expected_host.search(command_line)
            and expected_port.search(command_line)
        ):
            return launcher_pid
        return None

    @staticmethod
    def _launcher_value(script: str, variable: str) -> str:
        match = re.search(rf"^\s*\${re.escape(variable)}\s*=\s*'([^']+)'", script, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"The local launcher has no {variable} path.")
        return match.group(1)

    def _start_idle_monitor(self) -> None:
        if self._idle_thread is None or not self._idle_thread.is_alive():
            self._idle_thread = threading.Thread(target=self._idle_loop, name="friday-local-brain-idle", daemon=True)
            self._idle_thread.start()
        self._condition.notify_all()

    def _idle_loop(self) -> None:
        with self._condition:
            while self.state == LocalBrainState.READY and self._launcher is not None:
                if self._active_requests:
                    self._condition.wait()
                    continue
                remaining = self.idle_seconds - (time.monotonic() - self._last_used)
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                self.state = LocalBrainState.STOPPING
                break
            else:
                return
        try:
            with self._condition:
                self._stop_owned_locked()
                self.state = LocalBrainState.OFF
        except Exception:
            with self._condition:
                self.state = LocalBrainState.ERROR

    def _stop_owned_locked(self) -> None:
        launcher, server_pid = self._launcher, self._server_pid
        if launcher is None:
            return
        if server_pid is not None and server_pid != launcher.pid:
            raise RuntimeError("The retained local brain process identity changed; it was not stopped.")
        if launcher.poll() is None:
            # Popen retains a handle to the exact process started by FRIDAY.
            launcher.terminate()
        try:
            launcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            launcher.kill()
            launcher.wait(timeout=5)
        self._launcher = None
        self._server_pid = None

    def close(self) -> None:
        """Close only a local process owned by this manager."""
        with self._condition:
            if self._launcher is None:
                return
            self.state = LocalBrainState.STOPPING
            self._stop_owned_locked()
            self.state = LocalBrainState.OFF
            self._condition.notify_all()
