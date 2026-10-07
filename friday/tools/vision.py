"""Vision Lite tool adapters (Phase 8A.1): read-only screen observation.

Follows the existing ``friday/tools/adapters.py`` pattern: thin handlers over
a lazily provided manager, registered as READ_ONLY tools. Screenshots never
leave the machine and never enter tool results (structured evidence only).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from friday.tools.models import RiskLevel, ToolDefinition, ToolInvocationContext
from friday.tools.registry import ToolRegistry
from friday.vision.manager import VisionManager
from friday.vision.targets import GroundedTarget


_PRIVACY = frozenset({"public", "private", "local_only"})

_REGION_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer", "minimum": 0},
        "y": {"type": "integer", "minimum": 0},
        "width": {"type": "integer", "minimum": 1, "maximum": 8192},
        "height": {"type": "integer", "minimum": 1, "maximum": 8192},
        "monitor": {"type": "integer", "minimum": 1},
        "label": {"type": "string", "maxLength": 80},
    },
    "required": ["width", "height"],
    "additionalProperties": False,
}

_OBSERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "region": _REGION_SCHEMA,
        "monitor": {"type": "integer", "minimum": 1, "maximum": 16},
        "window": {"type": "string", "minLength": 1, "maxLength": 260},
        "include_dom": {"type": "boolean"},
        "ocr_mode": {"type": "string", "enum": ["auto", "always", "never"]},
    },
    "additionalProperties": False,
}


def _element_to_dict(element) -> dict[str, Any]:
    return {"name": element.name, "kind": element.kind,
            "identifier": element.identifier, "source": element.source.value,
            "bbox": None if element.bbox is None else list(element.bbox),
            "visible": element.visible, "enabled": element.enabled}


def observation_to_dict(observation) -> dict[str, Any]:
    """Structured observation for tool results. No raw image bytes, ever."""
    region = observation.region
    return {
        "status": observation.status,
        "timestamp": observation.timestamp,
        "region": None if region is None else {
            "x": region.x, "y": region.y, "width": region.width,
            "height": region.height, "monitor": region.monitor, "label": region.label,
        },
        "dimensions": None if observation.dimensions is None else list(observation.dimensions),
        "active_window": observation.active_window,
        "capture_bounds": None if observation.capture_bounds is None else list(observation.capture_bounds),
        "window_bounds": None if observation.window_bounds is None else list(observation.window_bounds),
        "window_handle": observation.window_handle,
        "uia_elements": [_element_to_dict(item) for item in observation.uia_elements],
        "dom_elements": [_element_to_dict(item) for item in observation.dom_elements],
        "ocr_blocks": [
            {"text": block.text, "confidence": block.confidence,
             "bbox": list(block.bbox), "source": block.source.value}
            for block in observation.ocr_blocks
        ],
        "fused_text": observation.fused_text,
        "ocr_status": observation.ocr_status.value,
        "remote_calls": observation.remote_calls,
        "trust": observation.trust,
        "notice": observation.notice,
        "provenance": list(observation.provenance),
    }


def _target_to_dict(target: GroundedTarget) -> dict[str, Any]:
    return {
        "id": target.id, "label": target.label, "normalized_label": target.normalized_label,
        "bbox": list(target.bbox), "center": [target.center_x, target.center_y],
        "source": target.source, "confidence": target.confidence,
        "window_title": target.window_title, "visible": target.visible,
        "enabled": target.enabled, "control_type": target.control_type,
        "observation_timestamp": target.observation_timestamp,
        "window_handle": target.window_handle, "metadata": target.metadata,
    }


def _candidate_to_dict(target: GroundedTarget) -> dict[str, Any]:
    return {"label": target.label, "source": target.source,
            "bbox": list(target.bbox), "confidence": target.confidence}


def register_vision_tools(
    registry: ToolRegistry,
    vision_provider: Callable[[], VisionManager],
) -> None:
    """Register read-only Vision Lite tools. Local-only by construction."""

    def observe(args: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        observation = vision_provider().observe(
            region=args.get("region"),
            monitor=args.get("monitor", 1),
            window_target=args.get("window"),
            include_dom=args.get("include_dom", False),
            ocr_mode=args.get("ocr_mode", "auto"),
        )
        return observation_to_dict(observation)

    registry.register(ToolDefinition(
        name="observe_screen", namespace="computer",
        description="Capture a lightweight on-demand screen observation: UIA/DOM evidence first, OCR fallback. Local-only, read-only.",
        input_schema=_OBSERVE_SCHEMA,
        handler=observe,
        capabilities=frozenset({"computer", "vision"}),
        risk_level=RiskLevel.READ_ONLY,
        privacy_support=_PRIVACY, source="native",
        side_effects="none", transmits_data=False,
        timeout_seconds=60.0,
    ))

    def read_text(args: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        observation = vision_provider().observe(
            region=args.get("region"),
            monitor=args.get("monitor", 1),
            window_target=args.get("window"),
            include_dom=args.get("include_dom", False),
            ocr_mode=args.get("ocr_mode", "auto"),
        )
        return {
            "status": observation.status,
            "text": observation.fused_text,
            "sources": sorted({item.split(":", 1)[0] for item in observation.provenance if ":" in item}),
            "ocr_status": observation.ocr_status.value,
            "trust": observation.trust,
            "notice": observation.notice,
        }

    registry.register(ToolDefinition(
        name="read_visible_text", namespace="computer",
        description="Read visible screen text (UIA/DOM preferred, OCR fallback) as structured data. Local-only, read-only.",
        input_schema=_OBSERVE_SCHEMA,
        handler=read_text,
        capabilities=frozenset({"computer", "vision"}),
        risk_level=RiskLevel.READ_ONLY,
        privacy_support=_PRIVACY, source="native",
        side_effects="none", transmits_data=False,
        timeout_seconds=60.0,
    ))

    def find_target(args: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        options = {
            "active_window_only": not args.get("screen_wide", False),
            "max_candidates": args.get("max_candidates", 5),
        }
        if "window" in args:
            options["window_target"] = args["window"]
        result = vision_provider().find_target(args["query"], **options)
        return {"status": result.status,
                "target": _target_to_dict(result.target) if result.target else None,
                "candidates": [_candidate_to_dict(item) for item in result.candidates]}

    registry.register(ToolDefinition(
        name="find_target", namespace="vision",
        description="Find a visible screen target by exact or normalized text; local and read-only.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 260},
            "window": {"type": "string", "minLength": 1, "maxLength": 260},
            "screen_wide": {"type": "boolean"},
            "max_candidates": {"type": "integer", "minimum": 1, "maximum": 20},
        }, "required": ["query"], "additionalProperties": False},
        handler=find_target, capabilities=frozenset({"computer", "vision"}),
        risk_level=RiskLevel.READ_ONLY, privacy_support=_PRIVACY, source="native",
        side_effects="none", transmits_data=False, timeout_seconds=60.0,
    ))
