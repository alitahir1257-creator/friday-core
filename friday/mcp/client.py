"""Bounded local stdio MCP lifecycle using only Python's standard library."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import json
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any

from friday.mcp.models import MCPServerConfig, MCPToolSpec
from friday.tools.models import RiskLevel, ToolDefinition
from friday.tools.registry import ToolRegistrationError, ToolRegistry


_PROTOCOL_VERSION = "2026-07-28"
_SUPPORTED_PROTOCOL_VERSIONS = {"2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_PROTOCOL_META_PREFIX = "io.modelcontextprotocol/"
_MCP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_SECRET_TEXT = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")
_SECRET_ENV_NAME = re.compile(r"(?i)(password|secret|token|authorization|cookie|api[_-]?key|credential)")
_SECRET_FIELD = _SECRET_ENV_NAME
_MAX_PROTOCOL_LINE = 2_000_000
_MAX_DISCOVERED_TOOLS = 256


class MCPClientError(RuntimeError):
    """A local MCP process failed or returned an invalid protocol message."""


class _MCPMethodNotFound(MCPClientError):
    """The peer does not implement a requested protocol method."""


class MCPStdioClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.discovered_tools: tuple[MCPToolSpec, ...] = ()
        self.last_error: str | None = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        self._stderr: deque[str] = deque(maxlen=20)
        self._rpc_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._request_id = 0
        self._available = False
        self._closing = False
        self._failure_notified = False
        self._unavailable_event = threading.Event()
        self._failure_callback: Callable[[], None] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._secret_values: tuple[str, ...] = ()
        self._modern_protocol = True
        self._protocol_version = _PROTOCOL_VERSION

    @property
    def is_available(self) -> bool:
        with self._state_lock:
            return self._available and self.process is not None and self.process.poll() is None

    def wait_unavailable(self, timeout: float) -> bool:
        return self._unavailable_event.wait(timeout)

    def set_failure_callback(self, callback: Callable[[], None]) -> None:
        self._failure_callback = callback
        if self._unavailable_event.is_set():
            self._notify_failure()

    def connect(self) -> tuple[MCPToolSpec, ...]:
        if not self.config.enabled:
            return ()
        if self.process is not None:
            raise MCPClientError("MCP client has already been started.")
        environment = self._child_environment()
        self.process = subprocess.Popen(
            [self.config.command, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            shell=False,
            close_fds=True,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        self._secret_values = tuple(
            sorted(
                {
                    environment[name]
                    for name in environment
                    if environment.get(name) and _SECRET_ENV_NAME.search(name)
                } | {
                    environment[name]
                    for name in self.config.environment_refs
                    if environment.get(name)
                },
                key=len,
                reverse=True,
            )
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, name=f"mcp-{self.config.name}-stdout", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"mcp-{self.config.name}-stderr", daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()
        try:
            try:
                discovered = self._request(
                    "server/discover", {}, self.config.startup_timeout_seconds,
                )
            except _MCPMethodNotFound:
                self._modern_protocol = False
                initialized = self._request(
                    "initialize",
                    {
                        "protocolVersion": _LEGACY_PROTOCOL_VERSIONS[0],
                        "capabilities": {},
                        "clientInfo": {"name": "friday-core", "version": "6A"},
                    },
                    self.config.startup_timeout_seconds,
                )
                selected_version = initialized.get("protocolVersion")
                if selected_version not in _LEGACY_PROTOCOL_VERSIONS:
                    raise MCPClientError("MCP server selected an unsupported protocol version.")
                self._protocol_version = selected_version
                self._notify("notifications/initialized", {})
            else:
                versions = discovered.get("supportedVersions")
                if not isinstance(versions, list):
                    raise MCPClientError("MCP server discovery returned invalid protocol versions.")
                common = [
                    version for version in versions
                    if isinstance(version, str) and version in _SUPPORTED_PROTOCOL_VERSIONS
                ]
                if not common:
                    raise MCPClientError("MCP server has no protocol version in common with FRIDAY.")
                self._protocol_version = max(common)
            specs = []
            cursor = None
            for _page in range(10):
                response = self._request(
                    "tools/list", {"cursor": cursor} if cursor else {},
                    self.config.startup_timeout_seconds,
                )
                raw_tools = response.get("tools", [])
                if not isinstance(raw_tools, list):
                    raise MCPClientError("MCP tools/list returned an invalid tool list.")
                for item in raw_tools:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                        continue
                    schema = item.get("inputSchema")
                    if not isinstance(schema, dict):
                        continue
                    specs.append(MCPToolSpec(
                        item["name"], str(item.get("description") or "")[:500], schema,
                    ))
                    if len(specs) >= _MAX_DISCOVERED_TOOLS:
                        break
                cursor = response.get("nextCursor")
                if not cursor or len(specs) >= _MAX_DISCOVERED_TOOLS:
                    break
            if cursor and len(specs) < _MAX_DISCOVERED_TOOLS:
                raise MCPClientError("MCP tool discovery exceeded the page limit.")
            self.discovered_tools = tuple(specs)
            with self._state_lock:
                if self._unavailable_event.is_set():
                    raise MCPClientError("MCP process exited during initialization.")
                self._available = True
            return self.discovered_tools
        except Exception:
            self._mark_unavailable("MCP startup failed.")
            self._stop_process(force=True)
            raise

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if not self.is_available:
            raise MCPClientError("MCP server is unavailable.")
        response = self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            self.config.timeout_seconds,
        )
        if response.get("isError") is True:
            text = " ".join(
                item.get("text", "") for item in response.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            )
            raise MCPClientError(self._safe_diagnostic(text or "MCP tool returned an error."))
        if "structuredContent" in response:
            return self._redact_output(response["structuredContent"])
        content = response.get("content", [])
        texts = [
            self._safe_diagnostic(
                item.get("text", "") if isinstance(item.get("text", ""), str)
                else str(item.get("text", ""))
            )
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if len(texts) == 1:
            text = texts[0]
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text
        return texts

    def disconnect(self) -> None:
        process = self.process
        if process is None:
            return
        self._closing = True
        if process.poll() is None and self.is_available and not self._modern_protocol:
            try:
                self._request("shutdown", {}, min(1.0, self.config.timeout_seconds))
            except Exception:
                pass
            try:
                self._notify("exit", {})
            except Exception:
                pass
        self._stop_process(force=False)
        self._mark_unavailable("MCP client disconnected.", notify=False)

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._rpc_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                self._mark_unavailable("MCP process exited.")
                raise MCPClientError("MCP server is unavailable.")
            self._request_id += 1
            request_id = self._request_id
            if self._modern_protocol:
                params = dict(params)
                params["_meta"] = {
                    _PROTOCOL_META_PREFIX + "protocolVersion": self._protocol_version,
                    _PROTOCOL_META_PREFIX + "clientInfo": {"name": "friday-core", "version": "6A"},
                    _PROTOCOL_META_PREFIX + "clientCapabilities": {},
                }
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_unavailable(f"MCP {method} timed out.")
                    self._stop_process(force=True)
                    raise MCPClientError(f"MCP {method} timed out.")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty:
                    self._mark_unavailable(f"MCP {method} timed out.")
                    self._stop_process(force=True)
                    raise MCPClientError(f"MCP {method} timed out.") from None
                if message is None:
                    raise MCPClientError(self.last_error or "MCP process closed its output.")
                if "method" in message:
                    if "id" in message:
                        self._write({
                            "jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "Client request is unsupported."},
                        })
                    continue
                if message.get("id") != request_id:
                    continue
                error = message.get("error")
                if error is not None:
                    if isinstance(error, dict) and error.get("code") == -32601:
                        raise _MCPMethodNotFound("MCP server does not implement this method.")
                    text = error.get("message", "MCP request failed.") if isinstance(error, dict) else "MCP request failed."
                    raise MCPClientError(self._safe_diagnostic(str(text)))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise MCPClientError("MCP returned an invalid JSON-RPC result.")
                return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MCPClientError("MCP server is unavailable.")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as error:
            self._mark_unavailable("MCP input pipe closed.")
            raise MCPClientError("MCP server is unavailable.") from error

    def _read_stdout(self) -> None:
        process = self.process
        try:
            if process is None or process.stdout is None:
                return
            while True:
                line = process.stdout.readline(_MAX_PROTOCOL_LINE + 1)
                if not line:
                    break
                if len(line) > _MAX_PROTOCOL_LINE or not line.endswith("\n"):
                    self._mark_unavailable("MCP server exceeded the protocol message size limit.")
                    if process.poll() is None:
                        process.terminate()
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    self._mark_unavailable("MCP server wrote invalid protocol output.")
                    if process.poll() is None:
                        process.terminate()
                    break
                if not isinstance(message, dict):
                    self._mark_unavailable("MCP server wrote an invalid message.")
                    if process.poll() is None:
                        process.terminate()
                    break
                try:
                    self._messages.put_nowait(message)
                except queue.Full:
                    self._mark_unavailable("MCP server exceeded the pending message limit.")
                    if process.poll() is None:
                        process.terminate()
                    break
        except (OSError, ValueError):
            self._mark_unavailable("MCP output pipe failed.")
        finally:
            try:
                self._messages.put_nowait(None)
            except queue.Full:
                pass
            if not self._closing:
                self._mark_unavailable("MCP process closed its output.")

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = process.stderr.readline(4096)
                if not line:
                    break
                self._stderr.append(self._safe_diagnostic(line.rstrip())[:300])
        except (OSError, ValueError):
            return

    def _child_environment(self) -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        allowed.update(self.config.environment_allowlist)
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        for child_name, parent_name in self.config.environment_refs.items():
            if parent_name in os.environ:
                environment[child_name] = os.environ[parent_name]
        return environment

    def _safe_diagnostic(self, message: str) -> str:
        for secret in self._secret_values:
            message = message.replace(secret, "[REDACTED]")
        message = _SECRET_TEXT.sub(r"\1[REDACTED]", message)
        return message[:500]

    def _redact_output(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key)[:120]: "[REDACTED]" if _SECRET_FIELD.search(str(key))
                else self._redact_output(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_output(item) for item in value[:1000]]
        if isinstance(value, str):
            return self._safe_diagnostic(value)
        return value

    def _mark_unavailable(self, reason: str, *, notify: bool = True) -> None:
        with self._state_lock:
            self._available = False
            if self.last_error is None:
                detail = " ".join(self._stderr)
                self.last_error = self._safe_diagnostic(reason + (f" {detail}" if detail else ""))[:1000]
        self._unavailable_event.set()
        if notify:
            self._notify_failure()

    def _notify_failure(self) -> None:
        callback = None
        with self._state_lock:
            if self._failure_notified or self._closing or self._failure_callback is None:
                return
            self._failure_notified = True
            callback = self._failure_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                return

    def _stop_process(self, *, force: bool) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None and force:
            process.terminate()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)


class MCPClientManager:
    """Own explicit MCP clients and register only locally allowlisted tools."""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry
        self.clients: dict[str, MCPStdioClient] = {}
        self.errors: dict[str, str] = {}
        self._tool_names: dict[str, tuple[str, ...]] = {}

    def connect(self, config: MCPServerConfig) -> tuple[str, ...]:
        if not config.enabled:
            return ()
        if config.name in self.clients:
            raise ValueError(f"MCP server already configured: {config.name}")
        client = MCPStdioClient(config)
        try:
            discovered = client.connect()
        except Exception as error:
            self.errors[config.name] = type(error).__name__
            client.disconnect()
            return ()

        self.clients[config.name] = client
        names: list[str] = []
        for remote in discovered:
            if remote.name not in config.allowed_tools or remote.name in config.blocked_tools:
                continue
            if not _MCP_NAME.fullmatch(remote.name):
                continue
            capabilities = config.capability_map.get(remote.name, frozenset({"mcp"}))
            tool = ToolDefinition(
                name=remote.name.casefold(),
                namespace=f"mcp.{config.name.casefold()}",
                description=remote.description or f"Call {remote.name} on local MCP server {config.name}.",
                input_schema=remote.input_schema,
                handler=lambda args, _context, tool_name=remote.name: client.call_tool(tool_name, args),
                capabilities=frozenset(capabilities),
                risk_level=config.risk_map.get(remote.name, RiskLevel.CONSEQUENTIAL.value),
                privacy_support=config.privacy_support,
                source=f"mcp:{config.name}",
                timeout_seconds=config.timeout_seconds,
                side_effects="local_mcp",
                transmits_data=config.transmits_data,
                metadata={"mcp_server": config.name, "mcp_tool": remote.name},
            )
            try:
                self.tool_registry.register(tool)
            except (ToolRegistrationError, TypeError, ValueError):
                continue
            names.append(tool.canonical_name)

        self._tool_names[config.name] = tuple(names)
        client.set_failure_callback(lambda server=config.name: self._mark_failed(server))
        self.errors.pop(config.name, None)
        return tuple(names)

    def disconnect(self, name: str) -> None:
        client = self.clients.pop(name, None)
        for tool_name in self._tool_names.pop(name, ()):
            if self.tool_registry.get(tool_name) is not None:
                self.tool_registry.disable(tool_name)
        if client is not None:
            client.disconnect()
        self.errors[name] = "DISCONNECTED"

    def disconnect_all(self) -> None:
        for name in tuple(self.clients):
            self.disconnect(name)

    def _mark_failed(self, name: str) -> None:
        self.errors[name] = "UNAVAILABLE"
        for tool_name in self._tool_names.get(name, ()):
            if self.tool_registry.get(tool_name) is not None:
                self.tool_registry.disable(tool_name)
