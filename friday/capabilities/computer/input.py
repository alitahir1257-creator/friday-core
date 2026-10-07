"""Explicit keyboard and pointer actions; text uses literal Unicode input."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("data", INPUT_UNION)]


class InputController:
    def type_text(self, value: str) -> str:
        if len(value) > 10000:
            raise ValueError("Text is too long for one action.")
        if value and all(char.isascii() and (char.isalnum() or char == " ") for char in value):
            # ponytail: plain ASCII words use virtual keys; punctuation escaping can extend this path if needed.
            from pywinauto.keyboard import send_keys
            send_keys(value, with_spaces=True, vk_packet=False)
            return "Text entered."
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        for offset in range(0, len(value), 256):
            units = value[offset:offset + 256].encode("utf-16-le")
            events = []
            for index in range(0, len(units), 2):
                unit = int.from_bytes(units[index:index + 2], "little")
                for flags in (0x0004, 0x0004 | 0x0002):
                    event = INPUT()
                    event.type = 1
                    event.data.ki = KEYBDINPUT(0, unit, flags, 0, 0)
                    events.append(event)
            batch = (INPUT * len(events))(*events)
            if user32.SendInput(len(events), batch, ctypes.sizeof(INPUT)) != len(events):
                raise ctypes.WinError(ctypes.get_last_error())
        return "Text entered."

    def press_key(self, key: str) -> str:
        from pywinauto.keyboard import send_keys
        names = {"ENTER": "{ENTER}", "ESC": "{ESC}", "TAB": "{TAB}", "UP": "{UP}",
                 "DOWN": "{DOWN}", "LEFT": "{LEFT}", "RIGHT": "{RIGHT}",
                 "HOME": "{HOME}", "END": "{END}", "BACKSPACE": "{BACKSPACE}"}
        send_keys(names[key.upper()])
        return f"Pressed {key}."

    def press_shortcut(self, shortcut: str) -> str:
        from pywinauto.keyboard import send_keys
        modifiers = {"CTRL": "^", "ALT": "%", "SHIFT": "+"}
        parts = shortcut.upper().split("+")
        keys = "".join(modifiers[part] for part in parts[:-1]) + parts[-1].lower()
        send_keys(keys)
        return f"Pressed {shortcut}."

    def click(self, x: int, y: int, double: bool = False) -> str:
        from pywinauto.mouse import click, double_click
        if x < 0 or y < 0 or x > 32767 or y > 32767:
            raise ValueError("Coordinates are outside the screen range.")
        (double_click if double else click)(coords=(x, y))
        return "Clicked screen coordinates."

    def scroll(self, steps: int) -> str:
        from pywinauto.mouse import scroll
        if not -10 <= steps <= 10:
            raise ValueError("Scroll is limited to ten steps per action.")
        scroll(wheel_dist=steps)
        return "Scrolled."
