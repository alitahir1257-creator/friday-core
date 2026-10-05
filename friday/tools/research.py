"""ResearchEngine adapter registrations for FRIDAY's single ToolRegistry path."""

from __future__ import annotations

from typing import Any

from friday.research.engine import ResearchEngine
from friday.research.planner import ResearchPlanner, ResearchQueryError
from friday.research.search import SearchProviderError
from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry


_ALL_PRIVACY = frozenset({"public", "private", "local_only"})


def register_research_tools(registry: ToolRegistry, engine: ResearchEngine) -> None:
    planner = engine.planner

    def can_send_query(args: dict[str, Any], context: ToolInvocationContext) -> bool:
        if context.privacy == "local_only":
            return False
        question = args.get("question", args.get("query", ""))
        try:
            return bool(planner.public_query(question, privacy=context.privacy))
        except ResearchQueryError:
            return False

    def run_is_private_safe(_args: dict[str, Any], context: ToolInvocationContext) -> bool:
        # The engine exits before all network access for local-only requests.
        if context.privacy == "local_only":
            return True
        return can_send_query(_args, context)

    def search(args: dict[str, Any], context: ToolInvocationContext) -> list[dict[str, Any]]:
        try:
            results = engine.search(
                args["query"], privacy=context.privacy,
                max_results=args.get("max_results", 8),
            )
        except SearchProviderError as error:
            return [{"status": error.code, "message": str(error)}]
        return [
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "source": item.source,
                "rank": item.rank,
                "published_at": item.published_at,
                "metadata": dict(item.metadata),
            }
            for item in results
        ]

    registry.register(ToolDefinition(
        name="search",
        namespace="research",
        description="Search configured zero-cost public research providers using a sanitized query.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "required": ["query"], "additionalProperties": False},
        handler=search,
        capabilities=frozenset({"research", "search"}),
        risk_level=RiskLevel.READ_ONLY,
        privacy_support=_ALL_PRIVACY,
        source="native",
        side_effects="external_read",
        transmits_data=True,
        privacy_resolver=can_send_query,
        timeout_seconds=15,
    ))

    def fetch(args: dict[str, Any], _context: ToolInvocationContext) -> str:
        page = engine.fetch(args["url"], timeout_seconds=args.get("timeout_seconds"))
        published = page.published_at or "unknown"
        headings = "\n".join(f"- {item}" for item in page.headings[:30]) or "(none)"
        return (
            f"Title: {page.title}\nURL: {page.final_url}\nCanonical URL: {page.canonical_url}\n"
            f"Published: {published}\nHeadings:\n{headings}\n\n{page.text[:20_000]}"
        )

    registry.register(ToolDefinition(
        name="fetch",
        namespace="research",
        description="Fetch and extract a selected public HTTP/HTTPS text page.",
        input_schema={"type": "object", "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 4096},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 30},
        }, "required": ["url"], "additionalProperties": False},
        handler=fetch,
        capabilities=frozenset({"research"}),
        risk_level=RiskLevel.READ_ONLY,
        privacy_support=frozenset({"public"}),
        source="native",
        side_effects="external_read",
        transmits_data=True,
        timeout_seconds=35,
    ))

    def run(args: dict[str, Any], context: ToolInvocationContext) -> str:
        result = engine.run(
            args["question"],
            privacy=context.privacy,
            freshness=args.get("freshness", "current"),
            max_sources=args.get("max_sources", 8),
            timeout_budget_seconds=args.get("timeout_budget_seconds", 45),
            preferred_domains=args.get("preferred_domains", ()),
            excluded_domains=args.get("excluded_domains", ()),
            source_urls=args.get("source_urls", ()),
            persist=args.get("persist", False),
            cancelled=context.cancellation_check,
        )
        return engine.format_result(result)

    registry.register(ToolDefinition(
        name="run",
        namespace="research",
        description="Run bounded public research through search, safe fetch, extraction, evidence ranking, and citation-aware synthesis.",
        input_schema={"type": "object", "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "freshness": {"type": "string", "enum": ["current", "recent", "timeless"]},
            "max_sources": {"type": "integer", "minimum": 1, "maximum": 8},
            "timeout_budget_seconds": {"type": "number", "minimum": 1, "maximum": 180},
            "preferred_domains": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 253}, "maxItems": 20},
            "excluded_domains": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 253}, "maxItems": 20},
            "source_urls": {"type": "array", "items": {"type": "string", "minLength": 8, "maxLength": 4096}, "maxItems": 8},
            "persist": {"type": "boolean"},
        }, "required": ["question"], "additionalProperties": False},
        handler=run,
        capabilities=frozenset({"research"}),
        risk_level=RiskLevel.READ_ONLY,
        privacy_support=_ALL_PRIVACY,
        source="native",
        side_effects="external_read",
        transmits_data=True,
        privacy_resolver=run_is_private_safe,
        timeout_seconds=185,
    ))

