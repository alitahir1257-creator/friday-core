"""Sequential, cancellable workers mediated by FRIDAY's ToolRegistry."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from collections.abc import Callable
import json
import math
import threading
from typing import Any

from friday.planner.models import (
    MAX_PLAN_REPAIRS,
    MAX_STEP_RETRIES,
    MAX_TOOL_CALLS,
    PlanStep,
    StepResult,
    StepStatus,
    TaskApprovalRequest,
    TaskEvent,
    TaskPlan,
    TaskResult,
    TaskRun,
    TaskStatus,
    ValidatedPlan,
    WorkerProfile,
)
from friday.planner.validator import PlanValidationError, PlanValidator
from friday.tools.approvals import effective_risk
from friday.tools.models import (
    ApprovalDecision,
    RiskLevel,
    ToolInvocationContext,
    ToolResult,
)
from friday.tools.registry import ToolRegistry


ApprovalCallback = Callable[[TaskApprovalRequest], str]
_TRANSIENT_ERRORS = {"TIMEOUTERROR", "CONNECTIONERROR", "OSERROR", "URLERROR", "MCPCLIENTERROR"}
_UNAVAILABLE_ERRORS = {"TOOL_NOT_FOUND", "TOOL_DISABLED", "MCPCLIENTERROR"}
_TERMINAL = {
    TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED,
    TaskStatus.PARTIAL, TaskStatus.CANCELLED, TaskStatus.DRY_RUN, TaskStatus.HANDOFF_READY,
}


class WorkerExecutor:
    def __init__(
        self,
        tool_registry: ToolRegistry,
        *,
        validator: PlanValidator | None = None,
        planner: Any = None,
        event_sink: Callable[[TaskEvent], None] | None = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.validator = validator or PlanValidator(tool_registry)
        self.planner = planner
        self.event_sink = event_sink
        self._runs: dict[str, TaskRun] = {}
        self._validated: dict[str, ValidatedPlan] = {}
        self._pending_step: dict[str, PlanStep] = {}
        self._summaries: dict[str, str] = {}
        self._lock = threading.RLock()
        self.current_task: TaskRun | None = None

    def execute(
        self,
        plan: TaskPlan,
        *,
        dry_run: bool = False,
        approval_callback: ApprovalCallback | None = None,
    ) -> TaskResult:
        try:
            task_plan = deepcopy(plan)
        except Exception:
            raise PlanValidationError("INVALID_PLAN", "The task plan could not be safely copied.") from None
        validated = self.validator.validate(task_plan)
        with self._lock:
            if self.current_task is not None and self.current_task.status not in _TERMINAL:
                raise RuntimeError("FRIDAY is already handling a task.")
            run = TaskRun(plan=validated.plan, status=TaskStatus.PLANNED)
            self._runs[run.id] = run
            self._validated[run.id] = validated
            self._summaries[run.id] = "Task planned."
            self.current_task = run
            self._prune_runs()
        self._emit(run, "task_planned", status=run.status.value)
        if dry_run:
            self._dry_run(run, validated)
            return self._result(run)

        handoff = plan.metadata.get("handoff") if isinstance(plan.metadata, dict) else None
        if isinstance(handoff, dict) and handoff.get("destination") == "codex":
            run.handoff = {**handoff, "goal": plan.goal}
            run.status = TaskStatus.HANDOFF_READY
            self._set_step_status(run, plan.steps[0].id, StepStatus.SKIPPED)
            self._summaries[run.id] = "Repository work is ready for the existing Codex handoff; FRIDAY did not launch it."
            self._emit(run, "task_completed", status=run.status.value)
            return self._result(run)

        self._advance(run, approval_callback)
        return self._result(run)

    def approve(self, task_id: str, answer: str) -> TaskResult:
        run = self._get_run(task_id)
        if run.status != TaskStatus.WAITING_APPROVAL or run.pending_approval is None:
            raise RuntimeError("This task is not waiting for approval.")
        if run.cancelled():
            self._cancel_run(run)
            return self._result(run)
        step = self._pending_step[run.id]
        accepted = isinstance(answer, str) and answer.strip().upper() == "YES"
        self._answer_approval(run, step, accepted)
        if accepted and run.status == TaskStatus.RUNNING:
            self._advance(run, None)
        return self._result(run)

    def skip(self, task_id: str) -> TaskResult:
        run = self._get_run(task_id)
        if run.status != TaskStatus.WAITING_APPROVAL or run.pending_approval is None:
            raise RuntimeError("This task has no pending step to skip.")
        step = self._pending_step.pop(run.id)
        run.pending_approval = None
        run.approval_decisions[step.id] = "SKIP"
        self._record_without_tool(run, step, StepStatus.SKIPPED, "Step skipped by the user.")
        self._skip_remaining(run)
        run.status = TaskStatus.PARTIAL if any(r.status == StepStatus.SUCCEEDED for r in run.step_results) else TaskStatus.CANCELLED
        self._summaries[run.id] = "The approval step was skipped; dependent work did not run."
        self._emit(run, "task_completed", status=run.status.value)
        return self._result(run)

    def cancel(self, task_id: str | None = None) -> bool:
        run = self.current_task if task_id is None else self._get_run(task_id)
        if run is None or run.status in _TERMINAL:
            return False
        run.cancel()
        if run.status == TaskStatus.WAITING_APPROVAL:
            self._cancel_run(run)
        return True

    def current_state(self) -> dict[str, Any] | None:
        return self.current_task.snapshot() if self.current_task is not None else None

    def _advance(self, run: TaskRun, approval_callback: ApprovalCallback | None) -> None:
        if run.status == TaskStatus.WAITING_APPROVAL:
            return
        run.status = TaskStatus.RUNNING
        while True:
            if run.cancelled():
                self._cancel_run(run)
                return
            statuses = {step.id: step.status for step in run.plan.steps}
            pending = [step for step in run.plan.steps if step.status in {StepStatus.PENDING, StepStatus.READY}]
            if not pending:
                run.status = TaskStatus.SUCCEEDED
                run.current_step_id = None
                self._summaries[run.id] = "Task completed."
                self._emit(run, "task_completed", status=run.status.value)
                return
            ready = next(
                (step for step in pending if all(statuses.get(dep) == StepStatus.SUCCEEDED for dep in step.dependencies)),
                None,
            )
            if ready is None:
                self._skip_remaining(run)
                run.status = TaskStatus.BLOCKED
                self._summaries[run.id] = "Task stopped because a required dependency did not complete."
                self._emit(run, "task_completed", status=run.status.value)
                return

            run.current_step_id = ready.id
            self._set_step_status(run, ready.id, StepStatus.RUNNING)
            tool = self._validated[run.id].tools.get(ready.id)
            if tool is None:
                self._block_step(run, ready, "No enabled tool supports this capability.")
                return
            current_tool = self.tool_registry.get(tool.canonical_name)
            if current_tool is None or not current_tool.enabled:
                result = self._registry_call(run, ready, approval=None)
                if result is None:
                    self._block_step(run, ready, "The task exceeded its tool-call limit.")
                    return
                self._record_tool_result(run, ready, tool, result, StepStatus.FAILED, False, 1)
                if self._try_repair(run, ready, result.error_code or "TOOL_UNAVAILABLE"):
                    continue
                self._fail_step(run, ready, result, blocked=True)
                return

            risk = effective_risk(
                current_tool,
                ready.arguments,
                ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy),
            )
            self._emit(run, "step_started", step=ready, tool=current_tool.canonical_name, risk=risk.value, status=StepStatus.RUNNING.value)
            decision = self.tool_registry.approval_policy.decide(
                current_tool,
                ready.arguments,
                ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy),
                risk,
            )
            if decision == ApprovalDecision.REQUIRE_CONFIRMATION:
                request = TaskApprovalRequest(
                    action_summary=ready.description[:300],
                    tool=current_tool.canonical_name,
                    risk=risk.value,
                    expected_side_effect=current_tool.side_effects,
                )
                run.status = TaskStatus.WAITING_APPROVAL
                run.pending_approval = request
                self._pending_step[run.id] = ready
                self._set_step_status(run, ready.id, StepStatus.WAITING_APPROVAL)
                self._emit(run, "step_waiting_approval", step=ready, tool=current_tool.canonical_name, risk=risk.value, status=run.status.value)
                if approval_callback is None:
                    result = self._registry_call(run, ready, approval=None)
                    if result is not None:
                        self._record_tool_result(run, ready, current_tool, result, StepStatus.WAITING_APPROVAL, None, 1)
                    self._summaries[run.id] = "Task is waiting for explicit approval."
                    return
                try:
                    answer = approval_callback(request)
                except Exception:
                    answer = "NO"
                accepted = isinstance(answer, str) and answer.strip().upper() == "YES"
                self._answer_approval(run, ready, accepted)
                if not accepted or run.status != TaskStatus.RUNNING:
                    return
                continue
            if decision == ApprovalDecision.DENY:
                result = self._registry_call(run, ready, approval=None)
                if result is None:
                    self._block_step(run, ready, "The task exceeded its tool-call limit.")
                    return
                self._record_tool_result(run, ready, current_tool, result, StepStatus.BLOCKED, False, 1)
                self._fail_step(run, ready, result, blocked=True)
                return

            self._run_approved_or_allowed(run, ready, current_tool, risk, approval=None)
            if run.status != TaskStatus.RUNNING:
                return

    def _answer_approval(self, run: TaskRun, step: PlanStep, accepted: bool) -> None:
        tool = self.tool_registry.get(step.preferred_tool or "")
        run.pending_approval = None
        self._pending_step.pop(run.id, None)
        run.approval_decisions[step.id] = "YES" if accepted else "NO"
        if not accepted:
            result = self._registry_call(run, step, approval=False)
            if result is not None and tool is not None:
                self._record_tool_result(run, step, tool, result, StepStatus.CANCELLED, False, 1)
            self._set_step_status(run, step.id, StepStatus.CANCELLED)
            self._skip_remaining(run)
            run.status = TaskStatus.CANCELLED
            self.current_task = run
            self._summaries[run.id] = "The approval request was declined."
            self._emit(run, "step_failed", step=step, tool=step.preferred_tool, status=StepStatus.CANCELLED.value)
            self._emit(run, "task_cancelled", status=run.status.value)
            return
        if run.cancelled():
            self._cancel_run(run)
            return
        run.explicit_approvals.add(step.id)
        run.status = TaskStatus.RUNNING
        self._set_step_status(run, step.id, StepStatus.RUNNING)
        if tool is None:
            self._block_step(run, step, "The approved tool is no longer available.")
            return
        risk = effective_risk(
            tool, step.arguments,
            ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy),
        )
        self._run_approved_or_allowed(run, step, tool, risk, approval=True)

    def _run_approved_or_allowed(
        self,
        run: TaskRun,
        step: PlanStep,
        tool: Any,
        risk: RiskLevel,
        *,
        approval: bool | None,
    ) -> None:
        attempt = 1
        while True:
            if run.cancelled():
                self._cancel_run(run)
                return
            result = self._registry_call(run, step, approval=approval)
            if result is None:
                self._block_step(run, step, "The task exceeded its tool-call limit.")
                return
            if result.requires_followup:
                request = TaskApprovalRequest(
                    step.description[:300], tool.canonical_name, risk.value, tool.side_effects,
                )
                run.pending_approval = request
                self._pending_step[run.id] = step
                run.status = TaskStatus.WAITING_APPROVAL
                self._set_step_status(run, step.id, StepStatus.WAITING_APPROVAL)
                self._record_tool_result(run, step, tool, result, StepStatus.WAITING_APPROVAL, None, attempt)
                self._emit(run, "step_waiting_approval", step=step, tool=tool.canonical_name, risk=risk.value, status=run.status.value)
                self._summaries[run.id] = "Task is waiting for explicit approval."
                return

            verification = self._verify(step, result)
            successful = result.success and verification
            final_status = StepStatus.SUCCEEDED if successful else StepStatus.FAILED
            self._record_tool_result(run, step, tool, result, final_status, verification, attempt, risk)
            if successful:
                self._set_step_status(run, step.id, StepStatus.SUCCEEDED)
                run.current_step_id = None
                self._emit(run, "step_completed", step=step, tool=tool.canonical_name, risk=risk.value, status=StepStatus.SUCCEEDED.value, duration_ms=result.duration_ms)
                return

            transient = result.error_code in _TRANSIENT_ERRORS
            safe_retry = risk in {RiskLevel.READ_ONLY, RiskLevel.REVERSIBLE} and tool.side_effects == "none"
            if (
                attempt <= MAX_STEP_RETRIES
                and transient
                and safe_retry
                and run.tool_calls < MAX_TOOL_CALLS
                and not run.cancelled()
            ):
                attempt += 1
                continue
            self._set_step_status(run, step.id, StepStatus.FAILED)
            self._emit(run, "step_failed", step=step, tool=tool.canonical_name, risk=risk.value, status=StepStatus.FAILED.value, duration_ms=result.duration_ms)
            if self._try_repair(run, step, result.error_code or "VERIFICATION_FAILED", risk=risk, side_effects=tool.side_effects):
                return
            self._fail_step(run, step, result)
            return

    def _registry_call(self, run: TaskRun, step: PlanStep, *, approval: bool | None) -> ToolResult | None:
        if run.tool_calls >= MAX_TOOL_CALLS:
            return None
        if run.cancelled():
            self._cancel_run(run)
            return None
        run.tool_calls += 1
        kwargs: dict[str, Any] = {
            "caller": "friday_worker",
            "privacy": run.plan.privacy,
            "action_metadata": {"task_id": run.id, "step_id": step.id, "capability": step.capability},
        }
        if approval is not None:
            kwargs["approval_callback"] = lambda _request: approval
        return self.tool_registry.execute(step.preferred_tool or "", step.arguments, **kwargs)

    def _dry_run(self, run: TaskRun, validated: ValidatedPlan) -> None:
        for step in run.plan.steps:
            tool = validated.tools.get(step.id)
            if tool is None or not tool.enabled:
                self._set_step_status(run, step.id, StepStatus.BLOCKED)
                continue
            risk = effective_risk(
                tool, step.arguments,
                ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy),
            )
            decision = self.tool_registry.approval_policy.decide(
                tool, step.arguments,
                ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy), risk,
            )
            if decision == ApprovalDecision.REQUIRE_CONFIRMATION:
                run.dry_run_approval_requests.append(TaskApprovalRequest(
                    step.description[:300], tool.canonical_name, risk.value, tool.side_effects,
                ))
            elif decision == ApprovalDecision.DENY:
                self._set_step_status(run, step.id, StepStatus.BLOCKED)
            else:
                self._set_step_status(run, step.id, StepStatus.READY)
        run.status = TaskStatus.DRY_RUN
        self._summaries[run.id] = "Dry run complete; no tools were invoked."
        self._emit(run, "task_completed", status=run.status.value)

    def _try_repair(
        self,
        run: TaskRun,
        step: PlanStep,
        failure_code: str,
        *,
        risk: RiskLevel | None = None,
        side_effects: str | None = None,
    ) -> bool:
        if (
            self.planner is None
            or not callable(getattr(self.planner, "repair", None))
            or run.repair_attempts >= MAX_PLAN_REPAIRS
            or failure_code not in _UNAVAILABLE_ERRORS
        ):
            return False
        validated = self._validated[run.id]
        failed_risk = risk or validated.risks.get(step.id)
        failed_tool = validated.tools.get(step.id)
        if failed_risk != RiskLevel.READ_ONLY or (side_effects or getattr(failed_tool, "side_effects", "unknown")) != "none":
            return False
        if any(
            item.status == StepStatus.SUCCEEDED and item.risk in {RiskLevel.CONSEQUENTIAL.value, RiskLevel.DESTRUCTIVE.value}
            for item in run.step_results
        ):
            return False
        run.repair_attempts += 1
        try:
            candidate = self.planner.repair(run.plan, step_id=step.id, failure_code=failure_code)
            repaired = self.validator.validate(candidate)
            if not self._is_narrow_repair(run.plan, repaired, step.id):
                return False
            replacement = repaired.tools.get(step.id)
            replacement_risk = repaired.risks.get(step.id)
            if replacement is None or not replacement.enabled or replacement_risk != RiskLevel.READ_ONLY or replacement.side_effects != "none":
                return False
        except (PlanValidationError, ValueError, RuntimeError):
            return False

        new_steps = []
        for old, new in zip(run.plan.steps, repaired.plan.steps):
            status = old.status if old.id != step.id else StepStatus.PENDING
            new_steps.append(replace(new, status=status))
        run.plan = replace(repaired.plan, id=run.plan.id, created_at=run.plan.created_at, steps=tuple(new_steps))
        self._validated[run.id] = ValidatedPlan(run.plan, repaired.tools, repaired.risks)
        run.current_step_id = None
        self._emit(run, "plan_repaired", step=step, tool=replacement.canonical_name, risk=replacement_risk.value, status="VALIDATED")
        return True

    @staticmethod
    def _is_narrow_repair(original: TaskPlan, repaired: ValidatedPlan, step_id: str) -> bool:
        new_plan = repaired.plan
        if (
            new_plan.goal != original.goal
            or new_plan.privacy != original.privacy
            or new_plan.namespace != original.namespace
            or len(new_plan.steps) != len(original.steps)
            or [step.id for step in new_plan.steps] != [step.id for step in original.steps]
        ):
            return False
        for before, after in zip(original.steps, new_plan.steps):
            same = (
                before.description == after.description
                and before.profile == after.profile
                and before.capability == after.capability
                and before.arguments == after.arguments
                and before.dependencies == after.dependencies
                and before.verification == after.verification
            )
            if not same:
                return False
            if before.id == step_id:
                if not after.preferred_tool or after.preferred_tool == before.preferred_tool:
                    return False
            elif after.preferred_tool != before.preferred_tool:
                return False
        return True

    def _record_tool_result(
        self,
        run: TaskRun,
        step: PlanStep,
        tool: Any,
        result: ToolResult,
        status: StepStatus,
        verification: bool | None,
        attempt: int,
        risk: RiskLevel | None = None,
    ) -> None:
        actual_risk = risk or effective_risk(
            tool, step.arguments,
            ToolInvocationContext(caller="friday_worker", privacy=run.plan.privacy),
        )
        run.step_results.append(StepResult(
            step.id,
            status,
            tool.canonical_name,
            actual_risk.value,
            attempt,
            result.summary[:500],
            result.error_code,
            verification,
            result,
        ))

    def _record_without_tool(self, run: TaskRun, step: PlanStep, status: StepStatus, summary: str) -> None:
        run.step_results.append(StepResult(step.id, status, step.preferred_tool, summary=summary))
        self._set_step_status(run, step.id, status)

    def _block_step(self, run: TaskRun, step: PlanStep, summary: str) -> None:
        self._record_without_tool(run, step, StepStatus.BLOCKED, summary)
        self._skip_remaining(run)
        run.status = TaskStatus.BLOCKED
        run.current_step_id = None
        self._summaries[run.id] = summary
        self._emit(run, "step_failed", step=step, tool=step.preferred_tool, status=StepStatus.BLOCKED.value)
        self._emit(run, "task_completed", status=run.status.value)

    def _fail_step(self, run: TaskRun, step: PlanStep, result: ToolResult, *, blocked: bool = False) -> None:
        self._skip_remaining(run)
        completed = any(item.status == StepStatus.SUCCEEDED for item in run.step_results)
        run.status = TaskStatus.BLOCKED if blocked else (TaskStatus.PARTIAL if completed else TaskStatus.FAILED)
        run.current_step_id = None
        self._summaries[run.id] = result.summary[:500] or "A task step failed."
        self._emit(run, "task_completed", status=run.status.value, duration_ms=result.duration_ms)

    def _skip_remaining(self, run: TaskRun) -> None:
        for step in run.plan.steps:
            if step.status in {StepStatus.PENDING, StepStatus.READY, StepStatus.WAITING_APPROVAL}:
                self._set_step_status(run, step.id, StepStatus.SKIPPED)

    def _cancel_run(self, run: TaskRun) -> None:
        run.cancel()
        if run.current_step_id:
            current = next((s for s in run.plan.steps if s.id == run.current_step_id), None)
            if current is not None and current.status in {StepStatus.PENDING, StepStatus.READY, StepStatus.WAITING_APPROVAL}:
                self._set_step_status(run, current.id, StepStatus.CANCELLED)
        self._skip_remaining(run)
        run.pending_approval = None
        self._pending_step.pop(run.id, None)
        run.status = TaskStatus.CANCELLED
        run.current_step_id = None
        self._summaries[run.id] = "Task cancelled."
        self._emit(run, "task_cancelled", status=run.status.value)

    @staticmethod
    def _set_step_status(run: TaskRun, step_id: str, status: StepStatus) -> None:
        run.plan = replace(
            run.plan,
            steps=tuple(replace(step, status=status) if step.id == step_id else step for step in run.plan.steps),
        )

    @staticmethod
    def _verify(step: PlanStep, result: ToolResult) -> bool:
        if not result.success:
            return False
        value = result.structured_data
        if step.verification == "tool_success":
            return True
        if step.verification == "window_exists":
            return step.preferred_tool == "computer.open_app" and "opened" in result.summary.casefold()
        if step.verification == "non_empty":
            return value not in (None, "", [], {})
        if step.verification == "numeric_result":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return math.isfinite(value)
            if isinstance(value, dict):
                return any(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value.values())
            if isinstance(value, str):
                try:
                    return math.isfinite(float(value.strip()))
                except ValueError:
                    try:
                        parsed = json.loads(value)
                    except ValueError:
                        return False
                    return WorkerExecutor._verify_numeric(parsed)
        return False

    @staticmethod
    def _verify_numeric(value: Any) -> bool:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return math.isfinite(value)
        return isinstance(value, dict) and any(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
            for item in value.values()
        )

    def _emit(
        self,
        run: TaskRun,
        event: str,
        *,
        step: PlanStep | None = None,
        tool: str | None = None,
        risk: str | None = None,
        status: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        item = TaskEvent(
            event=event,
            task_id=run.id,
            step_id=step.id if step else None,
            capability=step.capability if step else None,
            tool_name=tool,
            risk=risk,
            status=status,
            duration_ms=duration_ms,
        )
        run.events.append(item)
        if self.event_sink is not None:
            try:
                self.event_sink(item)
            except Exception:
                return

    def _get_run(self, task_id: str) -> TaskRun:
        with self._lock:
            try:
                return self._runs[task_id]
            except KeyError:
                raise KeyError("Task run not found.") from None

    def _prune_runs(self) -> None:
        while len(self._runs) > 24:
            candidate = next((key for key, value in self._runs.items() if value is not self.current_task and value.status in _TERMINAL), None)
            if candidate is None:
                return
            self._runs.pop(candidate, None)
            self._validated.pop(candidate, None)
            self._pending_step.pop(candidate, None)
            self._summaries.pop(candidate, None)

    def _result(self, run: TaskRun) -> TaskResult:
        return TaskResult(
            run.id,
            run.status,
            self._summaries.get(run.id, "Task state unavailable."),
            tuple(run.step_results),
            run.handoff,
        )
