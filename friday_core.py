from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import tkinter as tk
from tkinter import ttk


ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.json"
LOG_PATH = ROOT / "friday-core.log"
LAUNCHER_PATH = Path(r"D:\FRIDAY\scripts\friday-local-brain.ps1")
API_BASE = "http://127.0.0.1:8080/v1"
MODEL_NAME = "Qwen3.5-4B"
MAX_OUTPUT_TOKENS = 1024
MAX_HISTORY_TURNS = 6
SERVER_START_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 240

if urlsplit(API_BASE).hostname != "127.0.0.1":
    raise RuntimeError("FRIDAY Core only permits its configured loopback model endpoint.")

LOCAL_HTTP = build_opener(ProxyHandler({}))


class Route(str, Enum):
    GENERAL = "GENERAL"
    CODING = "CODING"
    RESEARCH = "RESEARCH"
    COMPUTER = "COMPUTER"
    MEMORY = "MEMORY"


def classify_intent(command: str) -> Route:
    text = command.casefold()
    route_terms = {
        Route.CODING: ("code", "coding", "python", "script", "debug", "bug", "function"),
        Route.RESEARCH: ("research", "sources", "investigate", "compare", "find out"),
        Route.COMPUTER: ("computer", "browser", "click", "open app", "desktop"),
        Route.MEMORY: ("remember", "yaad rakh", "recall", "what did i say"),
    }
    for route, terms in route_terms.items():
        if any(term in text for term in terms):
            return route
    return Route.GENERAL


def load_profile() -> dict[str, str]:
    defaults = {
        "assistant_name": "Friday",
        "user_name": "Ali",
        "language": "auto",
        "default_response_style": "concise",
    }
    try:
        supplied = json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        supplied = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read profile.json: {error}") from error
    if not isinstance(supplied, dict):
        raise RuntimeError("profile.json must contain a JSON object.")
    return {
        key: str(supplied.get(key, default)).strip() or default
        for key, default in defaults.items()
    }


def system_message(profile: dict[str, str]) -> str:
    language = profile["language"]
    language_rule = (
        "Match the user's language and use natural Roman Urdu when they do."
        if language.casefold() == "auto"
        else f"Respond in {language}."
    )
    return (
        f"You are {profile['assistant_name']}, a local assistant for "
        f"{profile['user_name']}. {language_rule} "
        f"Keep replies {profile['default_response_style']}. "
        "When asked for exact text, output only that text."
    )


def _get_models(timeout: float = 2.0) -> dict[str, Any]:
    request = Request(f"{API_BASE}/models", method="GET")
    with LOCAL_HTTP.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
            cwd=ROOT,
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


class FridayCore:
    def __init__(self, profile: dict[str, str]) -> None:
        self.profile = profile
        self.history: list[dict[str, str]] = []

    def respond(self, command: str) -> tuple[str, int | None, Route]:
        route = classify_intent(command)
        recent = self.history[-(MAX_HISTORY_TURNS * 2) :]
        messages = [
            {"role": "system", "content": system_message(self.profile)},
            *recent,
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

        self.history = [
            *recent,
            {"role": "user", "content": command},
            {"role": "assistant", "content": answer},
        ][-(MAX_HISTORY_TURNS * 2) :]
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"route={route.value} prompt_tokens={prompt_tokens} "
                f"output_tokens={usage.get('completion_tokens', 'unknown') if isinstance(usage, dict) else 'unknown'}\n"
            )
        return answer, prompt_tokens, route


class FridayWindow:
    def __init__(self, root: tk.Tk, core: FridayCore) -> None:
        self.root = root
        self.core = core
        self.root.title("Friday Core")
        self.root.geometry("480x330")
        self.root.minsize(380, 270)
        self.root.attributes("-topmost", True)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="FRIDAY", font=("Segoe UI", 16, "bold")).pack(anchor="w")

        self.response = tk.Text(frame, height=9, wrap="word", state="disabled")
        self.response.pack(fill="both", expand=True, pady=(10, 8))

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.command = tk.StringVar()
        self.input = ttk.Entry(controls, textvariable=self.command)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.send_button = ttk.Button(controls, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=(8, 0))

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, text="status:").pack(side="left")
        self.status = tk.StringVar(value="READY")
        ttk.Label(footer, textvariable=self.status).pack(side="left", padx=(4, 0))
        self.input.focus_set()

    def _on_enter(self, _event: tk.Event[Any]) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        command = self.command.get().strip()
        if not command or self.status.get() == "THINKING":
            return
        self.command.set("")
        self.status.set("THINKING")
        self.send_button.configure(state="disabled")
        threading.Thread(target=self._run, args=(command,), daemon=True).start()

    def _run(self, command: str) -> None:
        try:
            ensure_model_server()
            answer, _, _ = self.core.respond(command)
        except Exception as error:
            self.root.after(0, self._show_error, str(error))
            return
        self.root.after(0, self._show_response, answer)

    def _show_response(self, answer: str) -> None:
        self.response.configure(state="normal")
        self.response.delete("1.0", "end")
        self.response.insert("1.0", answer)
        self.response.configure(state="disabled")
        self.status.set("READY")
        self.send_button.configure(state="normal")
        self.input.focus_set()

    def _show_error(self, message: str) -> None:
        self.response.configure(state="normal")
        self.response.delete("1.0", "end")
        self.response.insert("1.0", f"Error: {message}")
        self.response.configure(state="disabled")
        self.status.set("ERROR")
        self.send_button.configure(state="normal")
        self.input.focus_set()


def main() -> None:
    profile = load_profile()
    root = tk.Tk()
    FridayWindow(root, FridayCore(profile))
    root.mainloop()


if __name__ == "__main__":
    main()
