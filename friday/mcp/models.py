"""Explicit, secret-free configuration for a local stdio MCP server."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping


_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$", re.I)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    transport: str = "stdio"
    enabled: bool = True
    allowed_tools: frozenset[str] = frozenset()
    blocked_tools: frozenset[str] = frozenset()
    environment_allowlist: frozenset[str] = frozenset()
    environment_refs: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    startup_timeout_seconds: float = 10.0
    risk_map: Mapping[str, str] = field(default_factory=dict)
    capability_map: Mapping[str, frozenset[str]] = field(default_factory=dict)
    privacy_support: frozenset[str] = frozenset({"public"})
    transmits_data: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("MCP server name must be a simple identifier.")
        object.__setattr__(self, "name", self.name.casefold())
        if self.transport != "stdio":
            raise ValueError("Only explicitly configured local stdio MCP is supported.")
        if not isinstance(self.command, str) or not self.command or "\x00" in self.command:
            raise ValueError("MCP command must be one executable name or path.")
        if not isinstance(self.args, tuple) or not all(isinstance(arg, str) and "\x00" not in arg for arg in self.args):
            raise ValueError("MCP arguments must be separate strings without null bytes.")
        if not isinstance(self.enabled, bool) or not isinstance(self.transmits_data, bool):
            raise ValueError("MCP enabled and transmission settings must be booleans.")
        if not isinstance(self.allowed_tools, (set, frozenset)) or not isinstance(self.blocked_tools, (set, frozenset)):
            raise ValueError("MCP tool allow and block lists must be sets.")
        if not isinstance(self.environment_allowlist, (set, frozenset)) or not isinstance(self.environment_refs, Mapping):
            raise ValueError("MCP environment rules must use sets and a mapping.")
        if not isinstance(self.risk_map, Mapping) or not isinstance(self.capability_map, Mapping):
            raise ValueError("MCP risk and capability mappings are invalid.")
        if not all(
            isinstance(name, str) and _NAME.fullmatch(name)
            for name in self.allowed_tools | self.blocked_tools
        ):
            raise ValueError("MCP tool allowlist entries must be simple names.")
        if not all(isinstance(name, str) and _ENV_NAME.fullmatch(name) for name in self.environment_allowlist):
            raise ValueError("MCP environment allowlist contains an invalid name.")
        if any(
            not isinstance(child, str) or not isinstance(parent, str)
            or child not in self.environment_allowlist or not _ENV_NAME.fullmatch(child)
            or not _ENV_NAME.fullmatch(parent)
            for child, parent in self.environment_refs.items()
        ):
            raise ValueError("MCP environment references must target allowlisted names.")
        if not isinstance(self.privacy_support, (set, frozenset)) or not all(
            isinstance(level, str) for level in self.privacy_support
        ):
            raise ValueError("MCP privacy support must be a set of privacy tiers.")
        for value in (self.timeout_seconds, self.startup_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= value <= 300:
                raise ValueError("MCP timeouts must be between 0.1 and 300 seconds.")
        if not self.privacy_support <= {"public", "private", "local_only"}:
            raise ValueError("MCP privacy support is invalid.")
        for tool_name, capabilities in self.capability_map.items():
            if not isinstance(tool_name, str) or not isinstance(capabilities, (set, frozenset)) or not all(
                isinstance(capability, str) and capability.strip() for capability in capabilities
            ):
                raise ValueError("MCP capability mappings are invalid.")


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict
