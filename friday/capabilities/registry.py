"""Small action contract shared by deterministic and future model planners."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Capability(str, Enum):
    COMPUTER = "COMPUTER"
    WINDOW = "WINDOW"
    INPUT = "INPUT"
    BROWSER = "BROWSER"


class Risk(str, Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Action:
    capability: Capability
    operation: str
    target: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.SAFE


@dataclass(frozen=True)
class ActionPlan:
    actions: tuple[Action, ...]
