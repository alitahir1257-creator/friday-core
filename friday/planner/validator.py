"""Deterministic validation and safe tool resolution for planner output."""

from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Any

from friday.tools.approvals import effective_risk
from friday.tools.models import RiskLevel, ToolInvocationContext
from friday.tools.registry import ToolRegistry
from friday.memory.policy import normalize_namespace
from friday.planner.models import (
    MAX_PLAN_STEPS,
    PlanStep,
    TaskPlan,
    ValidatedPlan,
    WorkerProfile,
)


_STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BASE_CAPABILITIES = {
    "general", "research", "coding", "computer", "browser", "input", "window",
    "knowledge", "memory", "filesystem", "mcp",
}
_VERIFICATIONS = {"tool_success", "non_empty", "numeric_result", "window_exists"}
_FORBIDDEN_TOOL_PARTS = {
    "shell", "terminal", "powershell", "cmd", "exec", "run", "execute", "command", "script",
}
_FORBIDDEN_CAPABILITIES = _FORBIDDEN_TOOL_PARTS | {"command_execution", "code_execution"}
_PROFILE_CAPABILITIES = {
    WorkerProfile.COMPUTER: {"computer", "browser", "input", "window"},
    WorkerProfile.KNOWLEDGE: {"knowledge", "memory", "filesystem"},
    WorkerProfile.RESEARCH: {"research"},
    WorkerProfile.CODING: {"coding"},
}


class PlanValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanValidator:
    def __init__(self, tool_registry: ToolRegistry, *, max_steps: int = MAX_PLAN_STEPS) -> None:
        self.tool_registry = tool_registry
        self.max_steps = min(max_steps, MAX_PLAN_STEPS)

    def validate(self, plan: TaskPlan) -> ValidatedPlan:
        if not isinstance(plan, TaskPlan):
            raise PlanValidationError("INVALID_PLAN", "A structured task plan is required.")
        if not isinstance(plan.goal, str) or not plan.goal.strip() or len(plan.goal) > 8000:
            raise PlanValidationError("INVALID_GOAL", "The task goal is invalid.")
        if plan.privacy not in {"public", "private", "local_only"}:
            raise PlanValidationError("INVALID_PRIVACY", "The task privacy level is invalid.")
        try:
            normalize_namespace(plan.namespace)
        except (TypeError, ValueError):
            raise PlanValidationError("INVALID_NAMESPACE", "The task namespace is invalid.") from None
        if isinstance(plan.max_steps, bool) or not isinstance(plan.max_steps, int) or not 1 <= plan.max_steps <= self.max_steps:
            raise PlanValidationError("STEP_LIMIT", "The plan exceeds FRIDAY's step limit.")
        if not plan.steps or len(plan.steps) > self.max_steps or len(plan.steps) > plan.max_steps:
            raise PlanValidationError("STEP_LIMIT", "The plan exceeds FRIDAY's step limit.")
        if not isinstance(plan.id, str) or not plan.id.strip() or len(plan.id) > 128:
            raise PlanValidationError("INVALID_PLAN", "The task identifier is invalid.")

        registered = {tool.canonical_name: tool for tool in self.tool_registry.list()}
        known_capabilities = _BASE_CAPABILITIES | {
            capability.casefold()
            for tool in registered.values()
            for capability in tool.capabilities
        }
        steps = {step.id: step for step in plan.steps if isinstance(step, PlanStep)}
        if len(steps) != len(plan.steps):
            raise PlanValidationError("INVALID_STEP", "The plan contains a malformed or duplicate step.")

        resolved: dict[str, Any] = {}
        risks: dict[str, RiskLevel | None] = {}
        for step in plan.steps:
            self._validate_step(step, steps, known_capabilities)
            tool = self._resolve_tool(step, registered)
            if tool is not None:
                self._validate_tool(plan, step, tool)
                risk = effective_risk(
                    tool,
                    step.arguments,
                    ToolInvocationContext(caller="friday_worker", privacy=plan.privacy),
                )
            else:
                risk = None
            resolved[step.id] = tool
            risks[step.id] = risk

        self._validate_acyclic(steps)
        return ValidatedPlan(plan, resolved, risks)

    @staticmethod
    def _validate_step(step: PlanStep, steps: dict[str, PlanStep], known: set[str]) -> None:
        if (
            not isinstance(step.id, str)
            or not _STEP_ID.fullmatch(step.id)
            or not isinstance(step.description, str)
            or not step.description.strip()
            or len(step.description) > 500
            or not isinstance(step.capability, str)
            or step.capability.casefold() not in known
        ):
            raise PlanValidationError("UNKNOWN_CAPABILITY", "A step has an unknown capability or malformed fields.")
        if step.verification not in _VERIFICATIONS:
            raise PlanValidationError("INVALID_VERIFICATION", "A step uses unsupported verification criteria.")
        if step.preferred_tool is not None and (
            not isinstance(step.preferred_tool, str) or not step.preferred_tool.strip()
        ):
            raise PlanValidationError("INVALID_TOOL", "A preferred tool name is invalid.")
        if not isinstance(step.arguments, dict):
            raise PlanValidationError("INVALID_ARGUMENTS", "Tool arguments must be a JSON object.")
        try:
            if len(json.dumps(step.arguments, allow_nan=False, separators=(",", ":"))) > 100_000:
                raise ValueError
        except (TypeError, ValueError):
            raise PlanValidationError("INVALID_ARGUMENTS", "Tool arguments must be finite JSON values within limits.") from None
        if (
            not isinstance(step.dependencies, tuple)
            or any(not isinstance(item, str) or item not in steps for item in step.dependencies)
            or step.id in step.dependencies
            or len(set(step.dependencies)) != len(step.dependencies)
        ):
            raise PlanValidationError("INVALID_DEPENDENCY", "A step has an invalid dependency.")

        profile = WorkerProfile(step.profile)
        if profile in _PROFILE_CAPABILITIES and step.capability not in _PROFILE_CAPABILITIES[profile]:
            raise PlanValidationError("PROFILE_SCOPE", "A worker profile cannot use this capability.")
        if profile == WorkerProfile.CODING and step.preferred_tool:
            raise PlanValidationError("CODING_HANDOFF_ONLY", "CODING steps return handoff metadata in Phase 6B.")

    def _resolve_tool(self, step: PlanStep, registered: dict[str, Any]) -> Any | None:
        if step.preferred_tool:
            tool = registered.get(step.preferred_tool)
            if tool is None:
                raise PlanValidationError("UNKNOWN_TOOL", "A plan references an unregistered tool.")
            return tool

        matches = [
            tool for tool in registered.values()
            if tool.enabled and step.capability in {tag.casefold() for tag in tool.capabilities}
        ]
        if len(matches) > 1:
            raise PlanValidationError("AMBIGUOUS_TOOL", "A step needs an exact registered tool name.")
        return matches[0] if matches else None

    @staticmethod
    def _validate_tool(plan: TaskPlan, step: PlanStep, tool: Any) -> None:
        tool_parts = set(re.split(r"[._-]+", tool.canonical_name.casefold()))
        if tool_parts & _FORBIDDEN_TOOL_PARTS or {tag.casefold() for tag in tool.capabilities} & _FORBIDDEN_CAPABILITIES:
            raise PlanValidationError("FORBIDDEN_TOOL", "Generic shell and command tools cannot run in Phase 6B.")
        if step.capability not in {tag.casefold() for tag in tool.capabilities}:
            raise PlanValidationError("CAPABILITY_MISMATCH", "The preferred tool does not provide the requested capability.")
        if plan.privacy not in tool.privacy_support:
            raise PlanValidationError("PRIVACY_DENIED", "The tool does not support the task privacy level.")
        if plan.privacy != "public" and tool.transmits_data:
            raise PlanValidationError("PRIVACY_DENIED", "Private task data cannot be sent to a remote tool.")
        if tool.canonical_name == "knowledge.search":
            try:
                namespace = normalize_namespace(step.arguments.get("namespace", "global"))
            except (TypeError, ValueError):
                raise PlanValidationError("PRIVACY_DENIED", "Knowledge search must use the task namespace.") from None
            if namespace != normalize_namespace(plan.namespace) or step.arguments.get("privacy", "public") != plan.privacy:
                raise PlanValidationError("PRIVACY_DENIED", "Knowledge search must use the task privacy and namespace.")
        if step.profile == WorkerProfile.GENERAL and step.capability not in {"general", "mcp"}:
            if not (tool.source.startswith("mcp:") and step.capability in {tag.casefold() for tag in tool.capabilities}):
                raise PlanValidationError("PROFILE_SCOPE", "The GENERAL profile cannot invoke this capability.")
        try:
            ToolRegistry._validate_arguments(step.arguments, tool.input_schema)
        except (TypeError, ValueError):
            raise PlanValidationError("INVALID_ARGUMENTS", "Tool arguments do not match the registered input schema.") from None

    @staticmethod
    def _validate_acyclic(steps: dict[str, PlanStep]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise PlanValidationError("DEPENDENCY_CYCLE", "The plan contains a dependency cycle.")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in steps[step_id].dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in steps:
            visit(step_id)
