"""Registry and on-demand diagnostics for FRIDAY's vetted public APIs."""

from __future__ import annotations

from dataclasses import replace
import threading
from typing import Any

from friday.apis.client import JsonApiClient
from friday.apis.connectors.frankfurter import FrankfurterConnector
from friday.apis.connectors.nager_date import NagerDateConnector
from friday.apis.connectors.open_meteo import OpenMeteoConnector
from friday.apis.models import ApiConnector, ApiConnectorMetadata, ApiResponse, utc_now


class ApiRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, ApiConnector] = {}
        self._status: dict[str, dict[str, str | None]] = {}
        self._lock = threading.RLock()

    def register(self, connector: ApiConnector) -> None:
        metadata = connector.metadata
        if metadata.connector_id in self._connectors:
            raise ValueError("Public API connector is already registered.")
        with self._lock:
            self._connectors[metadata.connector_id] = connector
            self._status[metadata.connector_id] = {
                "health": "unknown", "last_success": None, "last_failure": None,
            }

    def get(self, connector_id: str) -> ApiConnector | None:
        with self._lock:
            return self._connectors.get(connector_id)

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._connectors))

    def metadata(self, connector_id: str) -> ApiConnectorMetadata | None:
        with self._lock:
            connector = self._connectors.get(connector_id)
            if connector is None:
                return None
            status = self._status[connector_id]
            return replace(connector.metadata, **status)

    def diagnostics(self) -> tuple[ApiConnectorMetadata, ...]:
        return tuple(item for connector_id in self.ids() if (item := self.metadata(connector_id)) is not None)

    def invoke(
        self,
        connector_id: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        privacy: str = "public",
    ) -> ApiResponse:
        connector = self.get(connector_id)
        if connector is None:
            return ApiResponse(False, "unknown", "unknown", utc_now(), "", error_code="CONNECTOR_NOT_FOUND")
        if not connector.metadata.enabled:
            return ApiResponse(
                False, connector.metadata.provider, connector.metadata.capability.value,
                utc_now(), connector.metadata.base_url, error_code="CONNECTOR_DISABLED",
            )
        if privacy != "public":
            return ApiResponse(
                False, connector.metadata.provider, connector.metadata.capability.value,
                utc_now(), connector.metadata.base_url, error_code="POLICY_BLOCKED",
            )
        try:
            result = connector.call(operation, arguments, privacy=privacy)
        except Exception:
            result = ApiResponse(
                False, connector.metadata.provider, connector.metadata.capability.value,
                utc_now(), connector.metadata.base_url, error_code="CONNECTOR_ERROR",
            )
        with self._lock:
            status = self._status[connector_id]
            status["health"] = "healthy" if result.success else "degraded"
            status["last_success" if result.success else "last_failure"] = result.requested_at
        return result


def build_default_api_registry(client: JsonApiClient | None = None) -> ApiRegistry:
    api_client = client or JsonApiClient()
    registry = ApiRegistry()
    for connector in (
        OpenMeteoConnector(api_client),
        FrankfurterConnector(api_client),
        NagerDateConnector(api_client),
    ):
        registry.register(connector)
    return registry
