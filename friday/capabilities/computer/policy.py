"""Policy is enforced by operation and context, never by planner-supplied risk."""

from __future__ import annotations

import re

from friday.capabilities.registry import Action, Capability, Risk


_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|erase|wipe|format|uninstall|rm|del)\b|\b(hatao|mitao|delete karo)\b",
    re.I,
)
_CONSEQUENTIAL = re.compile(
    r"\b(send|submit|post|publish|buy|purchase|pay|transfer|install|password|security|credential|secret|admin|elevat)\w*\b",
    re.I,
)
_SAFE_KEYS = frozenset({"ESC", "TAB", "UP", "DOWN", "LEFT", "RIGHT", "HOME", "END", "BACKSPACE"})
_SAFE_SHORTCUTS = frozenset({"CTRL+L", "CTRL+T", "CTRL+R", "CTRL+F", "CTRL+C", "CTRL+A", "ALT+TAB"})
_ACTION_START = re.compile(
    r"^(?:please\s+)?(?:delete|remove|erase|wipe|format|uninstall|send|submit|post|publish|buy|purchase|pay|transfer|install|change|expose|share)\b",
    re.I,
)
_BENIGN_BUTTON = re.compile(r"^(?:search|find|next|previous|back|show|view|open|menu|more|expand|collapse)$", re.I)


class ApprovalPolicy:
    def classify_request(self, request: str) -> Risk:
        if not _ACTION_START.search(request.strip()):
            return Risk.SAFE
        if _DESTRUCTIVE.search(request):
            return Risk.BLOCKED
        if _CONSEQUENTIAL.search(request):
            return Risk.CONFIRM
        return Risk.SAFE

    def classify(self, action: Action) -> Risk:
        op = action.operation.upper()
        if op in {"DELETE", "UNINSTALL", "EXECUTE", "RUN_COMMAND", "READ_SECRET", "ELEVATE"}:
            return Risk.BLOCKED
        if op == "CLOSE_WINDOW" or op == "CLOSE":
            return Risk.CONFIRM
        if op in {"SEND", "SUBMIT", "POST", "PURCHASE", "PAY", "INSTALL"}:
            return Risk.CONFIRM
        if action.capability == Capability.INPUT:
            if op in {"CLICK", "DOUBLE_CLICK", "TYPE_TEXT"}:
                return Risk.CONFIRM  # Raw input lacks reliable target semantics.
            if op == "PRESS_KEY":
                if action.target.upper() == "ENTER":
                    return Risk.CONFIRM
                return Risk.SAFE if action.target.upper() in _SAFE_KEYS else Risk.BLOCKED
            if op == "PRESS_SHORTCUT":
                return Risk.SAFE if action.target.upper() in _SAFE_SHORTCUTS else Risk.BLOCKED
        if op == "TYPE_ELEMENT":
            name = str(action.parameters.get("name", ""))
            if not name or re.search(r"\b(password|secret|token|credential|card|cvv)\b", name, re.I):
                return Risk.BLOCKED
            return Risk.SAFE
        if op in {"CLICK_CONTROL", "CLICK_ELEMENT"}:
            label = str(action.parameters.get("name", ""))
            if not label.strip():
                return Risk.CONFIRM if action.parameters.get("automation_id") else Risk.BLOCKED
            if _DESTRUCTIVE.search(label):
                return Risk.BLOCKED
            if _CONSEQUENTIAL.search(label):
                return Risk.CONFIRM
            if op == "CLICK_CONTROL":
                return Risk.CONFIRM
            if str(action.parameters.get("role", "")).casefold() == "button" and not _BENIGN_BUTTON.fullmatch(label.strip()):
                return Risk.CONFIRM
        return Risk.SAFE
