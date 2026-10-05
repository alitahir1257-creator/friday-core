"""Central, fail-closed decisions for every registered tool invocation."""

from __future__ import annotations

from friday.tools.models import ApprovalDecision, RiskLevel, ToolDefinition, ToolInvocationContext


def effective_risk(
    tool: ToolDefinition,
    arguments: dict,
    context: ToolInvocationContext,
) -> RiskLevel:
    raw = tool.risk_level
    if tool.risk_resolver is not None:
        try:
            raw = tool.risk_resolver(arguments, context)
        except Exception:
            return RiskLevel.CONSEQUENTIAL
    try:
        return raw if isinstance(raw, RiskLevel) else RiskLevel(str(raw))
    except (TypeError, ValueError):
        return RiskLevel.CONSEQUENTIAL


class ApprovalPolicy:
    def decide(
        self,
        tool: ToolDefinition,
        arguments: dict,
        context: ToolInvocationContext,
        risk_level: RiskLevel | None = None,
    ) -> ApprovalDecision:
        if not tool.enabled:
            return ApprovalDecision.DENY
        if context.privacy not in tool.privacy_support:
            return ApprovalDecision.DENY
        if context.privacy != "public" and tool.transmits_data:
            return ApprovalDecision.DENY
        if tool.privacy_resolver is not None:
            try:
                if not tool.privacy_resolver(arguments, context):
                    return ApprovalDecision.DENY
            except Exception:
                return ApprovalDecision.DENY

        risk = risk_level or effective_risk(tool, arguments, context)
        if risk == RiskLevel.SENSITIVE and (
            context.privacy == "public" or tool.transmits_data
        ):
            return ApprovalDecision.DENY
        if risk in {RiskLevel.CONSEQUENTIAL, RiskLevel.DESTRUCTIVE}:
            return (
                ApprovalDecision.ALLOW
                if context.explicit_approval
                else ApprovalDecision.REQUIRE_CONFIRMATION
            )
        if tool.requires_confirmation and not context.explicit_approval:
            return ApprovalDecision.REQUIRE_CONFIRMATION
        return ApprovalDecision.ALLOW
