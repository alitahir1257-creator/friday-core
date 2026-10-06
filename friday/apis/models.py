"""Small contracts for vetted, read-only public API connectors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol


class ApiCapability(str, Enum):
    WEATHER = "weather"
    CURRENCY = "currency"
    HOLIDAYS = "holidays"


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ApiRequest:
    endpoint_id: str
    path: str
    params: Mapping[str, str | int | float]
    cache_ttl_seconds: float = 0


@dataclass(frozen=True)
class ApiFetch:
    data: Any
    source: str
    from_network: bool


@dataclass(frozen=True)
class ApiResponse:
    success: bool
    provider: str
    capability: str
    requested_at: str
    source: str
    data: Any = None
    provider_updated_at: str | None = None
    error_code: str | None = None
    from_network: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApiConnectorMetadata:
    connector_id: str
    provider: str
    capability: ApiCapability
    base_url: str
    auth_type: str
    free_status: str
    network_required: bool
    timeout_seconds: float
    rate_limit_notes: str
    privacy_notes: str
    enabled: bool = True
    health: str = "unknown"
    last_success: str | None = None
    last_failure: str | None = None


class ApiConnector(Protocol):
    metadata: ApiConnectorMetadata

    def call(self, operation: str, arguments: dict[str, Any], *, privacy: str = "public") -> ApiResponse:
        """Run one bounded connector operation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
