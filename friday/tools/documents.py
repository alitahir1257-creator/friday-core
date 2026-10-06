"""Approved-root document actions exposed through FRIDAY's ToolRegistry."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from friday.documents import DocumentError, DocumentManager
from friday.research.planner import ResearchQueryError
from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry


_PRIVACY = frozenset({"public", "private", "local_only"})


def register_document_tools(registry: ToolRegistry, manager: DocumentManager) -> None:
    def read(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        document = manager.read(args["source_id"], privacy=context.privacy)
        return {
            "title": document.title,
            "file": asdict(document.source_ref),
            "sections": [{"title": section.title, "content": section.content,
                          "heading_path": list(section.heading_path), "source": asdict(section.source_ref)}
                         for section in document.sections],
            "tables": [{"title": table.title, "rows": table.rows, "source": asdict(table.source_ref)}
                       for table in document.tables],
            "sheets": [{"name": sheet.name, "used_range": sheet.used_range} for sheet in document.sheets],
            "warnings": list(document.warnings),
        }

    def inspect(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        return manager.inspect(args["source_id"], privacy=context.privacy)

    def search(args: dict[str, Any], context: ToolInvocationContext) -> list[dict[str, Any]]:
        return [asdict(hit) for hit in manager.search(
            args["source_id"], args["query"], privacy=context.privacy, limit=args.get("limit", 10),
        )]

    def summarize(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        return manager.summarize(args["source_id"], privacy=context.privacy, section=args.get("section"))

    def compare(args: dict[str, Any], context: ToolInvocationContext) -> list[dict[str, Any]]:
        return manager.compare(args["source_ids"], privacy=context.privacy)

    def sheet_range(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        return manager.sheet_range(
            args["source_id"], args["sheet"], args["range"], operation=args.get("operation"),
            compare_range=args.get("compare_range"), privacy=context.privacy,
        )

    def answer(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        return manager.answer(args["source_id"], args["question"], privacy=context.privacy)

    def register(name: str, description: str, schema: dict[str, Any], handler: Any, capabilities: set[str]) -> None:
        registry.register(ToolDefinition(
            name=name, namespace="documents", description=description, input_schema=schema,
            handler=handler, capabilities=frozenset(capabilities), risk_level=RiskLevel.READ_ONLY,
            privacy_support=_PRIVACY, source="native", side_effects="none", timeout_seconds=45,
        ))

    source = {"type": "string", "minLength": 3, "maxLength": 1024}
    register("inspect", "Inspect one document through a configured approved local root.",
             {"type": "object", "properties": {"source_id": source}, "required": ["source_id"], "additionalProperties": False},
             inspect, {"documents", "filesystem"})
    register("read", "Read normalized sections and provenance from one approved local document.",
             {"type": "object", "properties": {"source_id": source}, "required": ["source_id"], "additionalProperties": False},
             read, {"documents", "filesystem"})
    register("search", "Search exact text in one approved document and return section provenance.",
             {"type": "object", "properties": {"source_id": source,
              "query": {"type": "string", "minLength": 1, "maxLength": 1000},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
              "required": ["source_id", "query"], "additionalProperties": False}, search, {"documents", "search", "filesystem"})
    register("summarize", "Summarize one approved document or named section with local context limits.",
             {"type": "object", "properties": {"source_id": source,
              "section": {"type": "string", "minLength": 1, "maxLength": 200}},
              "required": ["source_id"], "additionalProperties": False}, summarize, {"documents", "filesystem"})
    register("compare", "Compare 2 to 4 approved documents and report changed sections with provenance.",
             {"type": "object", "properties": {"source_ids": {"type": "array", "items": source, "minItems": 2, "maxItems": 4}},
              "required": ["source_ids"], "additionalProperties": False}, compare, {"documents", "filesystem"})
    register("sheet_range", "Read a bounded XLSX range and perform deterministic arithmetic or range comparison.",
             {"type": "object", "properties": {"source_id": source,
              "sheet": {"type": "string", "minLength": 1, "maxLength": 200},
              "range": {"type": "string", "minLength": 1, "maxLength": 40},
              "operation": {"type": "string", "enum": ["sum", "average", "min", "max", "count"]},
              "compare_range": {"type": "string", "minLength": 1, "maxLength": 40}},
              "required": ["source_id", "sheet", "range"], "additionalProperties": False},
             sheet_range, {"documents", "spreadsheets", "filesystem"})
    register("answer", "Answer a question from bounded local document excerpts with provenance.",
             {"type": "object", "properties": {"source_id": source,
              "question": {"type": "string", "minLength": 1, "maxLength": 2000}},
              "required": ["source_id", "question"], "additionalProperties": False}, answer, {"documents", "filesystem"})

    research = manager.research_engine
    if research is not None:
        def can_research(args: dict[str, Any], context: ToolInvocationContext) -> bool:
            if context.privacy != "public":
                return False
            try:
                return bool(research.planner.public_query(args.get("query", ""), privacy="public"))
            except (ResearchQueryError, AttributeError):
                return False

        def research_document(args: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            document = manager.read(args["source_id"], privacy=context.privacy)
            if document.source_ref.privacy != "public":
                raise DocumentError("ACCESS_DENIED", "Web research requires a document from an explicitly public approved root.")
            public_query = research.planner.public_query(args["query"], privacy="public")
            if not public_query:
                raise DocumentError("ACCESS_DENIED", "The query did not pass public-query privacy filtering.")
            local_hits = manager.search(args["source_id"], public_query, privacy=context.privacy, limit=8)
            result = research.run(public_query, privacy="public", max_sources=args.get("max_sources", 5), persist=False)
            return {
                "LOCAL DOCUMENT EVIDENCE": [asdict(hit) for hit in local_hits],
                "DOCUMENT WARNINGS": list(document.warnings),
                "WEB EVIDENCE": research.format_result(result),
            }

        registry.register(ToolDefinition(
            name="research", namespace="documents",
            description="Compare an explicitly public local document with separate public web research; only the query is sent to public search.",
            input_schema={"type": "object", "properties": {"source_id": source,
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "max_sources": {"type": "integer", "minimum": 1, "maximum": 8}},
                "required": ["source_id", "query"], "additionalProperties": False},
            handler=research_document, capabilities=frozenset({"documents", "research"}),
            risk_level=RiskLevel.READ_ONLY, privacy_support=frozenset({"public"}), source="native",
            side_effects="external_read", transmits_data=True, privacy_resolver=can_research,
            timeout_seconds=185,
        ))
