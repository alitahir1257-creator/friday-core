"""Structured planning through FRIDAY's existing brain and privacy policy."""

from __future__ import annotations

import json
import re
from typing import Callable

from friday.brain_manager import BrainRequestMetadata
from friday.memory.policy import normalize_namespace
from friday.model_policy import ModelPolicy, ModelPolicyEngine, ModelSelection
from friday.planner.models import PlanStep, TaskPlan, WorkerProfile
from friday.router import Route
from friday.tools.registry import ToolRegistry


_MULTI_STEP = re.compile(
    r"\b(?:then|after that|afterwards|next|finally)\b|;|"
    r"\b(?:first|open|launch|start|search|find|read|click|type|write|enter|press|"
    r"navigate|summari[sz]e|save|check)\b.{1,180}\band\b.{0,180}"
    r"\b(?:open|launch|start|search|find|read|click|type|write|enter|press|"
    r"navigate|summari[sz]e|save|check|close|send|publish)\b",
    re.IGNORECASE | re.DOTALL,
)
_STEP_FIELDS = {
    "id", "description", "profile", "capability", "preferred_tool", "arguments",
    "dependencies", "risk_hint", "verification",
}


def is_multi_step_request(goal: str) -> bool:
    """Use explicit action sequences as the planner boundary; simple asks stay direct."""
    return bool(_MULTI_STEP.search(goal))


def _schema_contract(schema: dict) -> dict:
    allowed = {
        "type", "required", "additionalProperties", "items", "enum", "const",
        "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    }
    result = {key: value for key, value in schema.items() if key in allowed}
    if isinstance(schema.get("properties"), dict):
        result["properties"] = {
            key: _schema_contract(value)
            for key, value in schema["properties"].items()
            if isinstance(value, dict)
        }
    if isinstance(schema.get("items"), dict):
        result["items"] = _schema_contract(schema["items"])
    if isinstance(schema.get("additionalProperties"), dict):
        result["additionalProperties"] = _schema_contract(schema["additionalProperties"])
    return result


class Planner:
    def __init__(
        self,
        brain_manager: object | None,
        tool_registry: ToolRegistry,
        policy_engine: ModelPolicyEngine | None = None,
        *,
        model_call: Callable[[str, str, str], str] | None = None,
    ) -> None:
        self.brain_manager = brain_manager
        self.tool_registry = tool_registry
        self.policy_engine = policy_engine or ModelPolicyEngine()
        self.model_call = model_call
        self.model_calls = 0
        self.last_selection: ModelSelection | None = None

    def plan(self, goal: str, *, privacy: str = "public", namespace: str = "global") -> TaskPlan:
        return self._generate(goal, privacy, namespace)

    def repair(
        self,
        plan: TaskPlan,
        *,
        step_id: str,
        failure_code: str,
    ) -> TaskPlan:
        namespace = normalize_namespace(plan.namespace)
        failed_step = next(step for step in plan.steps if step.id == step_id)
        context = {
            "step_id": step_id,
            "capability": failed_step.capability,
            "failure_code": failure_code,
            "steps": [
                {
                    "id": step.id,
                    "description": step.description,
                    "profile": step.profile.value,
                    "capability": step.capability,
                    "preferred_tool": step.preferred_tool,
                    "arguments": step.arguments,
                    "dependencies": list(step.dependencies),
                    "verification": step.verification,
                }
                for step in plan.steps
            ],
        }
        return self._generate(
            plan.goal, plan.privacy, namespace,
            repair_context=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        )

    def coding_handoff(self, goal: str, *, privacy: str = "public", namespace: str = "global") -> TaskPlan:
        return TaskPlan(
            goal=goal,
            privacy=privacy,
            namespace=normalize_namespace(namespace),
            steps=(PlanStep(
                id="coding_handoff",
                description="Hand repository work to the existing Codex path.",
                profile=WorkerProfile.CODING,
                capability="coding",
            ),),
            metadata={"handoff": {"destination": "codex", "automatic": False}},
        )

    def capability_placeholder(
        self,
        goal: str,
        *,
        capability: str,
        profile: WorkerProfile,
        privacy: str = "public",
        namespace: str = "global",
    ) -> TaskPlan:
        return TaskPlan(
            goal=goal,
            privacy=privacy,
            namespace=normalize_namespace(namespace),
            steps=(PlanStep(
                id=f"{profile.value.casefold()}_placeholder",
                description=f"Use the {profile.value.casefold()} capability for the requested task.",
                profile=profile,
                capability=capability,
            ),),
        )

    def _generate(
        self,
        goal: str,
        privacy: str,
        namespace: str,
        *,
        repair_context: str | None = None,
    ) -> TaskPlan:
        privacy = str(getattr(privacy, "value", privacy)).casefold()
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
            raise ValueError("The planning goal must contain 1 to 8000 characters.")
        if privacy not in {"public", "private", "local_only"}:
            raise ValueError("privacy must be public, private, or local_only.")
        namespace = normalize_namespace(namespace)
        allowed_tools = [
            {
                "name": tool.canonical_name,
                "capabilities": sorted(cap.casefold() for cap in tool.capabilities),
                "input_schema": _schema_contract(tool.input_schema),
            }
            for tool in self.tool_registry.list(enabled_only=True)
            if privacy in tool.privacy_support and (privacy == "public" or not tool.transmits_data)
        ]
        instruction = (
            "Create a bounded FRIDAY task plan. Return exactly one JSON object with a single key `steps`; "
            "no Markdown or extra text. Use 1-8 steps. Each step must contain id, description, profile, "
            "capability, preferred_tool, arguments, dependencies, and verification. Profiles are GENERAL, "
            "COMPUTER, KNOWLEDGE, RESEARCH, CODING. Use only exact tool names and capabilities from the "
            "catalog below. preferred_tool may be null only for a known placeholder capability. Never invent "
            "tools, use shell/command execution, or claim to have executed anything. Risk hints are ignored; "
            "tool policy decides approval. Use only the user's stated goal. Verification values: tool_success, "
            "non_empty, numeric_result, window_exists.\n"
            f"Task privacy: {privacy}. Task namespace: {namespace}. For knowledge.search, use exactly this privacy and namespace.\n"
            f"Allowed tools: {json.dumps(allowed_tools, ensure_ascii=False, separators=(',', ':'))}"
        )
        if repair_context is not None:
            instruction += (
                "\nRepair the unavailable read-only step using a different enabled tool with the same capability. "
                "Keep the goal, step IDs, dependencies, descriptions, arguments, and all other steps unchanged. "
                "Do not add work. Existing validated plan and safe failure code: " + repair_context
            )

        policy = ModelPolicy.PRIVATE if privacy in {"private", "local_only"} else ModelPolicy.SMART
        selection = self.policy_engine.select(
            goal,
            route_hint=Route.GENERAL,
            privacy=privacy,
            requested_policy=policy,
            cloudflare_enabled=bool(getattr(self.brain_manager, "cloud_enabled", False)),
            planning=True,
        )
        self.last_selection = selection
        if self.model_call is not None:
            raw = self.model_call(goal, instruction, privacy)
            self.model_calls += 1
        else:
            respond = getattr(self.brain_manager, "respond", None)
            if not callable(respond):
                raise RuntimeError("The FRIDAY planning brain is unavailable.")
            metadata = BrainRequestMetadata(
                sensitivity=privacy,
                privacy=privacy,
                task_class=policy.value,
                memory_namespace=namespace,
            )
            reply = respond(
                goal,
                [],
                request_metadata=metadata,
                model_selection=selection,
                planning=True,
                system_instruction=instruction,
            )
            self.model_calls += 1
            self.last_selection = reply.selection or selection
            raw = reply.text
        return self._parse_plan(goal, privacy, namespace, raw)

    @staticmethod
    def _parse_plan(goal: str, privacy: str, namespace: str, raw: str) -> TaskPlan:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("Planner output must be valid JSON.") from None
        if not isinstance(payload, dict) or set(payload) != {"steps"} or not isinstance(payload["steps"], list):
            raise ValueError("Planner output must contain only a steps array.")
        steps = []
        for item in payload["steps"]:
            if not isinstance(item, dict) or set(item) - _STEP_FIELDS:
                raise ValueError("Planner output contains an invalid step object.")
            required = {"id", "description", "profile", "capability", "arguments"}
            if not required <= set(item):
                raise ValueError("Planner output omits required step fields.")
            try:
                steps.append(PlanStep(
                    id=item["id"],
                    description=item["description"],
                    profile=str(item["profile"]).upper(),
                    capability=item["capability"],
                    preferred_tool=item.get("preferred_tool"),
                    arguments=item["arguments"],
                    dependencies=tuple(item.get("dependencies", ())),
                    risk_hint=item.get("risk_hint") if isinstance(item.get("risk_hint"), str) else None,
                    verification=item.get("verification", "tool_success"),
                ))
            except (TypeError, ValueError):
                raise ValueError("Planner output contains an invalid step value.") from None
        return TaskPlan(
            goal=goal,
            privacy=privacy,
            namespace=namespace,
            steps=tuple(steps),
        )
