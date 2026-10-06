"""ToolRegistry adapters for the vetted public API connectors."""

from __future__ import annotations

from typing import Any

from friday.apis.models import ApiResponse, utc_now
from friday.apis.registry import ApiRegistry
from friday.research.engine import ResearchEngine
from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry


_FALLBACK_CODES = frozenset({
    "TIMEOUT", "NETWORK_ERROR", "UPSTREAM_UNAVAILABLE", "RATE_LIMITED", "NOT_FOUND",
    "MALFORMED_JSON", "RESPONSE_TOO_LARGE", "REDIRECT_REJECTED", "PROVIDER_ERROR",
    "INVALID_CONTENT_TYPE", "INVALID_PROVIDER_RESPONSE",
})
_WEATHER_SCHEMA = {"type": "object", "properties": {
    "operation": {"type": "string", "enum": ["current", "forecast"]},
    "city": {"type": "string", "minLength": 2, "maxLength": 120},
    "days": {"type": "integer", "minimum": 1, "maximum": 7},
}, "required": ["operation", "city"], "additionalProperties": False}
_CURRENCY_SCHEMA = {"type": "object", "properties": {
    "operation": {"type": "string", "enum": ["rates", "convert"]},
    "base": {"type": "string", "minLength": 3, "maxLength": 3},
    "quote": {"type": "string", "minLength": 3, "maxLength": 3},
    "amount": {"type": "number", "minimum": 0, "maximum": 1000000000000},
}, "required": ["operation", "base", "quote"], "additionalProperties": False}
_HOLIDAYS_SCHEMA = {"type": "object", "properties": {
    "country_code": {"type": "string", "minLength": 2, "maxLength": 2},
    "country_name": {"type": "string", "minLength": 2, "maxLength": 80},
    "year": {"type": "integer", "minimum": 2000, "maximum": 2100},
}, "required": ["country_code", "year"], "additionalProperties": False}


class PublicApiToolError(RuntimeError):
    """A normalized connector failure; only transient/provider failures permit repair."""

    def __init__(self, response: ApiResponse, *, retryable: bool) -> None:
        super().__init__("The public API provider did not return usable structured data.")
        self.code = "PUBLIC_API_UNAVAILABLE" if retryable else (response.error_code or "PUBLIC_API_ERROR")
        self.structured_data = response.to_dict()


class PublicApiFallbackError(RuntimeError):
    code = "PUBLIC_API_FALLBACK_FAILED"

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__("The bounded public research fallback did not return usable evidence.")
        self.structured_data = response


def register_public_api_tools(
    registry: ToolRegistry,
    api_registry: ApiRegistry,
    research_engine: ResearchEngine,
) -> None:
    def register_api_tool(name: str, capability: str, connector_id: str, schema: dict[str, Any], timeout: float) -> None:
        def handler(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            response = api_registry.invoke(
                connector_id, arguments["operation"] if capability != "holidays" else "list",
                arguments, privacy=context.privacy,
            )
            if not response.success:
                raise PublicApiToolError(response, retryable=response.error_code in _FALLBACK_CODES)
            return response.to_dict()

        registry.register(ToolDefinition(
            name=name, namespace="public_api",
            description=f"Read structured {capability} data from its vetted public API; sends only the requested parameters.",
            input_schema=schema, handler=handler,
            capabilities=frozenset({capability, "public_api"}),
            risk_level=RiskLevel.READ_ONLY, privacy_support=frozenset({"public"}),
            source="native", side_effects="external_read", transmits_data=True,
            timeout_seconds=timeout,
            metadata={"connector_id": connector_id, "fallback_only": False},
        ))

    def register_fallback(name: str, capability: str, schema: dict[str, Any], timeout: float) -> None:
        def handler(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            if context.privacy != "public":
                raise PublicApiFallbackError({"error_code": "POLICY_BLOCKED"})
            query = _fallback_query(capability, arguments)
            try:
                result = research_engine.run(
                    query, privacy="public", freshness="current", max_sources=4,
                    timeout_budget_seconds=18, persist=False,
                )
                status = str(getattr(result.status, "value", result.status))
                formatted = research_engine.format_result(result)
            except Exception:
                raise PublicApiFallbackError({
                    "success": False, "provider": "FRIDAY ResearchEngine", "capability": capability,
                    "requested_at": utc_now(), "source": "research.run", "data": None,
                    "error_code": "RESEARCH_UNAVAILABLE", "from_network": False,
                }) from None
            payload = {
                "success": status == "COMPLETE",
                "provider": "FRIDAY ResearchEngine",
                "capability": capability,
                "requested_at": utc_now(),
                "source": "research.run",
                "data": formatted,
                "error_code": None if status == "COMPLETE" else str(getattr(result.error_code, "value", result.error_code) or status),
                "from_network": bool(getattr(result, "sources", ())),
            }
            if not payload["success"]:
                raise PublicApiFallbackError(payload)
            return payload

        registry.register(ToolDefinition(
            name=name, namespace="public_api",
            description=f"Use only as the single bounded fallback after a failed public API {capability} request.",
            input_schema=schema, handler=handler,
            capabilities=frozenset({capability, "public_api"}),
            risk_level=RiskLevel.READ_ONLY, privacy_support=frozenset({"public"}),
            source="native", side_effects="external_read", transmits_data=True,
            timeout_seconds=timeout,
            metadata={"fallback_only": True},
        ))

    register_api_tool("weather", "weather", "open_meteo", _WEATHER_SCHEMA, 18)
    register_api_tool("currency", "currency", "frankfurter", _CURRENCY_SCHEMA, 8)
    registry.register(ToolDefinition(
        name="holidays", namespace="public_api",
        description="Read public holiday records for one country code and year from Nager.Date Community API.",
        input_schema=_HOLIDAYS_SCHEMA,
        handler=lambda arguments, context: _invoke_holidays(api_registry, arguments, context),
        capabilities=frozenset({"holidays", "public_api"}),
        risk_level=RiskLevel.READ_ONLY, privacy_support=frozenset({"public"}),
        source="native", side_effects="external_read", transmits_data=True, timeout_seconds=8,
        metadata={"connector_id": "nager_date", "fallback_only": False},
    ))
    register_fallback("weather_research_fallback", "weather", _WEATHER_SCHEMA, 25)
    register_fallback("currency_research_fallback", "currency", _CURRENCY_SCHEMA, 25)
    register_fallback("holidays_research_fallback", "holidays", _HOLIDAYS_SCHEMA, 25)


def _invoke_holidays(api_registry: ApiRegistry, arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
    response = api_registry.invoke("nager_date", "list", arguments, privacy=context.privacy)
    if not response.success:
        raise PublicApiToolError(response, retryable=response.error_code in _FALLBACK_CODES)
    return response.to_dict()


def _fallback_query(capability: str, arguments: dict[str, Any]) -> str:
    if capability == "weather":
        return f"Current weather and forecast for {arguments['city']}"
    if capability == "currency":
        return f"Daily reference exchange rate from {arguments['base']} to {arguments['quote']}"
    country = arguments.get("country_name") or f"country code {arguments['country_code']}"
    return f"Public holidays in {country} for {arguments['year']}"
