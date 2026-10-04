"""Lightweight Tkinter window for FRIDAY Core."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from friday.core import FridayCore
from friday.profile import load_profile


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
