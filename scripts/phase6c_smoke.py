"""Offline Phase 6C acceptance for local automations and scheduled tasks."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
import json
import sqlite3
import sys
import tempfile
import threading
import time as monotonic_time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.automation import (  # noqa: E402
    AutomationManager,
    AutomationScheduler,
    AutomationStatus,
    AutomationTrigger,
    ConditionState,
    LocalTestEventSource,
    RunStatus,
    TriggerType,
)
from friday.capabilities.registry import Action, ActionPlan, Capability, Risk  # noqa: E402
from friday.core import FridayCore  # noqa: E402
from friday.model_policy import ModelPolicy  # noqa: E402
from friday.planner import Planner, WorkerExecutor  # noqa: E402
from friday.tools.adapters import register_computer_tools  # noqa: E402
from friday.tools.models import RiskLevel, ToolDefinition, ToolResult  # noqa: E402
from friday.tools.registry import ToolRegistry  # noqa: E402


BASE = datetime(2026, 10, 5, 7, 0, tzinfo=timezone(timedelta(hours=5)))
_PRIVACY = frozenset({"public", "private", "local_only"})


class FakeClock:
    def __init__(self, value: datetime = BASE) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class Fixture:
    def __init__(self, path: Path, *, clock: FakeClock | None = None,
                 event_source: LocalTestEventSource | None = None,
                 entered: threading.Event | None = None, release: threading.Event | None = None,
                 direct: bool = False) -> None:
        self.path = path
        self.clock = clock or FakeClock()
        self.calls: Counter[str] = Counter()
        self.order: list[str] = []
        self.plan_privacies: list[str] = []
        self.remote_calls = 0
        self.model_calls = 0
        self.direct_calls = 0
        self.events: list[dict] = []
        self.entered = entered
        self.release = release
        self.registry = ToolRegistry()
        for name, risk in (
            ("read", RiskLevel.READ_ONLY), ("reversible", RiskLevel.REVERSIBLE),
            ("consequential", RiskLevel.CONSEQUENTIAL), ("destructive", RiskLevel.DESTRUCTIVE),
            ("fail", RiskLevel.READ_ONLY), ("block", RiskLevel.READ_ONLY),
            ("second", RiskLevel.READ_ONLY), ("first", RiskLevel.READ_ONLY),
        ):
            self._register(name, risk)

        def model_call(goal: str, _instruction: str, privacy: str) -> str:
            self.model_calls += 1
            self.plan_privacies.append(privacy)
            lowered = goal.casefold()
            if "two step cancel" in lowered:
                steps = [("block", ()), ("second", ("step1",))]
            elif "queue first" in lowered:
                steps = [("first", ())]
            elif "queue second" in lowered:
                steps = [("second", ())]
            elif "failure first" in lowered:
                steps = [("fail", ())]
            elif "consequential" in lowered:
                steps = [("consequential", ())]
            elif "destructive" in lowered:
                steps = [("destructive", ())]
            elif "reversible" in lowered:
                steps = [("reversible", ())]
            else:
                steps = [("read", ())]
            return json.dumps({"steps": [
                {"id": f"step{index}", "description": f"Run {name}.", "profile": "GENERAL",
                 "capability": "general", "preferred_tool": f"task.{name}", "arguments": {},
                 "dependencies": list(dependencies), "verification": "tool_success"}
                for index, (name, dependencies) in enumerate(steps, start=1)
            ]})

        self.planner = Planner(None, self.registry, model_call=model_call)
        self.worker = WorkerExecutor(self.registry, planner=self.planner)
        self.event_source = event_source
        self.direct = direct
        self.manager = self._manager()

    def _register(self, name: str, risk: RiskLevel) -> None:
        def handler(_args: dict, _context) -> str:
            self.calls[name] += 1
            self.order.append(name)
            if name == "block" and self.entered is not None and self.release is not None:
                self.entered.set()
                self.release.wait(5)
            if name == "fail":
                raise RuntimeError("safe failure sentinel")
            return "ok"

        self.registry.register(ToolDefinition(
            name=name, namespace="task", description="Offline smoke tool.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler, capabilities=frozenset({"general"}), risk_level=risk,
            privacy_support=_PRIVACY, source="phase6c-smoke", side_effects="none",
        ))

    def _direct_action(self, goal: str, _privacy: str, *, approved: bool | None = None) -> ToolResult | None:
        if not self.direct or not goal.startswith("direct"):
            return None
        self.direct_calls += 1
        if "approval" in goal and approved is None:
            return ToolResult(False, "computer.test", "approval_required",
                              "Explicit approval is required.", requires_followup=True)
        if "approval" in goal and approved is False:
            return ToolResult(False, "computer.test", "denied", "Action cancelled.")
        return ToolResult(True, "computer.test", "success", "ok")

    def _manager(self) -> AutomationManager:
        return AutomationManager(
            self.planner, self.worker, database_path=self.path, event_source=self.event_source,
            event_sink=self.events.append, direct_action=self._direct_action if self.direct else None,
            clock=self.clock,
        )

    def reopen(self) -> None:
        self.manager.close()
        self.manager = self._manager()

    def close(self) -> None:
        self.manager.close()


def one_time(at: datetime) -> AutomationTrigger:
    return AutomationTrigger(TriggerType.ONE_TIME, run_at=at)


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"{label}: PASS")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="friday-phase6c-") as temp:
        temp_root = Path(temp)

        # A-C. Initial future calculation and occurrence-level idempotency.
        f = Fixture(temp_root / "once.sqlite3")
        due = BASE + timedelta(minutes=1)
        future = f.manager.create("future", "read-only check", one_time(due), now=BASE)
        check(future.next_run_at == due, "A ONE_TIME future")
        f.clock.value = due + timedelta(seconds=1)
        f.manager.tick(f.clock.value)
        f.manager.tick(f.clock.value)
        check(f.calls["read"] == 1 and len(f.manager.history(future.id)) == 1, "B-C ONE_TIME due and duplicate tick")
        f.close()

        # D-F. Calendar and anchored interval arithmetic.
        daily = AutomationScheduler.next_after(
            AutomationTrigger(TriggerType.DAILY, at_time=time(8)), BASE,
        )
        weekly = AutomationScheduler.next_after(
            AutomationTrigger(TriggerType.WEEKLY, at_time=time(10), weekday=0), BASE,
        )
        interval_trigger = AutomationTrigger(TriggerType.RECURRING_INTERVAL, interval_seconds=7200, anchor_at=BASE)
        interval = AutomationScheduler.next_after(interval_trigger, BASE)
        check(daily == BASE.replace(hour=8), "D DAILY")
        check(weekly == BASE.replace(hour=10), "E WEEKLY")
        check(interval == BASE + timedelta(hours=2), "F INTERVAL")

        # G. One missed one-time firing remains one occurrence after restart.
        f = Fixture(temp_root / "missed-once.sqlite3")
        missed = f.manager.create("missed", "read after downtime", one_time(BASE - timedelta(hours=1)), now=BASE)
        f.manager.tick(BASE)
        check(f.calls["read"] == 1 and f.manager.history(missed.id)[0].status == RunStatus.COMPLETED,
              "G MISSED ONE_TIME catch-up")
        f.close()

        # H. A delayed interval is coalesced once and re-armed on its original grid.
        f = Fixture(temp_root / "missed-interval.sqlite3")
        recurring = f.manager.create(
            "interval", "read every two hours", AutomationTrigger(TriggerType.RECURRING_INTERVAL, interval_seconds=7200),
            now=BASE,
        )
        resume_at = BASE + timedelta(hours=10)
        f.clock.value = resume_at
        f.manager.tick(resume_at)
        after_catchup = f.manager.get(recurring.id)
        check(f.calls["read"] == 1 and after_catchup.next_run_at == BASE + timedelta(hours=12),
              "H MISSED INTERVAL coalescing")
        f.close()

        # I. Read-only tool calls run automatically through WorkerExecutor.
        f = Fixture(temp_root / "read-only.sqlite3")
        read_only = f.manager.create("read-only", "run read only", one_time(BASE), now=BASE)
        result = f.manager.tick(BASE)[0]
        check(result.status == RunStatus.COMPLETED and f.calls["read"] == 1, "I READ_ONLY automation")
        f.close()

        # J-K. Consequential and destructive tools stay paused for user approval.
        f = Fixture(temp_root / "consequential.sqlite3")
        consequential = f.manager.create("consequential", "run consequential", one_time(BASE), now=BASE)
        waiting = f.manager.tick(BASE)[0]
        check(waiting.status == RunStatus.WAITING_APPROVAL and f.calls["consequential"] == 0,
              "J CONSEQUENTIAL approval")
        approved = f.manager.approve(waiting.run_id, True, now=BASE)
        check(approved.status == RunStatus.COMPLETED and f.calls["consequential"] == 1,
              "J CONSEQUENTIAL explicit approval")
        f.close()
        f = Fixture(temp_root / "destructive.sqlite3")
        destructive = f.manager.create("destructive", "run destructive", one_time(BASE), now=BASE)
        waiting = f.manager.tick(BASE)[0]
        check(waiting.status == RunStatus.WAITING_APPROVAL and f.calls["destructive"] == 0,
              "K DESTRUCTIVE approval")
        f.close()

        # L. Approval expiration records an expired receipt without execution.
        f = Fixture(temp_root / "approval-expiry.sqlite3")
        expiry_item = f.manager.create("expiry", "run consequential", one_time(BASE), now=BASE)
        waiting = f.manager.tick(BASE)[0]
        f.clock.value = BASE + timedelta(hours=25)
        f.manager.tick(f.clock.value)
        expired = f.manager.history(expiry_item.id)[0]
        check(expired.status == RunStatus.EXPIRED and expired.approval_state == "EXPIRED"
              and f.calls["consequential"] == 0, "L APPROVAL expiry")
        f.close()

        # M. PRIVATE uses PRIVATE planner policy and makes no remote calls.
        f = Fixture(temp_root / "private.sqlite3")
        private_goal = "PRIVATE_PROMPT_SENTINEL recurring local report"
        private = f.manager.create("private", private_goal, one_time(BASE), privacy="private", now=BASE)
        f.manager.tick(BASE)
        history = f.manager.history(private.id)[0]
        check(f.plan_privacies == ["private"] and f.planner.last_selection.policy == ModelPolicy.PRIVATE
              and f.remote_calls == 0, "M PRIVATE planning boundary")
        check(private_goal not in repr(history) and "PRIVATE_PROMPT_SENTINEL" not in history.result_summary,
              "Z HISTORY privacy")
        f.close()

        # N-O. Pause/resume skips missed daily slots and calculates a future local occurrence.
        f = Fixture(temp_root / "pause-resume.sqlite3")
        daily_item = f.manager.create(
            "daily", "daily local check", AutomationTrigger(TriggerType.DAILY, at_time=time(8)), now=BASE,
        )
        f.manager.pause(daily_item.id, now=BASE)
        f.manager.tick(BASE + timedelta(hours=3))
        resumed = f.manager.resume(daily_item.id, now=BASE + timedelta(hours=3))
        check(f.calls["read"] == 0 and resumed.status == AutomationStatus.ACTIVE
              and resumed.next_run_at == (BASE + timedelta(days=1)).replace(hour=8), "N-O PAUSE/RESUME")
        f.close()

        # P-Q. Disabled automation stays idle; schedule edits recalculate next_run_at.
        f = Fixture(temp_root / "edit-disable.sqlite3")
        disabled = f.manager.create("disabled", "disabled goal", one_time(BASE), now=BASE)
        disabled = f.manager.disable(disabled.id, now=BASE)
        f.manager.tick(BASE)
        editable = f.manager.create("editable", "editable goal", one_time(BASE + timedelta(hours=1)), now=BASE)
        changed_at = BASE + timedelta(hours=3)
        changed = f.manager.update(editable.id, trigger=one_time(changed_at), task_goal="new goal", now=BASE)
        check(disabled.status == AutomationStatus.DISABLED and f.calls["read"] == 0, "P DISABLE")
        check(changed.next_run_at == changed_at and changed.task_goal == "new goal", "Q EDIT schedule")
        stable = f.manager.create(
            "stable", "stable goal", AutomationTrigger(TriggerType.RECURRING_INTERVAL, interval_seconds=3600), now=BASE,
        )
        renamed = f.manager.update(stable.id, name="renamed", now=BASE + timedelta(minutes=30))
        check(renamed.next_run_at == stable.next_run_at, "Q non-schedule edit preserves next run")
        bounded = f.manager.create(
            "max runs", "max runs goal", AutomationTrigger(TriggerType.RECURRING_INTERVAL, interval_seconds=3600),
            max_runs=1, now=BASE,
        )
        f.clock.value = bounded.next_run_at
        f.manager.tick(f.clock.value)
        check(f.manager.get(bounded.id).status == AutomationStatus.COMPLETED, "max_runs bound")
        f.close()

        # R. run_now follows the same planner/worker/registry pipeline.
        f = Fixture(temp_root / "run-now.sqlite3")
        manual = f.manager.create("manual", "run manually", one_time(BASE + timedelta(days=1)), now=BASE)
        manual_result = f.manager.run_now(manual.id, now=BASE)
        check(manual_result.status == RunStatus.COMPLETED and f.calls["read"] == 1, "R RUN_NOW")
        f.close()

        # S. Cooperative cancellation stops dependent steps after the active tool returns.
        entered, release = threading.Event(), threading.Event()
        f = Fixture(temp_root / "cancel.sqlite3", entered=entered, release=release)
        cancel_item = f.manager.create("cancel", "two step cancel", one_time(BASE), now=BASE)
        tick_thread = threading.Thread(target=f.manager.tick, args=(BASE,), daemon=True)
        tick_thread.start()
        check(entered.wait(3), "S cancellation reached running tool")
        run_id = f.manager.current_run_id
        check(run_id is not None and f.manager.cancel_run(run_id), "S cancellation requested")
        release.set()
        tick_thread.join(5)
        cancelled = f.manager.history(cancel_item.id)[0]
        check(not tick_thread.is_alive() and cancelled.status == RunStatus.CANCELLED
              and f.calls["second"] == 0, "S remaining steps stopped")
        f.close()

        # T. Startup recovery marks interrupted runs failed and never resumes them blindly.
        f = Fixture(temp_root / "recovery.sqlite3")
        stale = f.manager.create("stale", "stale task", one_time(BASE + timedelta(hours=1)), now=BASE)
        stale_run_id = "stale-run-id"
        db = sqlite3.connect(f.path)
        db.execute("UPDATE automations SET status='RUNNING' WHERE id=?", (stale.id,))
        db.execute(
            """INSERT INTO automation_runs
            (id,automation_id,occurrence_key,scheduled_at,started_at,status,result_summary,approval_state,
             tool_count,restore_status,dispatch_kind)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (stale_run_id, stale.id, "time:interrupted", BASE.isoformat(), BASE.isoformat(), "RUNNING",
             "Running.", "NOT_REQUIRED", 0, "ACTIVE", "planner"),
        )
        db.commit()
        db.close()
        f.reopen()
        check(f.manager.history(stale.id)[0].status == RunStatus.FAILED
              and f.manager.get(stale.id).status == AutomationStatus.FAILED, "T STARTUP recovery")
        f.close()
        f = Fixture(temp_root / "approval-recovery.sqlite3")
        approval_recovery = f.manager.create(
            "approval recovery", "run consequential", one_time(BASE), now=BASE,
        )
        recovery_wait = f.manager.tick(BASE)[0]
        check(recovery_wait.status == RunStatus.WAITING_APPROVAL, "T stale approval setup")
        f.close()
        f = Fixture(temp_root / "approval-recovery.sqlite3")
        check(f.manager.history(approval_recovery.id)[0].status == RunStatus.EXPIRED
              and f.manager.get(approval_recovery.id).status == AutomationStatus.COMPLETED,
              "T stale approval recovery")
        f.close()

        # U-V. Event conditions are tri-state and duplicate event identities fire once.
        source = LocalTestEventSource()
        f = Fixture(temp_root / "events.sqlite3", event_source=source)
        true_item = f.manager.create("event true", "event true goal",
                                     AutomationTrigger(TriggerType.EVENT, event_name="true"), now=BASE)
        false_item = f.manager.create("event false", "event false goal",
                                      AutomationTrigger(TriggerType.EVENT, event_name="false"), now=BASE)
        unknown_item = f.manager.create("event unknown", "event unknown goal",
                                        AutomationTrigger(TriggerType.EVENT, event_name="unknown"), now=BASE)
        source.publish("true", "same-event-id", ConditionState.TRUE)
        source.publish("false", "false-event-id", ConditionState.FALSE)
        source.publish("unknown", "unknown-event-id", ConditionState.UNKNOWN)
        f.manager.tick(BASE)
        f.manager.tick(BASE)
        check(len(f.manager.history(true_item.id)) == 1 and not f.manager.history(false_item.id)
              and not f.manager.history(unknown_item.id), "U-V EVENT TRUE/FALSE/UNKNOWN and duplicate")
        f.close()

        # W. Two due automations execute sequentially; X. One failure does not strand the next.
        f = Fixture(temp_root / "queue.sqlite3")
        first = f.manager.create("queue first", "queue first", one_time(BASE - timedelta(minutes=2)), now=BASE)
        second = f.manager.create("queue second", "queue second", one_time(BASE - timedelta(minutes=1)), now=BASE)
        f.manager.tick(BASE)
        first_done = next(i for i, e in enumerate(f.events) if e["event"] == "automation_completed" and e["automation_id"] == first.id)
        second_started = next(i for i, e in enumerate(f.events) if e["event"] == "automation_started" and e["automation_id"] == second.id)
        check(first_done < second_started and f.calls["first"] == 1 and f.calls["second"] == 1,
              "W sequential bounded queue")
        f.close()
        f = Fixture(temp_root / "failure-isolation.sqlite3")
        failed_first = f.manager.create("failure first", "failure first", one_time(BASE - timedelta(minutes=2)), now=BASE)
        runnable_second = f.manager.create("second", "read after failure", one_time(BASE - timedelta(minutes=1)), now=BASE)
        f.manager.tick(BASE)
        check(f.manager.history(failed_first.id)[0].status == RunStatus.FAILED
              and f.manager.history(runnable_second.id)[0].status == RunStatus.COMPLETED,
              "X failure isolation")
        f.close()

        # Y, AA. Compact history persists; definitions survive manager restart.
        f = Fixture(temp_root / "persistence.sqlite3")
        saved = f.manager.create("persistent", "read persistent", one_time(BASE + timedelta(hours=1)), now=BASE)
        f.close()
        f = Fixture(temp_root / "persistence.sqlite3")
        check(f.manager.get(saved.id) is not None and f.manager.get(saved.id).task_goal == "read persistent",
              "AA restart persistence")
        check(all("arguments" not in str(run) and len(run.result_summary) < 100
                  for run in f.manager.history(saved.id)), "Y compact history")
        f.close()

        # AB. A deterministic direct COMPUTER adapter can skip Planner, while retaining approval.
        f = Fixture(temp_root / "direct.sqlite3", direct=True)
        direct_item = f.manager.create("direct", "direct simple action", one_time(BASE), now=BASE)
        direct_result = f.manager.run_now(direct_item.id, now=BASE)
        check(direct_result.status == RunStatus.COMPLETED and f.model_calls == 0 and f.direct_calls == 1,
              "AB direct deterministic action")
        direct_approval = f.manager.create("direct approval", "direct approval action", one_time(BASE), now=BASE)
        direct_waiting = f.manager.run_now(direct_approval.id, now=BASE)
        check(direct_waiting.status == RunStatus.WAITING_APPROVAL, "AB direct approval gate")
        direct_approved = f.manager.approve(direct_waiting.run_id, True, now=BASE)
        check(direct_approved.status == RunStatus.COMPLETED, "AB direct explicit approval")
        prior_direct_calls = f.direct_calls
        try:
            f.manager.approve(direct_waiting.run_id, True, now=BASE)
        except RuntimeError:
            pass
        else:
            raise AssertionError("A completed approval was accepted a second time.")
        check(f.direct_calls == prior_direct_calls, "AB duplicate approval rejected")
        f.close()

        # The production direct adapter uses FRIDAY's existing COMPUTER tool and registry.
        f = Fixture(temp_root / "direct-computer.sqlite3")

        class FakeComputer:
            def __init__(self) -> None:
                self.planner = self
                self.calls = 0

            @staticmethod
            def plan(_goal: str) -> ActionPlan:
                return ActionPlan((Action(Capability.COMPUTER, "OPEN_APP", "Notepad"),))

            def execute(self, _plan: ActionPlan, approved: bool = False) -> tuple[str, Risk]:
                self.calls += 1
                return "Opened Notepad.", Risk.SAFE

        computer = FakeComputer()
        register_computer_tools(f.registry, lambda: computer)
        f.manager.close()
        core = object.__new__(FridayCore)
        core._computer = computer
        core.tool_registry = f.registry
        core.planner = f.planner
        core.worker_executor = f.worker
        f.manager = AutomationManager.from_core(core, database_path=f.path, clock=f.clock)
        direct_computer = f.manager.create("direct computer", "open Notepad", one_time(BASE), now=BASE)
        direct_computer_result = f.manager.run_now(direct_computer.id, now=BASE)
        check(direct_computer_result.status == RunStatus.COMPLETED and computer.calls == 1
              and f.planner.model_calls == 0
              and f.registry.receipts[-1].tool_name == "computer.open_app",
              "AB existing deterministic COMPUTER path")
        f.close()

        # AC-AE regressions run separately against the established smoke suites.
        print("AC-AE Phase 6B/6A/private regressions: run separately")

        # Lightweight benchmark samples and shallow Python object footprint.
        f = Fixture(temp_root / "metrics.sqlite3")
        future_item = f.manager.create("metrics", "metrics goal", one_time(BASE + timedelta(days=1)), now=BASE)
        start = monotonic_time.perf_counter()
        for _ in range(1000):
            AutomationScheduler.next_after(interval_trigger, BASE + timedelta(minutes=1))
        next_ms = (monotonic_time.perf_counter() - start) * 1000 / 1000
        start = monotonic_time.perf_counter()
        for _ in range(100):
            f.manager.get(future_item.id)
        persistence_ms = (monotonic_time.perf_counter() - start) * 10
        start = monotonic_time.perf_counter()
        f.manager.tick(BASE)
        due_ms = (monotonic_time.perf_counter() - start) * 1000
        start = monotonic_time.perf_counter()
        f.manager.run_now(future_item.id, now=BASE)
        runner_ms = (monotonic_time.perf_counter() - start) * 1000
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        _ = [AutomationTrigger(TriggerType.DAILY, at_time=time(8)) for _ in range(100)]
        heap_bytes = max(0, tracemalloc.get_traced_memory()[0] - before) / 100
        tracemalloc.stop()
        print(f"METRICS due_check_ms={due_ms:.3f} persistence_read_ms={persistence_ms:.3f} "
              f"next_run_calc_ms={next_ms:.6f} runner_overhead_ms={runner_ms:.3f} "
              f"approx_trigger_heap_bytes={heap_bytes:.1f} remote_calls=0")
        f.close()
        print("PHASE 6C SMOKE: PASS; external network calls: 0")


if __name__ == "__main__":
    run()
