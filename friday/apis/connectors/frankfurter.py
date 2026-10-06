"""Daily reference exchange rates from Frankfurter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

from friday.apis.client import JsonApiClient
from friday.apis.connectors.base import BaseApiConnector
from friday.apis.models import ApiCapability, ApiConnectorMetadata, ApiError, ApiRequest


_CURRENCY = re.compile(r"[A-Z]{3}\Z")


class FrankfurterConnector(BaseApiConnector):
    def __init__(self, client: JsonApiClient) -> None:
        super().__init__(client, ApiConnectorMetadata(
            connector_id="frankfurter",
            provider="Frankfurter",
            capability=ApiCapability.CURRENCY,
            base_url="https://api.frankfurter.dev/v2",
            auth_type="none",
            free_status="Free for personal and commercial use; upstream rate-provider terms apply.",
            network_required=True,
            timeout_seconds=6,
            rate_limit_notes="No daily or monthly quota is published; abusive traffic is rate-limited.",
            privacy_notes="Sends only the requested currency codes; the provider says it does not log request URLs or IP addresses.",
        ))

    def _call(self, operation: str, arguments: dict[str, Any]):
        if operation not in {"rates", "convert"}:
            raise ApiError("INVALID_ARGUMENT", "Currency operation must be rates or convert.")
        amount = None
        if operation == "convert":
            amount = arguments.get("amount")
            if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                    or not math.isfinite(amount) or not 0 < amount <= 1_000_000_000_000):
                raise ApiError("INVALID_ARGUMENT", "Amount must be a finite number greater than zero and at most 1 trillion.")
        base = self._currency(arguments.get("base"))
        quote = self._currency(arguments.get("quote"))
        fetched = self._fetch(ApiRequest(
            "frankfurter", f"/v2/rate/{base}/{quote}", {}, cache_ttl_seconds=3600,
        ))
        payload = fetched.data
        if not isinstance(payload, dict):
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Frankfurter returned an invalid rate shape.")
        if payload.get("base") != base or payload.get("quote") != quote:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Frankfurter returned a different currency pair.")
        rate = payload.get("rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Frankfurter returned an invalid exchange rate.")
        provider_date = payload.get("date")
        if not isinstance(provider_date, str):
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Frankfurter returned no rate date.")
        try:
            date.fromisoformat(provider_date)
        except ValueError:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Frankfurter returned an invalid rate date.") from None
        data: dict[str, Any] = {"date": provider_date, "base": base, "quote": quote, "rate": rate}
        if operation == "convert":
            try:
                converted = Decimal(str(amount)) * Decimal(str(rate))
                converted_float = float(converted)
            except InvalidOperation:
                raise ApiError("INVALID_ARGUMENT", "Amount cannot be converted safely.") from None
            except (OverflowError, ValueError):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "The converted amount is outside its numeric range.") from None
            if not math.isfinite(converted_float):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "The converted amount is outside its numeric range.")
            data["amount"] = amount
            data["converted_amount"] = converted_float
        return data, (fetched.source,), provider_date, fetched.from_network

    @staticmethod
    def _currency(value: Any) -> str:
        if not isinstance(value, str):
            raise ApiError("INVALID_ARGUMENT", "Three-letter ISO currency codes are required.")
        code = value.strip().upper()
        if not _CURRENCY.fullmatch(code):
            raise ApiError("INVALID_ARGUMENT", "Three-letter ISO currency codes are required.")
        return code
