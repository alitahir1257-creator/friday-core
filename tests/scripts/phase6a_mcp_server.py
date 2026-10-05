"""Harmless, test-only MCP server for local stdio acceptance checks."""

from __future__ import annotations

import json
import os
import sys


_TOOLS = [
    {
        "name": "echo",
        "description": "Return the supplied text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 1000}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "minimum": -1000000, "maximum": 1000000},
                "b": {"type": "integer", "minimum": -1000000, "maximum": 1000000},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_status",
        "description": "Return harmless test-process status.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "not_allowed",
        "description": "A harmless tool omitted from the FRIDAY allowlist.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]
_calls = {"get_status": 0}
_META_PREFIX = "io.modelcontextprotocol/"


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: int, value: dict, *, modern: bool) -> None:
    if modern:
        value.setdefault("resultType", "complete")
    _send({"jsonrpc": "2.0", "id": request_id, "result": value})


def main() -> None:
    legacy = "--legacy" in sys.argv[1:]
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not legacy and method not in {"initialize", "notifications/initialized"}:
            meta = params.get("_meta", {})
            if meta.get(_META_PREFIX + "protocolVersion") != "2026-07-28":
                _send({"jsonrpc": "2.0", "id": request_id,
                       "error": {"code": -32602, "message": "Modern request metadata is required."}})
                continue
        if not legacy and method == "server/discover":
            _result(request_id, {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "cacheScope": "public",
                "ttlMs": 0,
            }, modern=True)
        elif legacy and method == "server/discover":
            _send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32601, "message": "Method not found."}})
        elif not legacy and method == "initialize":
            _send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32601, "message": "Method not found."}})
        elif legacy and method == "initialize":
            _result(request_id, {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "friday-phase6a-test-legacy", "version": "1"},
            }, modern=False)
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _result(request_id, {"tools": _TOOLS}, modern=not legacy)
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                text = arguments["text"]
                _result(request_id, {"content": [{"type": "text", "text": text}]}, modern=not legacy)
            elif name == "add":
                total = arguments["a"] + arguments["b"]
                _result(request_id, {"content": [{"type": "text", "text": str(total)}]}, modern=not legacy)
            elif name == "get_status":
                _calls[name] += 1
                status = {
                    "parent_marker_visible": os.environ.get("FRIDAY_PHASE6A_SYNTHETIC_SECRET") is not None,
                    "calls": _calls[name],
                }
                _result(request_id, {
                    "content": [{"type": "text", "text": json.dumps(status)}],
                    "structuredContent": status,
                }, modern=not legacy)
            elif name == "not_allowed":
                _result(request_id, {"content": [{"type": "text", "text": "not allowlisted"}]}, modern=not legacy)
            else:
                _send({"jsonrpc": "2.0", "id": request_id,
                       "error": {"code": -32602, "message": "Unknown harmless test tool."}})
        elif method == "shutdown" and legacy:
            _result(request_id, {}, modern=False)
        elif method == "exit":
            return


if __name__ == "__main__":
    main()
