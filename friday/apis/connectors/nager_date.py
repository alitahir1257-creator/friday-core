"""Community public-holiday data from the no-key Nager.Date API v4."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from friday.apis.client import JsonApiClient
from friday.apis.connectors.base import BaseApiConnector
from friday.apis.models import ApiCapability, ApiConnectorMetadata, ApiError, ApiRequest


_COUNTRY_CODE = re.compile(r"[A-Z]{2}\Z")


class NagerDateConnector(BaseApiConnector):
    def __init__(self, client: JsonApiClient) -> None:
        super().__init__(client, ApiConnectorMetadata(
            connector_id="nager_date",
            provider="Nager.Date Community API",
            capability=ApiCapability.HOLIDAYS,
            base_url="https://nagerholidays.com/api/v4",
            auth_type="none",
            free_status="Free for private/non-profit use; commercial API use requires active sponsorship.",
            network_required=True,
            timeout_seconds=6,
            rate_limit_notes="Provider documentation states no request limits; service availability and accuracy are not warranted.",
            privacy_notes="Sends only an ISO country code and year; the v4 public API returns English holiday names.",
        ))

    def _call(self, operation: str, arguments: dict[str, Any]):
        if operation != "list":
            raise ApiError("INVALID_ARGUMENT", "Holiday operation must be list.")
        country = arguments.get("country_code")
        if not isinstance(country, str) or not _COUNTRY_CODE.fullmatch(country.strip().upper()):
            raise ApiError("INVALID_ARGUMENT", "An ISO 3166-1 alpha-2 country code is required.")
        country = country.strip().upper()
        year = arguments.get("year")
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= date.today().year + 5:
            raise ApiError("INVALID_ARGUMENT", "Holiday year must be from 2000 through five years ahead.")
        fetched = self._fetch(ApiRequest(
            "nager_holidays", f"/api/v4/Holidays/{country}/{year}", {}, cache_ttl_seconds=21_600,
        ))
        if not isinstance(fetched.data, list) or len(fetched.data) > 100:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Nager.Date returned an invalid holiday list.")
        holidays: list[dict[str, Any]] = []
        for item in fetched.data:
            if not isinstance(item, dict):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Nager.Date returned an invalid holiday record.")
            holiday_date = item.get("date")
            name = item.get("name")
            types = item.get("holidayTypes")
            subdivisions = item.get("subdivisionCodes")
            if (not isinstance(holiday_date, str) or not isinstance(name, str)
                    or not isinstance(item.get("nationalHoliday"), bool)
                    or not isinstance(item.get("countryCode"), str)
                    or item["countryCode"].upper() != country
                    or not isinstance(types, list) or len(types) > 8
                    or any(not isinstance(kind, str) or len(kind) > 40 for kind in types)
                    or (subdivisions is not None and (
                        not isinstance(subdivisions, list) or len(subdivisions) > 100
                        or any(not isinstance(code, str) or len(code) > 80 for code in subdivisions)
                    ))):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Nager.Date returned an invalid holiday record.")
            try:
                date.fromisoformat(holiday_date)
            except ValueError:
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Nager.Date returned an invalid holiday date.") from None
            holidays.append({
                "date": holiday_date,
                "name": name[:160],
                "country_code": country,
                "national_holiday": item["nationalHoliday"],
                "subdivision_codes": subdivisions,
                "holiday_types": types,
            })
        return {"country_code": country, "year": year, "holidays": holidays}, (fetched.source,), None, fetched.from_network
