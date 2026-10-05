"""Small, FRIDAY-owned contracts for tool definitions and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE = "REVERSIBLE"
    SENSITIVE = "SENSITIVE"
    CONSEQUENTIAL = "CONSEQUENTIAL"
    DESTRUCTIVE = "DESTRUCTIVE"


class ApprovalDecision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    DENY = "DENY"


@dataclass(frozen=True)
class ToolInvocationContext:
    caller: str = "user"
    privacy: str = "public"
    explicit_approval: bool = False
    action_metadata: Mapping[str, Any] = field(default_factory=dict)
    cancellation_check: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized = str(getattr(self.privacy, "value", self.privacy)).casefold()
        if normalized not in {"public", "private", "local_only"}:
            raise ValueError("privacy must be public, private, or local_only.")
        object.__setattr__(self, "privacy", normalized)


@dataclass(frozen=True)
class ToolApprovalRequest:
    tool_name: str
    risk_level: RiskLevel
    privacy: str
    caller: str
    argument_names: tuple[str, ...]
    summary: str


ToolHandler = Callable[[dict[str, Any], ToolInvocationContext], Any]
RiskResolver = Callable[[dict[str, Any], ToolInvocationContext], RiskLevel | str]
PrivacyResolver = Callable[[dict[str, Any], ToolInvocationContext], bool]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    capabilities: frozenset[str] = frozenset()
    risk_level: RiskLevel | str | None = None
    privacy_support: frozenset[str] = frozenset({"public"})
    source: str = "native"
    enabled: bool = True
    requires_confirmation: bool = False
    timeout_seconds: float = 10.0
    side_effects: str = "none"
    transmits_data: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    risk_resolver: RiskResolver | None = field(default=None, repr=False, compare=False)
    privacy_resolver: PrivacyResolver | None = field(default=None, repr=False, compare=False)

    @property
    def canonical_name(self) -> str:
        return f"{self.namespace}.{self.name}"


@dataclass(frozen=True)
class ToolReceipt:
    timestamp: str
    tool_name: str
    risk_level: RiskLevel
    approval_result: str
    success: bool
    duration_ms: float
    side_effects: str


@dataclass(frozen=True)
class ToolResult:
    success: bool
    tool_name: str
    status: str
    summary: str
    structured_data: Any = None
    error_code: str | None = None
    duration_ms: float = 0.0
    side_effects: str = "none"
    requires_followup: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
