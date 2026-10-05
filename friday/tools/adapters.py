"""Thin adapters that keep existing FRIDAY capabilities authoritative."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from friday.capabilities.computer.controller import ComputerController
from friday.capabilities.registry import Action, ActionPlan, Capability, Risk
from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry


_PRIVACY = frozenset({"public", "private", "local_only"})
_READ_ONLY_OPERATIONS = {
    (Capability.WINDOW, "ENUMERATE_WINDOWS"),
    (Capability.WINDOW, "INSPECT_CONTROLS"),
    (Capability.BROWSER, "INSPECT_PAGE"),
    (Capability.BROWSER, "READ_VISIBLE_TEXT"),
}
_BROWSER_OPERATIONS_THAT_SEND = {
    "NAVIGATE", "NEW_TAB", "BACK", "FORWARD", "REFRESH", "CLICK_ELEMENT", "TYPE_ELEMENT",
}


class ToolAdapterError(RuntimeError):
    """A capability refused an invocation under its existing safety contract."""


def register_computer_tools(
    registry: ToolRegistry,
    controller_provider: Callable[[], ComputerController],
) -> None:
    def run_plan(action: Action, context: ToolInvocationContext) -> str:
        result, risk = controller_provider().execute(ActionPlan((action,)), approved=context.explicit_approval)
        if risk != Risk.SAFE:
            raise ToolAdapterError("The existing COMPUTER policy blocked this action.")
        return result

    registry.register(ToolDefinition(
        name="open_app", namespace="computer", description="Open an installed desktop application.",
        input_schema={"type": "object", "properties": {"app": {"type": "string", "minLength": 1, "maxLength": 200}},
                      "required": ["app"], "additionalProperties": False},
        handler=lambda args, context: run_plan(
            Action(Capability.COMPUTER, "OPEN_APP", args["app"]), context
        ),
        capabilities=frozenset({"computer"}), risk_level=RiskLevel.REVERSIBLE,
        privacy_support=_PRIVACY, source="native", side_effects="local_desktop",
    ))

    registry.register(ToolDefinition(
        name="type_text", namespace="computer", description="Type literal text into the focused desktop application.",
        input_schema={"type": "object", "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 10000},
        }, "required": ["text"], "additionalProperties": False},
        handler=lambda args, context: run_plan(
            Action(Capability.INPUT, "TYPE_TEXT", args["text"]), context
        ),
        capabilities=frozenset({"computer", "input"}), risk_level=RiskLevel.CONSEQUENTIAL,
        privacy_support=_PRIVACY, source="native", side_effects="local_input",
    ))

    registry.register(ToolDefinition(
        name="open_url", namespace="browser", description="Open an HTTP or HTTPS website in the managed browser.",
        input_schema={"type": "object", "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2048}},
                      "required": ["url"], "additionalProperties": False},
        handler=lambda args, context: run_plan(
            Action(Capability.BROWSER, "NAVIGATE", args["url"]), context
        ),
        capabilities=frozenset({"browser"}), risk_level=RiskLevel.REVERSIBLE,
        privacy_support=frozenset({"public"}), source="native", side_effects="external_navigation",
        transmits_data=True,
    ))

    def request_risk(args: dict[str, Any], _context: ToolInvocationContext) -> RiskLevel:
        controller = controller_provider()
        request_risk = controller.policy.classify_request(args["request"])
        if request_risk in {Risk.CONFIRM, Risk.BLOCKED}:
            return RiskLevel.DESTRUCTIVE if request_risk == Risk.BLOCKED else RiskLevel.CONSEQUENTIAL
        plan = controller.planner.plan(args["request"])
        if plan is None:
            return RiskLevel.READ_ONLY
        levels = [controller.policy.classify(action) for action in plan.actions]
        if Risk.BLOCKED in levels:
            return RiskLevel.DESTRUCTIVE
        if Risk.CONFIRM in levels:
            return RiskLevel.CONSEQUENTIAL
        if all((action.capability, action.operation.upper()) in _READ_ONLY_OPERATIONS for action in plan.actions):
            return RiskLevel.READ_ONLY
        return RiskLevel.REVERSIBLE

    def request_privacy(args: dict[str, Any], context: ToolInvocationContext) -> bool:
        if context.privacy == "public":
            return True
        plan = controller_provider().planner.plan(args["request"])
        if plan is None:
            return True
        return not any(
            action.capability == Capability.BROWSER
            and action.operation.upper() in _BROWSER_OPERATIONS_THAT_SEND
            for action in plan.actions
        )

    registry.register(ToolDefinition(
        name="dispatch_request", namespace="computer",
        description="Run an existing deterministic COMPUTER request through FRIDAY's dispatcher.",
        input_schema={"type": "object", "properties": {"request": {"type": "string", "minLength": 1, "maxLength": 2000}},
                      "required": ["request"], "additionalProperties": False},
        handler=lambda args, context: _run_request(controller_provider(), args["request"], context),
        capabilities=frozenset({"computer", "browser"}), risk_level=RiskLevel.CONSEQUENTIAL,
        risk_resolver=request_risk, privacy_support=_PRIVACY, privacy_resolver=request_privacy,
        source="native", side_effects="local_or_external",
    ))


def _run_request(
    controller: ComputerController,
    request: str,
    context: ToolInvocationContext,
) -> str:
    answer, risk = controller.execute_request(request, approved=context.explicit_approval)
    if risk != Risk.SAFE:
        raise ToolAdapterError("The existing COMPUTER policy blocked this request.")
    return answer


def register_knowledge_tools(registry: ToolRegistry, manager: Any | None) -> None:
    def search(args: dict[str, Any], context: ToolInvocationContext) -> list[dict[str, Any]]:
        if manager is None:
            raise RuntimeError("Unified knowledge is unavailable.")
        requested_privacy = args.get("privacy", "public")
        privacy = max((context.privacy, requested_privacy), key={"public": 0, "private": 1, "local_only": 2}.__getitem__)
        results = manager.search(
            args["query"], namespace=args.get("namespace", "global"),
            privacy_level=privacy, route_backend="local", max_results=args.get("max_results", 10),
        )
        return [asdict(result) for result in results]

    registry.register(ToolDefinition(
        name="search", namespace="knowledge", description="Search FRIDAY's privacy-filtered local memory, vault, and approved local sources.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "namespace": {"type": "string", "minLength": 1, "maxLength": 100},
            "privacy": {"type": "string", "enum": ["public", "private", "local_only"]},
            "max_results": {"type": "integer", "minimum": 0, "maximum": 20},
        }, "required": ["query"], "additionalProperties": False},
        handler=search, capabilities=frozenset({"knowledge", "memory", "filesystem"}),
        risk_level=RiskLevel.READ_ONLY, privacy_support=_PRIVACY, source="native",
        side_effects="none",
    ))

    def list_sources(_args: dict[str, Any], _context: ToolInvocationContext) -> list[dict[str, str]]:
        if manager is None:
            raise RuntimeError("Unified knowledge is unavailable.")
        sources = []
        if manager.memory is not None:
            sources.append({"name": "Memory", "type": "memory", "status": "READY"})
        if manager.vault is not None:
            sources.append({"name": "Obsidian Vault", "type": "vault", "status": "READY"})
        for connector_name, status in manager.connectors.status().items():
            connector = next((item for item in manager.connectors.list_connectors() if item.name == connector_name), None)
            sources.append({
                "name": connector_name,
                "type": connector.source_type if connector is not None else "connector",
                "status": str(status),
            })
        return sources

    registry.register(ToolDefinition(
        name="list_sources", namespace="knowledge", description="List the configured local knowledge sources and safe status labels.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_sources, capabilities=frozenset({"knowledge"}), risk_level=RiskLevel.READ_ONLY,
        privacy_support=_PRIVACY, source="native", side_effects="none",
    ))
