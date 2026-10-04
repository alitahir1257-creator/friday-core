"""Lightweight Tkinter window for FRIDAY Core."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from friday.core import FridayCore
from friday.profile import load_profile


class FridayWindow:
    def __init__(self, root: tk.Tk, core: FridayCore) -> None:
        self.root = root
        self.core = core
        self._closing = False
        self.brain_status = "READY"
        self._computer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-computer")
        self.root.title("Friday Core")
        self.root.geometry("480x330")
        self.root.minsize(380, 270)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self.remote_laya_status = tk.StringVar()
        ttk.Label(footer, textvariable=self.remote_laya_status).pack(side="right")
        self._refresh_laya_status()
        self.input.focus_set()

    def _on_enter(self, _event: tk.Event[Any]) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        command = self.command.get().strip()
        if not command or self.status.get() in {"THINKING", "EXECUTING", "WAITING_APPROVAL"}:
            return
        self.command.set("")
        self.status.set("THINKING")
        self.send_button.configure(state="disabled")
        self._computer_executor.submit(self._run, command)

    def _run(self, command: str) -> None:
        try:
            answer, _, _ = self.core.respond(command, self._queue_status)
        except Exception as error:
            if not self._closing:
                self.root.after(0, self._show_error, str(error))
            return
        if not self._closing:
            self.root.after(0, self._refresh_laya_status)
            self.root.after(0, self._show_response, answer)

    def _refresh_laya_status(self) -> None:
        label = self.core.laya_status.replace("_", " ").title()
        self.remote_laya_status.set(f"Laya remote: {label}")

    def _queue_status(self, status: str) -> None:
        self.brain_status = status
        compact_status = "THINKING" if status in {"THINKING_CLOUD", "THINKING_LOCAL", "STARTING_LOCAL_BRAIN"} else status
        if not self._closing:
            self.root.after(0, self.status.set, compact_status)

    def _show_response(self, answer: str) -> None:
        self.response.configure(state="normal")
        self.response.delete("1.0", "end")
        self.response.insert("1.0", answer)
        self.response.configure(state="disabled")
        if self.core.pending_approval is not None:
            self.status.set("WAITING_APPROVAL")
            self.root.after(0, self._ask_approval)
            return
        self.status.set("READY")
        self.send_button.configure(state="normal")
        self.input.focus_set()

    def _ask_approval(self) -> None:
        approved = messagebox.askyesno("FRIDAY approval", "Approve this computer action?\n\n" + (self.core.pending_approval or ""))
        self.status.set("EXECUTING" if approved else "READY")
        self._computer_executor.submit(self._run_approved, approved)

    def _run_approved(self, approved: bool) -> None:
        try:
            answer = self.core.approve_pending(approved)
        except Exception as error:
            if not self._closing:
                self.root.after(0, self._show_error, str(error))
            return
        if not self._closing:
            self.root.after(0, self._show_response, answer)

    def _on_close(self) -> None:
        self._closing = True
        self._computer_executor.submit(self._close_resources)
        self._computer_executor.shutdown(wait=False)
        self.root.destroy()

    def _close_resources(self) -> None:
        if self.core._computer is not None:
            self.core._computer.browser.close()
        self.core.close()

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
