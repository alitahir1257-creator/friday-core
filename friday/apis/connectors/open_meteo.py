"""Open-Meteo geocoding and small current/forecast responses."""

from __future__ import annotations

import math
import re
from typing import Any

from friday.apis.client import JsonApiClient
from friday.apis.connectors.base import BaseApiConnector
from friday.apis.models import ApiCapability, ApiConnectorMetadata, ApiError, ApiRequest
from friday.research.planner import ResearchPlanner


_SAFE_PLACE = re.compile(r"[\w\s.,'()\-]{2,120}\Z", re.UNICODE)


class OpenMeteoConnector(BaseApiConnector):
    def __init__(self, client: JsonApiClient) -> None:
        super().__init__(client, ApiConnectorMetadata(
            connector_id="open_meteo",
            provider="Open-Meteo",
            capability=ApiCapability.WEATHER,
            base_url="https://api.open-meteo.com/v1/forecast",
            auth_type="none",
            free_status="Free for non-commercial use; paid plans are required for commercial API use.",
            network_required=True,
            timeout_seconds=6,
            rate_limit_notes="Free tier: 600/minute, 5,000/hour, 10,000/day, 300,000/month; no uptime guarantee.",
            privacy_notes="Sends only the requested place name to geocoding and resolved coordinates to forecast.",
        ))

    def _call(self, operation: str, arguments: dict[str, Any]):
        if operation not in {"current", "forecast"}:
            raise ApiError("INVALID_ARGUMENT", "Weather operation must be current or forecast.")
        days = arguments.get("days", 3)
        if operation == "forecast" and (
            isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7
        ):
            raise ApiError("INVALID_ARGUMENT", "Forecast days must be from 1 to 7.")
        place = arguments.get("city")
        if not isinstance(place, str):
            raise ApiError("INVALID_ARGUMENT", "A city or place name is required.")
        place = place.strip().strip("?.! ")
        safe_place = ResearchPlanner.public_query(place, privacy="public")
        if (not _SAFE_PLACE.fullmatch(place) or not safe_place
                or safe_place.casefold() != place.casefold()
                or re.search(r"\b(?:my|our|home|work|here|current location)\b", place, re.I)):
            raise ApiError("PRIVACY_BLOCKED", "Enter a public city or place name only.")

        location = self._fetch(ApiRequest(
            "open_meteo_geocoding", "/v1/search",
            {"name": place, "count": 1, "language": "en", "format": "json"},
            cache_ttl_seconds=3600,
        ))
        results = location.data.get("results") if isinstance(location.data, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ApiError("NOT_FOUND", "Open-Meteo did not find that public place.")
        match = results[0]
        latitude = self._coordinate(match.get("latitude"), -90, 90)
        longitude = self._coordinate(match.get("longitude"), -180, 180)
        if latitude is None or longitude is None:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned invalid location coordinates.")

        if operation == "current":
            request = ApiRequest(
                "open_meteo_weather", "/v1/forecast",
                {"latitude": latitude, "longitude": longitude,
                 "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m,weather_code",
                 "timezone": "auto"},
                cache_ttl_seconds=120,
            )
        elif operation == "forecast":
            request = ApiRequest(
                "open_meteo_weather", "/v1/forecast",
                {"latitude": latitude, "longitude": longitude,
                 "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                 "forecast_days": days, "timezone": "auto"},
                cache_ttl_seconds=600,
            )
        weather = self._fetch(request)
        if not isinstance(weather.data, dict):
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned an invalid forecast shape.")
        output: dict[str, Any] = {
            "location": {
                "name": self._short_text(match.get("name"), 120),
                "country": self._short_text(match.get("country"), 80),
                "timezone": self._short_text(weather.data.get("timezone"), 80),
            },
        }
        updated_at = None
        if operation == "current":
            current = weather.data.get("current")
            if not isinstance(current, dict):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned no current conditions.")
            updated_at = self._short_text(current.get("time"), 40)
            output["current"] = {
                "time": updated_at,
                "temperature_2m": self._number(current.get("temperature_2m")),
                "relative_humidity_2m": self._number(current.get("relative_humidity_2m")),
                "apparent_temperature": self._number(current.get("apparent_temperature")),
                "precipitation": self._number(current.get("precipitation")),
                "wind_speed_10m": self._number(current.get("wind_speed_10m")),
                "weather_code": self._number(current.get("weather_code")),
                "units": self._units(weather.data.get("current_units"), (
                    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                    "precipitation", "wind_speed_10m", "weather_code",
                )),
            }
        else:
            daily = weather.data.get("daily")
            if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned no daily forecast.")
            dates = daily["time"]
            if not 1 <= len(dates) <= 7 or any(not isinstance(day, str) or len(day) != 10 for day in dates):
                raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned an invalid forecast range.")
            output["forecast"] = {
                "daily": {
                    "time": dates,
                    "weather_code": self._bounded_list(daily.get("weather_code"), len(dates)),
                    "temperature_2m_max": self._bounded_list(daily.get("temperature_2m_max"), len(dates)),
                    "temperature_2m_min": self._bounded_list(daily.get("temperature_2m_min"), len(dates)),
                    "precipitation_probability_max": self._bounded_list(daily.get("precipitation_probability_max"), len(dates)),
                },
                "units": self._units(weather.data.get("daily_units"), (
                    "weather_code", "temperature_2m_max", "temperature_2m_min",
                    "precipitation_probability_max",
                )),
            }
            updated_at = dates[0]
        sources = tuple(dict.fromkeys((location.source, weather.source)))
        return output, sources, updated_at, location.from_network or weather.from_network

    @staticmethod
    def _coordinate(value: Any, minimum: float, maximum: float) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return float(value) if minimum <= value <= maximum else None

    @staticmethod
    def _number(value: Any) -> float | int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _short_text(value: Any, max_length: int) -> str | None:
        return value[:max_length] if isinstance(value, str) and value else None

    @classmethod
    def _bounded_list(cls, value: Any, length: int) -> list[float | int | None]:
        if not isinstance(value, list) or len(value) != length:
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned an invalid forecast series.")
        return [cls._number(item) for item in value]

    @staticmethod
    def _units(value: Any, names: tuple[str, ...]) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ApiError("INVALID_PROVIDER_RESPONSE", "Open-Meteo returned invalid units metadata.")
        return {
            name: unit[:24]
            for name in names
            if isinstance((unit := value.get(name)), str)
        }
