"""Local, bounded automation scheduling through FRIDAY's existing tool path."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time as wall_time, timedelta
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Protocol
import uuid

from friday.memory.policy import contains_possible_secret, normalize_namespace
from friday.paths import AUTOMATION_DB_PATH
from friday.planner.models import TaskStatus
from friday.tools.models import ToolResult


class TriggerType(str, Enum):
    ONE_TIME = "ONE_TIME"
    RECURRING_INTERVAL = "RECURRING_INTERVAL"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    EVENT = "EVENT"


class AutomationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ConditionState(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AutomationTrigger:
    type: TriggerType | str
    run_at: datetime | None = None
    interval_seconds: float | None = None
    at_time: wall_time | str | None = None
    weekday: int | None = None
    event_name: str | None = None
    anchor_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            kind = TriggerType(str(getattr(self.type, "value", self.type)).upper())
        except ValueError as error:
            raise ValueError("Unsupported automation trigger type.") from error
        object.__setattr__(self, "type", kind)
        if isinstance(self.at_time, str):
            try:
                object.__setattr__(self, "at_time", wall_time.fromisoformat(self.at_time))
            except ValueError as error:
                raise ValueError("at_time must be an HH:MM[:SS] local time.") from error
        if self.run_at is not None:
            _require_aware(self.run_at, "run_at")
        if self.anchor_at is not None:
            _require_aware(self.anchor_at, "anchor_at")
        if kind == TriggerType.ONE_TIME:
            if self.run_at is None:
                raise ValueError("ONE_TIME requires run_at.")
        elif kind == TriggerType.RECURRING_INTERVAL:
            if self.interval_seconds is None or isinstance(self.interval_seconds, bool):
                raise ValueError("RECURRING_INTERVAL requires interval_seconds.")
            interval = float(self.interval_seconds)
            if not math.isfinite(interval) or not 0 < interval <= 31_536_000:
                raise ValueError("interval_seconds must be greater than 0 and at most one year.")
            object.__setattr__(self, "interval_seconds", interval)
        elif kind == TriggerType.DAILY:
            if not isinstance(self.at_time, wall_time) or self.at_time.tzinfo is not None:
                raise ValueError("DAILY requires a local at_time without a timezone.")
        elif kind == TriggerType.WEEKLY:
            if not isinstance(self.at_time, wall_time) or self.at_time.tzinfo is not None:
                raise ValueError("WEEKLY requires a local at_time without a timezone.")
            if isinstance(self.weekday, bool) or self.weekday not in range(7):
                raise ValueError("WEEKLY weekday must be Monday=0 through Sunday=6.")
        elif kind == TriggerType.EVENT:
            if not isinstance(self.event_name, str) or not self.event_name.strip() or len(self.event_name) > 128:
                raise ValueError("EVENT requires an event_name of 1 to 128 characters.")


@dataclass(frozen=True)
class Automation:
    id: str
    name: str
    description: str
    enabled: bool
    privacy: str
    namespace: str
    trigger: AutomationTrigger
    task_goal: str
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_run_at: datetime | None
    status: AutomationStatus
    max_runs: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutomationRun:
    id: str
    automation_id: str
    occurrence_key: str
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: RunStatus
    result_summary: str
    approval_state: str
    duration_ms: float | None
    tool_count: int
    task_id: str | None
    approval_expires_at: datetime | None
    restore_status: str = AutomationStatus.ACTIVE.value
    dispatch_kind: str = "planner"


@dataclass(frozen=True)
class AutomationResult:
    run_id: str
    automation_id: str
    status: RunStatus
    summary: str
    approval_state: str


@dataclass(frozen=True)
class EventEvaluation:
    state: ConditionState | str
    occurrence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ConditionState(str(getattr(self.state, "value", self.state)).upper()))


class EventSource(Protocol):
    def evaluate(self, trigger: AutomationTrigger, now: datetime) -> EventEvaluation: ...


class LocalTestEventSource:
    """Deterministic local event source for tests and explicitly published events."""

    def __init__(self) -> None:
        self._events: dict[str, EventEvaluation] = {}
        self._lock = threading.Lock()

    def publish(
        self, event_name: str, occurrence_id: str,
        state: ConditionState | str = ConditionState.TRUE,
    ) -> None:
        if not event_name or not occurrence_id or len(occurrence_id) > 4096:
            raise ValueError("event_name and occurrence_id are required.")
        with self._lock:
            self._events[event_name] = EventEvaluation(state, occurrence_id)

    def evaluate(self, trigger: AutomationTrigger, _now: datetime) -> EventEvaluation:
        with self._lock:
            return self._events.get(trigger.event_name or "", EventEvaluation(ConditionState.UNKNOWN))


def _require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value


def _clock_now() -> datetime:
    return datetime.now().astimezone()


def _dt_text(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds") if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _trigger_dict(trigger: AutomationTrigger) -> dict[str, Any]:
    return {
        "type": trigger.type.value,
        "run_at": _dt_text(trigger.run_at),
        "interval_seconds": trigger.interval_seconds,
        "at_time": trigger.at_time.isoformat() if trigger.at_time else None,
        "weekday": trigger.weekday,
        "event_name": trigger.event_name,
        "anchor_at": _dt_text(trigger.anchor_at),
    }


def _trigger_from_json(value: str) -> AutomationTrigger:
    data = json.loads(value)
    return AutomationTrigger(
        type=data["type"], run_at=_parse_dt(data.get("run_at")),
        interval_seconds=data.get("interval_seconds"), at_time=data.get("at_time"),
        weekday=data.get("weekday"), event_name=data.get("event_name"),
        anchor_at=_parse_dt(data.get("anchor_at")),
    )


class AutomationScheduler:
    """Timezone-aware occurrence arithmetic; callers drive it through tick()."""

    @staticmethod
    def next_after(trigger: AutomationTrigger, after: datetime) -> datetime | None:
        _require_aware(after, "after")
        if trigger.type == TriggerType.ONE_TIME or trigger.type == TriggerType.EVENT:
            return None
        if trigger.type == TriggerType.RECURRING_INTERVAL:
            anchor = trigger.anchor_at or after
            interval = timedelta(seconds=float(trigger.interval_seconds or 0))
            elapsed = (after - anchor).total_seconds()
            steps = max(1, math.floor(elapsed / interval.total_seconds()) + 1)
            return anchor + steps * interval
        candidate = datetime.combine(after.date(), trigger.at_time, tzinfo=after.tzinfo)
        if trigger.type == TriggerType.DAILY:
            return candidate if candidate > after else candidate + timedelta(days=1)
        days = (int(trigger.weekday) - after.weekday()) % 7
        candidate += timedelta(days=days)
        return candidate if candidate > after else candidate + timedelta(days=7)

    @staticmethod
    def seconds_until_next(automations: list[Automation], now: datetime) -> float | None:
        _require_aware(now, "now")
        future = [item.next_run_at for item in automations if item.enabled and item.status == AutomationStatus.ACTIVE
                  and item.next_run_at is not None and item.next_run_at > now]
        return max(0.0, (min(future) - now).total_seconds()) if future else None


class _AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                enabled INTEGER NOT NULL, privacy TEXT NOT NULL, namespace TEXT NOT NULL,
                trigger_json TEXT NOT NULL, task_goal TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                next_run_at TEXT, last_run_at TEXT, status TEXT NOT NULL,
                max_runs INTEGER, metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS automations_due_idx ON automations(status, enabled, next_run_at);
            CREATE TABLE IF NOT EXISTS automation_runs (
                id TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES automations(id),
                occurrence_key TEXT NOT NULL, scheduled_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, status TEXT NOT NULL, result_summary TEXT NOT NULL,
                approval_state TEXT NOT NULL, duration_ms REAL, tool_count INTEGER NOT NULL DEFAULT 0,
                task_id TEXT, approval_expires_at TEXT, restore_status TEXT NOT NULL,
                dispatch_kind TEXT NOT NULL DEFAULT 'planner',
                UNIQUE(automation_id, occurrence_key)
            );
            CREATE INDEX IF NOT EXISTS automation_runs_queue_idx ON automation_runs(status, scheduled_at, id);
            CREATE INDEX IF NOT EXISTS automation_runs_approval_idx ON automation_runs(status, approval_expires_at);
            """
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def automation(self, automation_id: str) -> Automation | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
        return self._automation(row) if row else None

    def automations(self) -> list[Automation]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM automations ORDER BY julianday(created_at), id").fetchall()
        return [self._automation(row) for row in rows]

    def save_automation(self, item: Automation) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO automations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,
                enabled=excluded.enabled, privacy=excluded.privacy, namespace=excluded.namespace,
                trigger_json=excluded.trigger_json, task_goal=excluded.task_goal,
                updated_at=excluded.updated_at, next_run_at=excluded.next_run_at,
                last_run_at=excluded.last_run_at, status=excluded.status,
                max_runs=excluded.max_runs, metadata_json=excluded.metadata_json""",
                (item.id, item.name, item.description, int(item.enabled), item.privacy, item.namespace,
                 json.dumps(_trigger_dict(item.trigger), separators=(",", ":")), item.task_goal,
                 _dt_text(item.created_at), _dt_text(item.updated_at), _dt_text(item.next_run_at),
                 _dt_text(item.last_run_at), item.status.value, item.max_runs,
                 json.dumps(item.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )

    def claim(
        self, item: Automation, occurrence_key: str, scheduled_at: datetime,
        next_run_at: datetime | None, restore_status: AutomationStatus,
        *, expected_next_run: datetime | None = None, manual: bool = False, queue_limit: int = 32,
    ) -> AutomationRun | None:
        run_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._db.execute("SELECT * FROM automations WHERE id=?", (item.id,)).fetchone()
                if not current or (not manual and (not current["enabled"] or current["status"] != AutomationStatus.ACTIVE.value)):
                    self._db.rollback()
                    return None
                if expected_next_run is not None and current["next_run_at"] != _dt_text(expected_next_run):
                    self._db.rollback()
                    return None
                if current["max_runs"] is not None:
                    count = self._db.execute("SELECT COUNT(*) FROM automation_runs WHERE automation_id=?", (item.id,)).fetchone()[0]
                    if count >= current["max_runs"]:
                        self._db.execute("UPDATE automations SET status='COMPLETED', enabled=0, next_run_at=NULL WHERE id=?", (item.id,))
                        self._db.commit()
                        return None
                queued = self._db.execute(
                    "SELECT COUNT(*) FROM automation_runs WHERE status='QUEUED'"
                ).fetchone()[0]
                live_for_automation = self._db.execute(
                    "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND status IN ('QUEUED','RUNNING','WAITING_APPROVAL')",
                    (item.id,),
                ).fetchone()[0]
                if queued >= queue_limit or (manual and live_for_automation):
                    self._db.rollback()
                    return None
                self._db.execute(
                    """INSERT INTO automation_runs
                    (id,automation_id,occurrence_key,scheduled_at,status,result_summary,approval_state,
                     tool_count,restore_status,dispatch_kind)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, item.id, occurrence_key, _dt_text(scheduled_at), RunStatus.QUEUED.value,
                     "Queued.", "NOT_REQUIRED", 0, restore_status.value, "planner"),
                )
                stored_next = current["next_run_at"] if manual or item.trigger.type == TriggerType.EVENT else _dt_text(next_run_at)
                self._db.execute(
                    "UPDATE automations SET next_run_at=?, status='RUNNING', updated_at=? WHERE id=?",
                    (stored_next, _dt_text(_clock_now()), item.id),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None
            except Exception:
                self._db.rollback()
                raise
        return self.run(run_id)

    def claim_approval(self, run_id: str, now: datetime) -> AutomationRun | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT status,approval_expires_at FROM automation_runs WHERE id=?", (run_id,)
                ).fetchone()
                if (row is None or row["status"] != RunStatus.WAITING_APPROVAL.value
                        or not row["approval_expires_at"] or _parse_dt(row["approval_expires_at"]) <= now):
                    self._db.rollback()
                    return None
                self._db.execute(
                    "UPDATE automation_runs SET status='RUNNING', approval_state='PROCESSING' WHERE id=?",
                    (run_id,),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.run(run_id)

    def expire_approval(self, run_id: str, now: datetime) -> AutomationRun | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT status,approval_expires_at FROM automation_runs WHERE id=?", (run_id,)
                ).fetchone()
                if (row is None or row["status"] != RunStatus.WAITING_APPROVAL.value
                        or (row["approval_expires_at"] and _parse_dt(row["approval_expires_at"]) > now)):
                    self._db.rollback()
                    return None
                self._db.execute(
                    "UPDATE automation_runs SET status='EXPIRED', finished_at=?, result_summary=?, "
                    "approval_state='EXPIRED', approval_expires_at=NULL WHERE id=?",
                    (_dt_text(now), "Approval expired; task skipped.", run_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.run(run_id)

    def cancel_pending(self, run_id: str, now: datetime) -> AutomationRun | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT status FROM automation_runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None or row["status"] not in {RunStatus.QUEUED.value, RunStatus.WAITING_APPROVAL.value}:
                    self._db.rollback()
                    return None
                self._db.execute(
                    "UPDATE automation_runs SET status='CANCELLED', finished_at=?, result_summary=?, "
                    "approval_state='CANCELLED', approval_expires_at=NULL WHERE id=?",
                    (_dt_text(now), "Task cancelled.", run_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.run(run_id)

    def run(self, run_id: str) -> AutomationRun | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def run_for_occurrence(self, automation_id: str, occurrence_key: str) -> AutomationRun | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM automation_runs WHERE automation_id=? AND occurrence_key=?", (automation_id, occurrence_key)).fetchone()
        return self._run(row) if row else None

    def queued_runs(self, limit: int) -> list[AutomationRun]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM automation_runs WHERE status='QUEUED' ORDER BY julianday(scheduled_at), id LIMIT ?", (limit,)
            ).fetchall()
        return [self._run(row) for row in rows]

    def queued_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM automation_runs WHERE status='QUEUED'").fetchone()[0])

    def run_count(self, automation_id: str) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM automation_runs WHERE automation_id=?", (automation_id,)
            ).fetchone()[0])

    def update_run(self, run_id: str, **values: Any) -> None:
        allowed = {
            "started_at", "finished_at", "status", "result_summary", "approval_state", "duration_ms",
            "tool_count", "task_id", "approval_expires_at", "dispatch_kind",
        }
        if values.keys() - allowed:
            raise ValueError("Unsupported run update field.")
        if not values:
            return
        for key in ("started_at", "finished_at", "approval_expires_at"):
            if key in values and isinstance(values[key], datetime):
                values[key] = _dt_text(values[key])
        fields = ",".join(f"{name}=?" for name in values)
        with self._lock, self._db:
            self._db.execute(f"UPDATE automation_runs SET {fields} WHERE id=?", (*values.values(), run_id))

    def set_automation_state(
        self, automation_id: str, status: AutomationStatus, *, now: datetime,
        last_run_at: datetime | None = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE automations SET status=?, updated_at=?, last_run_at=COALESCE(?,last_run_at) WHERE id=?",
                (status.value, _dt_text(now), _dt_text(last_run_at), automation_id),
            )

    def history(self, automation_id: str, limit: int) -> list[AutomationRun]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM automation_runs WHERE automation_id=? ORDER BY julianday(scheduled_at) DESC, id DESC LIMIT ?",
                (automation_id, limit),
            ).fetchall()
        return [self._run(row) for row in rows]

    def expiring(self, now: datetime) -> list[AutomationRun]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM automation_runs WHERE status='WAITING_APPROVAL'"
            ).fetchall()
        return [self._run(row) for row in rows if row["approval_expires_at"] and _parse_dt(row["approval_expires_at"]) <= now]

    def recover(self, now: datetime) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT * FROM automation_runs WHERE status IN ('RUNNING','WAITING_APPROVAL')"
                ).fetchall()
                for row in rows:
                    run_status = RunStatus.EXPIRED if row["status"] == RunStatus.WAITING_APPROVAL.value else RunStatus.FAILED
                    summary = "Interrupted approval expired." if run_status == RunStatus.EXPIRED else "Interrupted before completion."
                    self._db.execute(
                        "UPDATE automation_runs SET status=?, finished_at=?, result_summary=?, approval_state=?, approval_expires_at=NULL WHERE id=?",
                        (run_status.value, _dt_text(now), summary,
                         "EXPIRED" if run_status == RunStatus.EXPIRED else row["approval_state"], row["id"]),
                    )
                    restore = AutomationStatus(row["restore_status"])
                    automation_row = self._db.execute(
                        "SELECT trigger_json,enabled,next_run_at FROM automations WHERE id=?", (row["automation_id"],)
                    ).fetchone()
                    one_time_scheduled = bool(
                        automation_row and row["occurrence_key"].startswith("time:")
                        and _trigger_from_json(automation_row["trigger_json"]).type == TriggerType.ONE_TIME
                    )
                    enabled = automation_row["enabled"] if automation_row else 1
                    next_run = automation_row["next_run_at"] if automation_row else None
                    if one_time_scheduled and run_status == RunStatus.FAILED:
                        restore = AutomationStatus.FAILED
                    elif one_time_scheduled and run_status == RunStatus.EXPIRED:
                        restore, enabled, next_run = AutomationStatus.COMPLETED, 0, None
                    if restore == AutomationStatus.RUNNING:
                        restore = AutomationStatus.ACTIVE
                    self._db.execute(
                        "UPDATE automations SET status=?, enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                        (restore.value, enabled, next_run, _dt_text(now), row["automation_id"]),
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _automation(row: sqlite3.Row) -> Automation:
        return Automation(
            row["id"], row["name"], row["description"], bool(row["enabled"]), row["privacy"], row["namespace"],
            _trigger_from_json(row["trigger_json"]), row["task_goal"], _parse_dt(row["created_at"]),
            _parse_dt(row["updated_at"]), _parse_dt(row["next_run_at"]), _parse_dt(row["last_run_at"]),
            AutomationStatus(row["status"]), row["max_runs"], json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> AutomationRun:
        return AutomationRun(
            row["id"], row["automation_id"], row["occurrence_key"], _parse_dt(row["scheduled_at"]),
            _parse_dt(row["started_at"]), _parse_dt(row["finished_at"]), RunStatus(row["status"]),
            row["result_summary"], row["approval_state"], row["duration_ms"], row["tool_count"],
            row["task_id"], _parse_dt(row["approval_expires_at"]), row["restore_status"], row["dispatch_kind"],
        )


@dataclass(frozen=True)
class _DispatchOutcome:
    status: RunStatus
    summary: str
    approval_state: str = "NOT_REQUIRED"
    task_id: str | None = None
    tool_count: int = 0
    dispatch_kind: str = "planner"
    direct_result: ToolResult | None = field(default=None, repr=False, compare=False)


class AutomationRunner:
    """Dispatch only through the existing planner/worker, or the registered direct path."""

    def __init__(self, planner: Any, worker_executor: Any, direct_action: Callable[..., ToolResult | None] | None = None) -> None:
        self.planner = planner
        self.worker_executor = worker_executor
        self.direct_action = direct_action

    def execute(self, automation: Automation, cancelled: Callable[[], bool]) -> _DispatchOutcome:
        if self.direct_action is not None:
            direct = self.direct_action(automation.task_goal, automation.privacy)
            if direct is not None:
                return self._direct_outcome(direct)
        if cancelled():
            return _DispatchOutcome(RunStatus.CANCELLED, "Cancelled before planning.")
        plan = self.planner.plan(
            automation.task_goal, privacy=automation.privacy, namespace=automation.namespace,
        )
        if cancelled():
            return _DispatchOutcome(RunStatus.CANCELLED, "Cancelled before execution.")
        result = self.worker_executor.execute(plan)
        state = result.status if isinstance(result.status, TaskStatus) else TaskStatus(str(result.status))
        current = getattr(self.worker_executor, "current_task", None)
        tool_count = int(getattr(current, "tool_calls", 0))
        if state == TaskStatus.WAITING_APPROVAL:
            return _DispatchOutcome(RunStatus.WAITING_APPROVAL, "Waiting for explicit approval.",
                                    "REQUIRED", result.task_id, tool_count)
        if state == TaskStatus.SUCCEEDED:
            return _DispatchOutcome(RunStatus.COMPLETED, "Task completed.", "NOT_REQUIRED", result.task_id, tool_count)
        if state == TaskStatus.CANCELLED:
            return _DispatchOutcome(RunStatus.CANCELLED, "Task cancelled.", "DECLINED", result.task_id, tool_count)
        return _DispatchOutcome(RunStatus.FAILED, "Task did not complete.", "NOT_REQUIRED", result.task_id, tool_count)

    @staticmethod
    def _direct_outcome(result: ToolResult) -> _DispatchOutcome:
        if result.requires_followup:
            return _DispatchOutcome(RunStatus.WAITING_APPROVAL, "Waiting for explicit approval.",
                                    "REQUIRED", tool_count=1, dispatch_kind="direct", direct_result=result)
        if result.success:
            return _DispatchOutcome(RunStatus.COMPLETED, "Task completed.", tool_count=1,
                                    dispatch_kind="direct", direct_result=result)
        return _DispatchOutcome(RunStatus.FAILED, "Task did not complete.", "NOT_REQUIRED",
                                tool_count=1, dispatch_kind="direct", direct_result=result)


class AutomationManager:
    """Persistent CRUD, safe recovery, bounded scheduling, approvals, and history."""

    DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=24)

    def __init__(
        self, planner: Any, worker_executor: Any, *, database_path: str | Path | None = None,
        event_source: EventSource | None = None, event_sink: Callable[[dict[str, Any]], None] | None = None,
        direct_action: Callable[..., ToolResult | None] | None = None,
        clock: Callable[[], datetime] = _clock_now,
        approval_timeout: timedelta = DEFAULT_APPROVAL_TIMEOUT, queue_limit: int = 32,
    ) -> None:
        if (not isinstance(approval_timeout, timedelta) or approval_timeout <= timedelta(0)
                or approval_timeout > timedelta(days=30)):
            raise ValueError("approval_timeout must be between 0 and 30 days.")
        if isinstance(queue_limit, bool) or not isinstance(queue_limit, int) or not 1 <= queue_limit <= 256:
            raise ValueError("queue_limit must be between 1 and 256.")
        override = os.environ.get("FRIDAY_AUTOMATION_DB_PATH")
        path = Path(database_path or override or AUTOMATION_DB_PATH)
        self.store = _AutomationStore(path)
        self.runner = AutomationRunner(planner, worker_executor, direct_action)
        self.worker_executor = worker_executor
        self.event_source = event_source
        self.event_sink = event_sink
        self.clock = clock
        self.approval_timeout = approval_timeout
        self.queue_limit = queue_limit
        self._state_lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._current_run_id: str | None = None
        self._cancelled: set[str] = set()
        self.store.recover(self._now())

    @classmethod
    def from_core(cls, core: Any, **kwargs: Any) -> AutomationManager:
        """Attach to FRIDAY's existing planner, executor, and deterministic computer path."""
        kwargs.setdefault("direct_action", core.dispatch_automation_computer)
        return cls(core.planner, core.worker_executor, **kwargs)

    @property
    def database_path(self) -> Path:
        return self.store.path

    @property
    def current_run_id(self) -> str | None:
        with self._state_lock:
            return self._current_run_id

    def create(
        self, name: str, task_goal: str, trigger: AutomationTrigger, *, description: str = "",
        privacy: str = "public", namespace: str = "global", enabled: bool = True,
        max_runs: int | None = None, metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Automation:
        current = _require_aware(now or self._now(), "now")
        if not isinstance(trigger, AutomationTrigger):
            raise ValueError("trigger must be an AutomationTrigger.")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        name = self._text(name, "name", 200)
        task_goal = self._text(task_goal, "task_goal", 2000)
        description = self._text(description, "description", 2000, allow_empty=True)
        privacy = str(getattr(privacy, "value", privacy)).casefold()
        if privacy not in {"public", "private", "local_only"}:
            raise ValueError("privacy must be public, private, or local_only.")
        namespace = normalize_namespace(namespace)
        if max_runs is not None and (isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1):
            raise ValueError("max_runs must be a positive integer or None.")
        attributes = self._metadata(metadata)
        if trigger.type == TriggerType.RECURRING_INTERVAL and trigger.anchor_at is None:
            trigger = replace(trigger, anchor_at=current)
        next_run = trigger.run_at if trigger.type == TriggerType.ONE_TIME else AutomationScheduler.next_after(trigger, current)
        status = AutomationStatus.ACTIVE if enabled else AutomationStatus.DISABLED
        item = Automation(uuid.uuid4().hex, name, description, enabled, privacy, namespace, trigger,
                          task_goal, current, current, next_run if enabled else None, None, status,
                          max_runs, attributes)
        self.store.save_automation(item)
        self._emit("automation_created", item)
        return item

    def get(self, automation_id: str) -> Automation | None:
        return self.store.automation(automation_id)

    def list(self) -> list[Automation]:
        return self.store.automations()

    def update(self, automation_id: str, *, now: datetime | None = None, **changes: Any) -> Automation:
        allowed = {"name", "description", "task_goal", "trigger", "privacy", "enabled"}
        if not changes or changes.keys() - allowed:
            raise ValueError("update accepts name, description, task_goal, trigger, privacy, and enabled.")
        current = _require_aware(now or self._now(), "now")
        item = self._required(automation_id)
        if self._has_live_run(automation_id):
            raise RuntimeError("A queued, running, or approval-waiting automation cannot be edited.")
        values = dict(changes)
        if "name" in values:
            values["name"] = self._text(values["name"], "name", 200)
        if "description" in values:
            values["description"] = self._text(values["description"], "description", 2000, allow_empty=True)
        if "task_goal" in values:
            values["task_goal"] = self._text(values["task_goal"], "task_goal", 2000)
        if "privacy" in values:
            privacy = str(getattr(values["privacy"], "value", values["privacy"])).casefold()
            if privacy not in {"public", "private", "local_only"}:
                raise ValueError("privacy must be public, private, or local_only.")
            values["privacy"] = privacy
        if "trigger" in values and not isinstance(values["trigger"], AutomationTrigger):
            raise ValueError("trigger must be an AutomationTrigger.")
        if "enabled" in values and not isinstance(values["enabled"], bool):
            raise ValueError("enabled must be a boolean.")
        updated = replace(item, **values, updated_at=current)
        if values.get("enabled") is False:
            updated = replace(updated, enabled=False, next_run_at=None, status=AutomationStatus.DISABLED)
        elif item.status == AutomationStatus.PAUSED:
            updated = replace(updated, enabled=True, next_run_at=None)
        elif item.status in {AutomationStatus.DISABLED, AutomationStatus.COMPLETED} and values.get("enabled") is True:
            trigger = self._reanchor(updated.trigger, current)
            updated = replace(updated, enabled=True, status=AutomationStatus.ACTIVE,
                              trigger=trigger, next_run_at=self._initial_next(trigger, current))
        elif "trigger" in values and item.status in {AutomationStatus.ACTIVE, AutomationStatus.FAILED}:
            trigger = self._reanchor(updated.trigger, current)
            updated = replace(updated, enabled=True, status=AutomationStatus.ACTIVE,
                              trigger=trigger, next_run_at=self._initial_next(trigger, current))
        elif item.status == AutomationStatus.FAILED and values.get("enabled") is True:
            trigger = self._reanchor(updated.trigger, current)
            updated = replace(updated, enabled=True, status=AutomationStatus.ACTIVE,
                              trigger=trigger, next_run_at=self._initial_next(trigger, current))
        self.store.save_automation(updated)
        return updated

    def pause(self, automation_id: str, *, now: datetime | None = None) -> Automation:
        item = self._required(automation_id)
        if self._has_live_run(automation_id):
            raise RuntimeError("A queued, running, or approval-waiting automation cannot be paused.")
        updated = replace(item, enabled=True, status=AutomationStatus.PAUSED,
                          next_run_at=None, updated_at=_require_aware(now or self._now(), "now"))
        self.store.save_automation(updated)
        self._emit("automation_paused", updated)
        return updated

    def resume(self, automation_id: str, *, now: datetime | None = None) -> Automation:
        current = _require_aware(now or self._now(), "now")
        item = self._required(automation_id)
        if self._has_live_run(automation_id):
            raise RuntimeError("Cancel the active run before resuming this automation.")
        if item.status not in {AutomationStatus.PAUSED, AutomationStatus.DISABLED, AutomationStatus.FAILED}:
            raise RuntimeError("Only paused, disabled, or failed automations can be resumed.")
        trigger = self._reanchor(item.trigger, current)
        next_run = self._initial_next(trigger, current)
        updated = replace(item, enabled=True, status=AutomationStatus.ACTIVE, trigger=trigger,
                          next_run_at=next_run, updated_at=current)
        self.store.save_automation(updated)
        self._emit("automation_resumed", updated)
        return updated

    def disable(self, automation_id: str, *, now: datetime | None = None) -> Automation:
        item = self._required(automation_id)
        if self._has_live_run(automation_id):
            raise RuntimeError("Cancel the active run before disabling this automation.")
        updated = replace(item, enabled=False, status=AutomationStatus.DISABLED, next_run_at=None,
                          updated_at=_require_aware(now or self._now(), "now"))
        self.store.save_automation(updated)
        self._emit("automation_disabled", updated)
        return updated

    archive = disable

    def history(self, automation_id: str, *, limit: int = 100) -> list[AutomationRun]:
        self._required(automation_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("history limit must be between 1 and 1000.")
        return self.store.history(automation_id, limit)

    def run_now(self, automation_id: str, *, now: datetime | None = None) -> AutomationResult:
        current = _require_aware(now or self._now(), "now")
        item = self._required(automation_id)
        if self._has_live_run(automation_id):
            raise RuntimeError("This automation already has a queued or active run.")
        if self.store.queued_count() >= self.queue_limit:
            raise RuntimeError("The automation queue is full.")
        run = self.store.claim(item, f"manual:{uuid.uuid4().hex}", current, item.next_run_at,
                               item.status, expected_next_run=None, manual=True, queue_limit=self.queue_limit)
        if run is None:
            raise RuntimeError("The automation could not be queued.")
        self._emit("automation_due", item, run)
        self._drain_queue()
        return self._result(run.id)

    def tick(self, now: datetime | None = None) -> list[AutomationResult]:
        current = _require_aware(now or self._now(), "now")
        self._expire_approvals(current)
        capacity = max(0, self.queue_limit - self.store.queued_count())
        if capacity:
            for item in self.list():
                if not capacity:
                    break
                if not item.enabled or item.status != AutomationStatus.ACTIVE:
                    continue
                if item.trigger.type == TriggerType.EVENT:
                    if self.event_source is None:
                        continue
                    try:
                        evaluation = self.event_source.evaluate(item.trigger, current)
                        if not isinstance(evaluation, EventEvaluation):
                            raise ValueError("event source returned an invalid evaluation")
                    except Exception:
                        evaluation = EventEvaluation(ConditionState.UNKNOWN)
                    if (evaluation.state != ConditionState.TRUE
                            or not isinstance(evaluation.occurrence_id, str)
                            or not evaluation.occurrence_id or len(evaluation.occurrence_id) > 4096):
                        continue
                    identity = f"{item.trigger.event_name}\0{evaluation.occurrence_id}"
                    occurrence_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    key = f"event:{occurrence_hash}"
                    run = self.store.claim(item, key, current, None, AutomationStatus.ACTIVE,
                                           queue_limit=self.queue_limit)
                else:
                    scheduled = item.next_run_at
                    if scheduled is None or scheduled > current:
                        continue
                    next_run = None if item.trigger.type == TriggerType.ONE_TIME else AutomationScheduler.next_after(item.trigger, current)
                    key = f"time:{_dt_text(scheduled)}"
                    run = self.store.claim(item, key, scheduled, next_run, AutomationStatus.ACTIVE,
                                           expected_next_run=scheduled, queue_limit=self.queue_limit)
                if run is not None:
                    capacity -= 1
                    self._emit("automation_due", item, run)
        before = {run.id for run in self.store.queued_runs(self.queue_limit)}
        self._drain_queue(current)
        return [self._result(run_id) for run_id in before]

    def approve(self, run_id: str, approved: bool, *, now: datetime | None = None) -> AutomationResult:
        if not isinstance(approved, bool):
            raise ValueError("approved must be a boolean.")
        current = _require_aware(now or self._now(), "now")
        run = self.store.run(run_id)
        if run is None or run.status != RunStatus.WAITING_APPROVAL:
            raise RuntimeError("This automation run is not waiting for approval.")
        if run.approval_expires_at is None or run.approval_expires_at <= current:
            self._expire_run(run, current)
            self._drain_queue(current)
            return self._result(run_id)
        if not self._dispatch_lock.acquire(blocking=False):
            raise RuntimeError("The automation runner is busy.")
        waiting = False
        try:
            claimed = self.store.claim_approval(run_id, current)
            if claimed is None:
                latest = self.store.run(run_id)
                if latest is not None and latest.status == RunStatus.WAITING_APPROVAL \
                        and (latest.approval_expires_at is None or latest.approval_expires_at <= current):
                    self._expire_run(latest, current)
                elif latest is None or latest.status != RunStatus.EXPIRED:
                    raise RuntimeError("This automation run is no longer awaiting approval.")
            else:
                run = claimed
                automation = self._required(run.automation_id)
                with self._state_lock:
                    self._current_run_id = run.id
                try:
                    if run.dispatch_kind == "direct":
                        result = self.runner.direct_action(automation.task_goal, automation.privacy, approved=approved)
                        outcome = self.runner._direct_outcome(result) if result is not None else _DispatchOutcome(
                            RunStatus.FAILED, "Task did not complete.", "APPROVAL_LOST", dispatch_kind="direct")
                        if not approved:
                            outcome = replace(outcome, status=RunStatus.CANCELLED, summary="Task cancelled.")
                    elif run.task_id:
                        task_result = self.worker_executor.approve(run.task_id, "YES" if approved else "NO")
                        state = task_result.status if isinstance(task_result.status, TaskStatus) else TaskStatus(str(task_result.status))
                        current_task = getattr(self.worker_executor, "current_task", None)
                        count = int(getattr(current_task, "tool_calls", run.tool_count))
                        if state == TaskStatus.WAITING_APPROVAL:
                            outcome = _DispatchOutcome(RunStatus.WAITING_APPROVAL, "Waiting for explicit approval.",
                                                       "REQUIRED", task_result.task_id, count)
                        elif state == TaskStatus.SUCCEEDED:
                            outcome = _DispatchOutcome(RunStatus.COMPLETED, "Task completed.",
                                                       "APPROVED", task_result.task_id, count)
                        elif state == TaskStatus.CANCELLED:
                            outcome = _DispatchOutcome(RunStatus.CANCELLED, "Task cancelled.",
                                                       "DECLINED", task_result.task_id, count)
                        else:
                            outcome = _DispatchOutcome(RunStatus.FAILED, "Task did not complete.",
                                                       "APPROVED" if approved else "DECLINED", task_result.task_id, count)
                    else:
                        outcome = _DispatchOutcome(RunStatus.FAILED, "Task did not complete.", "APPROVAL_LOST")
                except Exception:
                    outcome = _DispatchOutcome(RunStatus.FAILED, "Task did not complete.", "APPROVAL_FAILED",
                                               run.task_id, run.tool_count, run.dispatch_kind)
                if self._is_cancelled(run.id):
                    outcome = replace(outcome, status=RunStatus.CANCELLED, summary="Task cancelled.",
                                      approval_state="CANCELLED")
                if outcome.status == RunStatus.WAITING_APPROVAL:
                    expiry = current + self.approval_timeout
                    self.store.update_run(
                        run.id, status=RunStatus.WAITING_APPROVAL.value, result_summary=outcome.summary,
                        approval_state="REQUIRED", task_id=outcome.task_id or run.task_id,
                        tool_count=outcome.tool_count, approval_expires_at=expiry,
                        duration_ms=max(0.0, (current - (run.started_at or current)).total_seconds() * 1000),
                    )
                    self.store.set_automation_state(automation.id, AutomationStatus.WAITING_APPROVAL, now=current)
                    self._emit("automation_waiting_approval", automation, run,
                               status=RunStatus.WAITING_APPROVAL.value, at=current)
                    waiting = True
                else:
                    outcome = replace(
                        outcome, approval_state="APPROVED" if approved else "DECLINED",
                        tool_count=run.tool_count + outcome.tool_count if run.dispatch_kind == "direct" else outcome.tool_count,
                    )
                    self._finish(run, outcome, current)
        finally:
            with self._state_lock:
                if self._current_run_id == run_id:
                    self._current_run_id = None
            self._dispatch_lock.release()
        latest = self.store.run(run_id)
        if not waiting and latest is not None and latest.status != RunStatus.EXPIRED:
            self._drain_queue(current)
        return self._result(run_id)

    def cancel_run(self, run_id: str, *, now: datetime | None = None) -> bool:
        current = _require_aware(now or self._now(), "now")
        run = self.store.run(run_id)
        if run is None or run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.EXPIRED}:
            return False
        if run.status == RunStatus.RUNNING:
            with self._state_lock:
                self._cancelled.add(run_id)
            if run.dispatch_kind == "planner":
                self.worker_executor.cancel(run.task_id)
            return True
        cancelled = self.store.cancel_pending(run_id, current)
        if cancelled is None:
            latest = self.store.run(run_id)
            if latest is None or latest.status != RunStatus.RUNNING:
                return False
            with self._state_lock:
                self._cancelled.add(run_id)
            if latest.dispatch_kind == "planner":
                self.worker_executor.cancel(latest.task_id)
            return True
        if cancelled.dispatch_kind == "planner" and cancelled.task_id:
            self.worker_executor.cancel(cancelled.task_id)
        self._restore_automation(cancelled, current, RunStatus.CANCELLED)
        self._drain_queue(current)
        return True

    def cancel_current(self) -> bool:
        run_id = self.current_run_id
        return self.cancel_run(run_id) if run_id else False

    def close(self) -> None:
        self.store.close()

    def _drain_queue(self, now: datetime | None = None) -> None:
        if not self._dispatch_lock.acquire(blocking=False):
            return
        try:
            for run in self.store.queued_runs(self.queue_limit):
                if self._executor_busy():
                    break
                automation = self.get(run.automation_id)
                if automation is None:
                    self.store.update_run(run.id, status=RunStatus.FAILED.value,
                                          finished_at=self._now(), result_summary="Automation unavailable.")
                    continue
                start = _require_aware(now, "now") if now is not None else self._now()
                self.store.update_run(run.id, status=RunStatus.RUNNING.value, started_at=start,
                                      result_summary="Running.")
                self.store.set_automation_state(automation.id, AutomationStatus.RUNNING, now=start,
                                                last_run_at=start)
                with self._state_lock:
                    self._current_run_id = run.id
                self._emit("automation_started", automation, run, status=RunStatus.RUNNING.value, at=start)
                try:
                    outcome = self.runner.execute(automation, lambda: self._is_cancelled(run.id))
                except Exception:
                    outcome = _DispatchOutcome(RunStatus.FAILED, "Task did not complete.")
                finish = self._now()
                if outcome.status == RunStatus.WAITING_APPROVAL:
                    expiry = finish + self.approval_timeout
                    self.store.update_run(run.id, status=RunStatus.WAITING_APPROVAL.value,
                                          result_summary=outcome.summary, approval_state="REQUIRED",
                                          task_id=outcome.task_id, duration_ms=max(0.0, (finish - start).total_seconds() * 1000),
                                          tool_count=outcome.tool_count, approval_expires_at=expiry,
                                          dispatch_kind=outcome.dispatch_kind)
                    self.store.set_automation_state(automation.id, AutomationStatus.WAITING_APPROVAL, now=finish)
                    self._emit("automation_waiting_approval", automation, run,
                               status=RunStatus.WAITING_APPROVAL.value, at=finish)
                    with self._state_lock:
                        self._current_run_id = None
                    break
                if self._is_cancelled(run.id):
                    outcome = replace(outcome, status=RunStatus.CANCELLED, summary="Task cancelled.", approval_state="CANCELLED")
                self._finish(run, outcome, finish, start=start)
                with self._state_lock:
                    self._current_run_id = None
        finally:
            self._dispatch_lock.release()

    def _finish(
        self, run: AutomationRun, outcome: _DispatchOutcome, now: datetime,
        *, start: datetime | None = None,
    ) -> None:
        item = self.get(run.automation_id)
        start = start or run.started_at or now
        duration = max(0.0, (now - start).total_seconds() * 1000)
        self.store.update_run(
            run.id, status=outcome.status.value, finished_at=now, result_summary=outcome.summary,
            approval_state=outcome.approval_state, duration_ms=duration, tool_count=outcome.tool_count,
            task_id=outcome.task_id or run.task_id, approval_expires_at=None,
            dispatch_kind=outcome.dispatch_kind,
        )
        self._restore_automation(run, now, outcome.status)
        if item is not None:
            event = "automation_completed" if outcome.status == RunStatus.COMPLETED else "automation_failed"
            self._emit(event, item, run, status=outcome.status.value, at=now, duration_ms=round(duration, 1))
        with self._state_lock:
            self._cancelled.discard(run.id)
            if self._current_run_id == run.id:
                self._current_run_id = None

    def _restore_automation(self, run: AutomationRun, now: datetime, outcome: RunStatus) -> None:
        item = self.get(run.automation_id)
        if item is None:
            return
        restore = AutomationStatus(run.restore_status)
        if not item.enabled:
            restore = AutomationStatus.DISABLED
        if run.restore_status == AutomationStatus.ACTIVE.value and item.trigger.type == TriggerType.ONE_TIME:
            restore = AutomationStatus.COMPLETED if outcome in {RunStatus.COMPLETED, RunStatus.EXPIRED} else AutomationStatus.FAILED
            if outcome in {RunStatus.COMPLETED, RunStatus.EXPIRED, RunStatus.FAILED, RunStatus.CANCELLED}:
                item = replace(item, enabled=False, next_run_at=None)
                self.store.save_automation(item)
        if item.max_runs is not None and self.store.run_count(item.id) >= item.max_runs:
            item = replace(item, enabled=False, next_run_at=None)
            self.store.save_automation(item)
            restore = AutomationStatus.COMPLETED
        self.store.set_automation_state(item.id, restore, now=now)

    def _expire_approvals(self, now: datetime) -> None:
        for run in self.store.expiring(now):
            self._expire_run(run, now)

    def _expire_run(self, run: AutomationRun, now: datetime) -> None:
        expired = self.store.expire_approval(run.id, now)
        if expired is None:
            return
        if run.dispatch_kind == "planner" and run.task_id:
            self.worker_executor.cancel(run.task_id)
        self._restore_automation(expired, now, RunStatus.EXPIRED)
        item = self.get(expired.automation_id)
        if item:
            self._emit("automation_failed", item, expired, status=RunStatus.EXPIRED.value, at=now)

    def _has_live_run(self, automation_id: str) -> bool:
        return any(run.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}
                   for run in self.store.history(automation_id, 1000))

    def _executor_busy(self) -> bool:
        current = getattr(self.worker_executor, "current_task", None)
        status = getattr(getattr(current, "status", None), "value", getattr(current, "status", None))
        return bool(current is not None and status not in {
            "SUCCEEDED", "FAILED", "BLOCKED", "PARTIAL", "CANCELLED", "DRY_RUN", "HANDOFF_READY",
        })

    def _is_cancelled(self, run_id: str) -> bool:
        with self._state_lock:
            return run_id in self._cancelled

    def _required(self, automation_id: str) -> Automation:
        item = self.get(automation_id)
        if item is None:
            raise LookupError("Automation not found.")
        return item

    def _result(self, run_id: str) -> AutomationResult:
        run = self.store.run(run_id)
        if run is None:
            raise LookupError("Automation run not found.")
        return AutomationResult(run.id, run.automation_id, run.status, run.result_summary, run.approval_state)

    def _now(self) -> datetime:
        return _require_aware(self.clock(), "clock")

    @staticmethod
    def _initial_next(trigger: AutomationTrigger, now: datetime) -> datetime | None:
        if trigger.type == TriggerType.ONE_TIME:
            return trigger.run_at if trigger.run_at >= now else now
        return AutomationScheduler.next_after(trigger, now)

    @staticmethod
    def _reanchor(trigger: AutomationTrigger, now: datetime) -> AutomationTrigger:
        return replace(trigger, anchor_at=now) if trigger.type == TriggerType.RECURRING_INTERVAL else trigger

    @staticmethod
    def _text(value: Any, field_name: str, limit: int, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be text.")
        value = value.strip()
        if (not value and not allow_empty) or len(value) > limit or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError(f"{field_name} must contain at most {limit} printable characters.")
        return value

    @staticmethod
    def _metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict) or contains_possible_secret(metadata):
            raise ValueError("metadata must be a secret-free JSON object.")
        try:
            encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("metadata must be a JSON object.") from error
        if len(encoded) > 8192:
            raise ValueError("metadata exceeds 8 KB.")
        return json.loads(encoded)

    def _emit(
        self, event: str, automation: Automation, run: AutomationRun | None = None,
        *, status: str | None = None, at: datetime | None = None, duration_ms: float | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        payload = {
            "event": event, "automation_id": automation.id,
            "run_id": run.id if run else None, "trigger_type": automation.trigger.type.value,
            "status": status or (run.status.value if run else automation.status.value),
            "timestamp": _dt_text(at or self._now()), "duration_ms": duration_ms,
        }
        try:
            self.event_sink(payload)
        except Exception:
            pass
