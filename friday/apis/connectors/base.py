"""Shared response handling for the vetted public API connectors."""

from __future__ import annotations

from typing import Any

from friday.apis.client import JsonApiClient
from friday.apis.models import ApiConnectorMetadata, ApiError, ApiFetch, ApiRequest, ApiResponse, utc_now


class BaseApiConnector:
    metadata: ApiConnectorMetadata

    def __init__(self, client: JsonApiClient, metadata: ApiConnectorMetadata) -> None:
        self.client = client
        self.metadata = metadata

    def call(self, operation: str, arguments: dict[str, Any], *, privacy: str = "public") -> ApiResponse:
        requested_at = utc_now()
        if privacy != "public":
            return ApiResponse(
                False, self.metadata.provider, self.metadata.capability.value,
                requested_at, self.metadata.base_url, error_code="POLICY_BLOCKED",
            )
        try:
            data, sources, updated_at, from_network = self._call(operation, arguments)
        except ApiError as error:
            return ApiResponse(
                False, self.metadata.provider, self.metadata.capability.value,
                requested_at, self.metadata.base_url, error_code=error.code,
            )
        return ApiResponse(
            True, self.metadata.provider, self.metadata.capability.value,
            requested_at, "; ".join(sources), data=data,
            provider_updated_at=updated_at, from_network=from_network,
        )

    def _fetch(self, request: ApiRequest) -> ApiFetch:
        return self.client.get_json(request)

    def _call(self, operation: str, arguments: dict[str, Any]) -> tuple[Any, tuple[str, ...], str | None, bool]:
        raise NotImplementedError
