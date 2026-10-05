"""Small in-memory contracts for bounded task planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Event
from typing import Any, Mapping
import uuid


MAX_PLAN_STEPS = 8
MAX_TOOL_CALLS = 12
MAX_STEP_RETRIES = 1
MAX_PLAN_REPAIRS = 1


class WorkerProfile(str, Enum):
    GENERAL = "GENERAL"
    COMPUTER = "COMPUTER"
    KNOWLEDGE = "KNOWLEDGE"
    RESEARCH = "RESEARCH"
    CODING = "CODING"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    DRY_RUN = "DRY_RUN"
    HANDOFF_READY = "HANDOFF_READY"


@dataclass(frozen=True)
class PlanStep:
    id: str
    description: str
    capability: str
    preferred_tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    risk_hint: str | None = None
    verification: str = "tool_success"
    profile: WorkerProfile | str = WorkerProfile.GENERAL
    status: StepStatus = StepStatus.PENDING

    def __post_init__(self) -> None:
        if isinstance(self.capability, str):
            object.__setattr__(self, "capability", self.capability.casefold())
        if isinstance(self.dependencies, (tuple, list)):
            object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "profile", WorkerProfile(str(getattr(self.profile, "value", self.profile)).upper()))
        object.__setattr__(self, "status", StepStatus(self.status))


@dataclass(frozen=True)
class TaskPlan:
    goal: str
    steps: tuple[PlanStep, ...]
    privacy: str = "public"
    namespace: str = "global"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PLANNED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_steps: int = MAX_PLAN_STEPS
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "privacy", str(getattr(self.privacy, "value", self.privacy)).casefold())
        object.__setattr__(self, "status", TaskStatus(self.status))


@dataclass(frozen=True)
class TaskApprovalRequest:
    action_summary: str
    tool: str
    risk: str
    expected_side_effect: str

    def to_dict(self) -> dict[str, str]:
        return {
            "action_summary": self.action_summary,
            "tool": self.tool,
            "risk": self.risk,
            "expected_side_effect": self.expected_side_effect,
        }


@dataclass(frozen=True)
class TaskEvent:
    event: str
    task_id: str
    step_id: str | None = None
    capability: str | None = None
    tool_name: str | None = None
    risk: str | None = None
    status: str | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class StepResult:
    step_id: str
    status: StepStatus
    tool_name: str | None = None
    risk: str | None = None
    attempt: int = 1
    summary: str = ""
    failure_code: str | None = None
    verification_passed: bool | None = None
    tool_result: Any = None


@dataclass
class TaskRun:
    plan: TaskPlan
    status: TaskStatus = TaskStatus.PLANNED
    current_step_id: str | None = None
    step_results: list[StepResult] = field(default_factory=list)
    events: list[TaskEvent] = field(default_factory=list)
    pending_approval: TaskApprovalRequest | None = None
    approval_decisions: dict[str, str] = field(default_factory=dict)
    explicit_approvals: set[str] = field(default_factory=set)
    dry_run_approval_requests: list[TaskApprovalRequest] = field(default_factory=list)
    tool_calls: int = 0
    repair_attempts: int = 0
    handoff: Mapping[str, Any] | None = None
    _cancel_event: Event = field(default_factory=Event, repr=False)

    @property
    def id(self) -> str:
        return self.plan.id

    def cancel(self) -> None:
        """Request cooperative cancellation; an active tool is allowed to finish."""
        self._cancel_event.set()

    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def snapshot(self) -> dict[str, Any]:
        """Return HUD-safe state without goals, arguments, or tool output."""
        return {
            "task_id": self.id,
            "status": self.status.value,
            "current_step_id": self.current_step_id,
            "step_statuses": {step.id: step.status.value for step in self.plan.steps},
            "completed_steps": sum(step.status == StepStatus.SUCCEEDED for step in self.plan.steps),
            "total_steps": len(self.plan.steps),
            "tool_calls": self.tool_calls,
            "repair_attempts": self.repair_attempts,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval else None,
        }


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    summary: str
    step_results: tuple[StepResult, ...]
    handoff: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ValidatedPlan:
    plan: TaskPlan
    tools: Mapping[str, Any]
    risks: Mapping[str, Any]
