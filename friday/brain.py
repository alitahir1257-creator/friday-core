"""Local OpenAI-compatible Qwen brain backed by llama.cpp."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from friday.profile import AssistantProfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / "friday-core.log"
LAUNCHER_PATH = Path(r"D:\FRIDAY\scripts\friday-local-brain.ps1")
API_BASE = "http://127.0.0.1:8080/v1"
MODEL_NAME = "Qwen3.5-4B"
MAX_OUTPUT_TOKENS = 1024
SERVER_START_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 240

if urlsplit(API_BASE).hostname != "127.0.0.1":
    raise RuntimeError("FRIDAY Core only permits its configured loopback model endpoint.")

LOCAL_HTTP = build_opener(ProxyHandler({}))


def ensure_model_server() -> None:
    try:
        _get_models()
        return
    except (OSError, URLError, HTTPError, TimeoutError, json.JSONDecodeError):
        pass

    if os.name != "nt":
        raise RuntimeError("The local model is offline; the supplied launcher requires Windows.")
    if not LAUNCHER_PATH.is_file():
        raise RuntimeError(f"Local model launcher was not found: {LAUNCHER_PATH}")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with LOG_PATH.open("a", encoding="utf-8") as server_log:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(LAUNCHER_PATH),
                "-ContextSize",
                "4096",
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )

    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            _get_models()
            return
        except (OSError, URLError, HTTPError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"llama.cpp did not become ready within five minutes{detail}")


def _get_models(timeout: float = 2.0) -> dict[str, Any]:
    request = Request(f"{API_BASE}/models", method="GET")
    with LOCAL_HTTP.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class LocalQwenBrain:
    def __init__(self, profile: AssistantProfile) -> None:
        self._system_prompt = profile.system_prompt()

    def respond(
        self, command: str, history: list[dict[str, str]]
    ) -> tuple[str, int | None, int | None]:
        ensure_model_server()
        messages = [
            {"role": "system", "content": self._system_prompt},
            *history,
            {"role": "user", "content": command},
        ]
        request_body = {
            "model": MODEL_NAME,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.3,
            "stream": False,
        }
        request = Request(
            f"{API_BASE}/chat/completions",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with LOCAL_HTTP.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))

        try:
            answer = payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise RuntimeError("The local model returned no assistant response.") from error

        usage = payload.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion_tokens = (
            usage.get("completion_tokens") if isinstance(usage, dict) else None
        )
        return answer, prompt_tokens, completion_tokens
