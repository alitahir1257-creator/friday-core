"""The single synchronous invocation path for native and MCP tools."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
import re
import threading
import time
from typing import Any, Callable

from friday.tools.approvals import ApprovalPolicy, effective_risk
from friday.tools.models import (
    ApprovalDecision,
    RiskLevel,
    ToolApprovalRequest,
    ToolDefinition,
    ToolInvocationContext,
    ToolReceipt,
    ToolResult,
)


_NAME_PART = re.compile(r"^[a-z][a-z0-9_-]*$")
_SECRET_KEY = re.compile(r"password|secret|token|authorization|cookie|api[_-]?key|credential", re.I)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|credential)\s*([:=])\s*[^\s,;]+"
)
_SUPPORTED_SCHEMA_KEYS = {
    "$schema", "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "description", "title", "default",
}
_RECEIPT_LIMIT = 1000
_SCHEMA_SIZE_LIMIT = 100_000


class ToolRegistrationError(ValueError):
    """A tool definition is unsafe, malformed, or conflicts with a registration."""


class ToolRegistry:
    def __init__(self, approval_policy: ApprovalPolicy | None = None) -> None:
        self.approval_policy = approval_policy or ApprovalPolicy()
        self._tools: dict[str, ToolDefinition] = {}
        self._receipts: deque[ToolReceipt] = deque(maxlen=_RECEIPT_LIMIT)
        self._lock = threading.RLock()

    def register(self, tool: ToolDefinition, *, replace_existing: bool = False) -> None:
        parts = (*tool.namespace.split("."), tool.name)
        if not all(_NAME_PART.fullmatch(part) for part in parts):
            raise ToolRegistrationError("Tool names must use simple namespaced identifiers.")
        if not callable(tool.handler):
            raise ToolRegistrationError("Tool handler must be callable.")
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ToolRegistrationError("Tool input schema must describe an object.")
        if not isinstance(tool.capabilities, (set, frozenset)) or not all(
            isinstance(value, str) and value.strip() for value in tool.capabilities
        ):
            raise ToolRegistrationError("Tool capabilities must be non-empty names.")
        if not isinstance(tool.privacy_support, (set, frozenset)) or not tool.privacy_support <= {
            "public", "private", "local_only"
        }:
            raise ToolRegistrationError("Tool privacy support is invalid.")
        if isinstance(tool.timeout_seconds, bool) or not isinstance(tool.timeout_seconds, (int, float)) or not 0 < tool.timeout_seconds <= 300:
            raise ToolRegistrationError("Tool timeout must be between 0 and 300 seconds.")
        try:
            schema_size = len(json.dumps(tool.input_schema, allow_nan=False, separators=(",", ":")))
        except (TypeError, ValueError):
            raise ToolRegistrationError("Tool input schema must contain finite JSON values.") from None
        if schema_size > _SCHEMA_SIZE_LIMIT:
            raise ToolRegistrationError("Tool input schema exceeds the 100 KB limit.")
        self._validate_schema_shape(tool.input_schema)

        key = tool.canonical_name
        with self._lock:
            existing = self._tools.get(key)
            if existing is not None:
                if not replace_existing or existing.source != tool.source:
                    raise ToolRegistrationError(f"Tool already registered: {key}")
            self._tools[key] = tool

    def unregister(self, canonical_name: str) -> ToolDefinition | None:
        with self._lock:
            return self._tools.pop(canonical_name, None)

    def enable(self, canonical_name: str) -> None:
        self._set_enabled(canonical_name, True)

    def disable(self, canonical_name: str) -> None:
        self._set_enabled(canonical_name, False)

    def _set_enabled(self, canonical_name: str, enabled: bool) -> None:
        from dataclasses import replace

        with self._lock:
            tool = self._tools[canonical_name]
            self._tools[canonical_name] = replace(tool, enabled=enabled)

    def get(self, canonical_name: str) -> ToolDefinition | None:
        with self._lock:
            return self._tools.get(canonical_name)

    def list(self, *, enabled_only: bool = False) -> list[ToolDefinition]:
        with self._lock:
            return [
                tool
                for _, tool in sorted(self._tools.items())
                if not enabled_only or tool.enabled
            ]

    def find_by_capability(self, capability: str) -> list[ToolDefinition]:
        wanted = capability.casefold()
        return [tool for tool in self.list() if wanted in {tag.casefold() for tag in tool.capabilities}]

    @property
    def receipts(self) -> tuple[ToolReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def execute(
        self,
        canonical_name: str,
        arguments: dict[str, Any],
        *,
        caller: str = "user",
        privacy: str = "public",
        action_metadata: Mapping[str, Any] | None = None,
        cancellation_check: Callable[[], bool] | None = None,
        approval_callback: Callable[[ToolApprovalRequest], bool] | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        tool = self.get(canonical_name)
        if tool is None:
            return self._finish(
                canonical_name, RiskLevel.CONSEQUENTIAL, ApprovalDecision.DENY, False,
                "unavailable", "This tool is not registered.", "TOOL_NOT_FOUND", started, "unknown",
            )

        try:
            context = ToolInvocationContext(caller, privacy, False, action_metadata or {}, cancellation_check)
        except ValueError as error:
            return self._finish(
                canonical_name, effective_risk(tool, {}, ToolInvocationContext()), ApprovalDecision.DENY,
                False, "invalid_context", str(error), "INVALID_CONTEXT", started, tool.side_effects,
            )

        risk = effective_risk(tool, arguments if isinstance(arguments, dict) else {}, context)
        if not tool.enabled:
            return self._finish(
                canonical_name, risk, ApprovalDecision.DENY, False, "disabled",
                "This tool is disabled.", "TOOL_DISABLED", started, tool.side_effects,
            )
        try:
            self._validate_arguments(arguments, tool.input_schema)
        except (TypeError, ValueError) as error:
            return self._finish(
                canonical_name, risk, ApprovalDecision.DENY, False, "invalid_arguments",
                str(error), "INVALID_ARGUMENTS", started, tool.side_effects,
                secret_values=_secret_values(arguments),
            )

        decision = self.approval_policy.decide(tool, arguments, context, risk)
        approval_result = decision.value
        if decision == ApprovalDecision.REQUIRE_CONFIRMATION:
            if approval_callback is None:
                return self._finish(
                    canonical_name, risk, decision, False, "approval_required",
                    "Explicit approval is required before this tool can run.", None, started,
                    tool.side_effects, requires_followup=True,
                    secret_values=_secret_values(arguments),
                )
            request = ToolApprovalRequest(
                canonical_name,
                risk,
                context.privacy,
                context.caller,
                tuple(sorted(arguments)),
                _approval_summary(canonical_name, arguments),
            )
            try:
                approved = approval_callback(request) is True
            except Exception:
                approved = False
            if not approved:
                return self._finish(
                    canonical_name, risk, ApprovalDecision.DENY, False, "denied",
                    "The action was not approved.", "APPROVAL_DENIED", started, tool.side_effects,
                    approval_result="DENY", secret_values=_secret_values(arguments),
                )
            approval_result = "USER_APPROVED"
            context = ToolInvocationContext(
                context.caller, context.privacy, True, context.action_metadata, context.cancellation_check,
            )
            decision = self.approval_policy.decide(tool, arguments, context, risk)

        if decision != ApprovalDecision.ALLOW:
            return self._finish(
                canonical_name, risk, decision, False, "denied",
                "This tool invocation is blocked by FRIDAY's privacy or approval policy.",
                "POLICY_DENIED", started, tool.side_effects, approval_result=decision.value,
                secret_values=_secret_values(arguments),
            )

        secret_values = _secret_values(arguments)
        try:
            raw = tool.handler(arguments, context)
            if hasattr(raw, "__await__"):
                raise TypeError("Async tool handlers are not supported by the synchronous registry.")
            safe_data = _safe_value(raw, secret_values)
            summary = _summary(safe_data)
            return self._finish(
                canonical_name, risk, decision, True, "success", summary, None, started,
                tool.side_effects, structured_data=safe_data, approval_result=approval_result,
                secret_values=secret_values,
            )
        except Exception as error:
            safe_error = _redact_text(str(error), secret_values)[:500]
            return self._finish(
                canonical_name, risk, decision, False, "error",
                f"Tool failed ({type(error).__name__}).", type(error).__name__.upper(), started,
                tool.side_effects, structured_data={"message": safe_error},
                approval_result=approval_result, secret_values=secret_values,
            )

    def _finish(
        self,
        name: str,
        risk: RiskLevel,
        decision: ApprovalDecision,
        success: bool,
        status: str,
        summary: str,
        error_code: str | None,
        started: float,
        side_effects: str,
        *,
        structured_data: Any = None,
        requires_followup: bool = False,
        approval_result: str | None = None,
        secret_values: tuple[str, ...] = (),
    ) -> ToolResult:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        safe_summary = _redact_text(summary, secret_values)
        receipt = ToolReceipt(
            datetime.now(timezone.utc).isoformat(), name, risk,
            approval_result or decision.value, success, elapsed, side_effects,
        )
        with self._lock:
            self._receipts.append(receipt)
        return ToolResult(
            success, name, status, safe_summary,
            _safe_value(structured_data, secret_values), error_code, elapsed, side_effects,
            requires_followup, {"receipt": _safe_value(receipt, secret_values)},
        )

    @classmethod
    def _validate_schema_shape(cls, schema: dict[str, Any], depth: int = 0) -> None:
        if not isinstance(schema, dict):
            raise ToolRegistrationError("Tool input schema entries must be objects.")
        if depth > 16:
            raise ToolRegistrationError("Tool input schema is too deeply nested.")
        if set(schema) - _SUPPORTED_SCHEMA_KEYS:
            unsupported = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYS)[0]
            raise ToolRegistrationError(f"Unsupported JSON Schema keyword: {unsupported}")
        schema_type = schema.get("type")
        allowed_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
        if schema_type is not None and (not isinstance(schema_type, str) or schema_type not in allowed_types):
            raise ToolRegistrationError("Tool schema contains an unsupported value type.")
        if "enum" in schema and not isinstance(schema["enum"], list):
            raise ToolRegistrationError("Tool schema enum must be an array.")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
            raise ToolRegistrationError("Tool schema properties must be an object.")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            raise ToolRegistrationError("Tool schema required fields must be names.")
        if any(key not in properties for key in required):
            raise ToolRegistrationError("Tool schema requires an undefined property.")
        for child in properties.values():
            if not isinstance(child, dict):
                raise ToolRegistrationError("Tool property schemas must be objects.")
            cls._validate_schema_shape(child, depth + 1)
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, dict):
                raise ToolRegistrationError("Array items schema must be an object.")
            cls._validate_schema_shape(items, depth + 1)
        extra = schema.get("additionalProperties", False)
        if isinstance(extra, dict):
            cls._validate_schema_shape(extra, depth + 1)
        elif not isinstance(extra, bool):
            raise ToolRegistrationError("additionalProperties must be a boolean or schema.")

    @classmethod
    def _validate_arguments(cls, arguments: Any, schema: dict[str, Any]) -> None:
        if not isinstance(arguments, dict) or any(not isinstance(k, str) for k in arguments):
            raise ValueError("Tool arguments must be an object with string keys.")
        try:
            encoded = json.dumps(arguments, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("Tool arguments must contain only finite JSON values.") from None
        if len(encoded) > 100_000:
            raise ValueError("Tool arguments exceed the 100 KB limit.")
        cls._validate_value(arguments, schema, "arguments", 0)

    @classmethod
    def _validate_value(cls, value: Any, schema: dict[str, Any], path: str, depth: int) -> None:
        if depth > 16:
            raise ValueError(f"{path} is too deeply nested.")
        kind = schema.get("type")
        valid = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: type(v) is int,
            "number": lambda v: type(v) in (int, float) and math.isfinite(v),
            "boolean": lambda v: type(v) is bool,
            "null": lambda v: v is None,
        }
        if kind in valid and not valid[kind](value):
            raise ValueError(f"{path} must be {kind}.")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} has an unsupported value.")
        if "const" in schema and value != schema["const"]:
            raise ValueError(f"{path} has an unsupported value.")
        if kind == "object":
            properties = schema.get("properties", {})
            missing = set(schema.get("required", ())) - set(value)
            if missing:
                raise ValueError(f"{path} is missing required fields: {', '.join(sorted(missing))}.")
            additional = schema.get("additionalProperties", False)
            unexpected = set(value) - set(properties)
            if unexpected and additional is False:
                raise ValueError(f"{path} contains unexpected fields: {', '.join(sorted(unexpected))}.")
            for key, child_value in value.items():
                child_schema = properties.get(key, additional if isinstance(additional, dict) else None)
                if child_schema is not None:
                    cls._validate_value(child_value, child_schema, f"{path}.{key}", depth + 1)
        elif kind == "array":
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
                raise ValueError(f"{path} has an invalid number of items.")
            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(value):
                    cls._validate_value(item, item_schema, f"{path}[{index}]", depth + 1)
        elif kind == "string":
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
                raise ValueError(f"{path} has an invalid length.")
        elif kind in {"integer", "number"}:
            if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
                raise ValueError(f"{path} is outside the allowed range.")


def _secret_values(value: Any, seen: set[int] | None = None, depth: int = 0) -> tuple[str, ...]:
    if depth > 16:
        return ()
    seen = seen if seen is not None else set()
    found: set[str] = set()
    if isinstance(value, Mapping):
        if id(value) in seen:
            return ()
        seen.add(id(value))
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)) and isinstance(item, (str, int, float)):
                text = str(item)
                if len(text) >= 3:
                    found.add(text)
            else:
                found.update(_secret_values(item, seen, depth + 1))
    elif isinstance(value, (list, tuple)):
        if id(value) in seen:
            return ()
        seen.add(id(value))
        for item in value:
            found.update(_secret_values(item, seen, depth + 1))
    return tuple(sorted(found, key=len, reverse=True))


def _redact_text(text: str, secret_values: tuple[str, ...]) -> str:
    for secret in secret_values:
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)


def _safe_value(value: Any, secret_values: tuple[str, ...] = ()) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            safe_key = str(key)[:120]
            output[safe_key] = "[REDACTED]" if _SECRET_KEY.search(safe_key) else _safe_value(item, secret_values)
        return output
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, secret_values) for item in value[:1000]]
    if isinstance(value, str):
        return _redact_text(value[:20_000], secret_values)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    return f"<{type(value).__name__}>"


def _summary(value: Any) -> str:
    if isinstance(value, str):
        return value or "Completed."
    if isinstance(value, list):
        return f"Completed; {len(value)} result(s)."
    if isinstance(value, (int, float, bool)):
        return str(value)
    if value is None:
        return "Completed."
    return "Completed."


def _approval_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    safe_arguments = _safe_value(arguments, _secret_values(arguments))
    rendered = json.dumps(safe_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(rendered) > 240:
        rendered = rendered[:237] + "..."
    return f"Run {tool_name} with {rendered}"
