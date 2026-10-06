"""Bounded JSON transport for the fixed public API endpoints FRIDAY ships."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from friday.apis.models import ApiError, ApiFetch, ApiRequest
from friday.research.fetch import ResearchFetchError, ResearchFetcher


_ENDPOINTS = {
    "open_meteo_geocoding": "https://geocoding-api.open-meteo.com",
    "open_meteo_weather": "https://api.open-meteo.com",
    "frankfurter": "https://api.frankfurter.dev",
    "nager_holidays": "https://nagerholidays.com",
}
_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,512}\Z")
_PARAMETER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")
_MAX_CACHE_ENTRIES = 64
_MAX_RESPONSE_BYTES = 262_144


class JsonApiClient:
    """Fetch JSON from a compile-time endpoint allowlist using the safe research transport."""

    def __init__(self, *, fetcher: ResearchFetcher | None = None) -> None:
        self._fetcher = fetcher or ResearchFetcher(
            timeout_seconds=6,
            max_bytes=_MAX_RESPONSE_BYTES,
            max_redirects=0,
            accepted_types=("application/json", "application/problem+json"),
            user_agent="FRIDAY-Public-API/1.0 (local read-only client)",
        )
        self._cache: OrderedDict[str, tuple[float, Any, str]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def get_json(self, request: ApiRequest, *, privacy: str = "public") -> ApiFetch:
        if privacy != "public":
            raise ApiError("POLICY_BLOCKED", "Public API calls are blocked by FRIDAY privacy policy.")
        base_url = _ENDPOINTS.get(request.endpoint_id)
        if base_url is None:
            raise ApiError("HOST_NOT_ALLOWED", "The API endpoint is not in FRIDAY's fixed allowlist.")
        if not isinstance(request.path, str) or not _PATH.fullmatch(request.path) or ".." in request.path:
            raise ApiError("INVALID_ENDPOINT", "The API path is invalid.")
        if not isinstance(request.params, dict) and not hasattr(request.params, "items"):
            raise ApiError("INVALID_PARAMETERS", "API query parameters must be a mapping.")
        if (isinstance(request.cache_ttl_seconds, bool)
                or not isinstance(request.cache_ttl_seconds, (int, float))
                or not math.isfinite(request.cache_ttl_seconds)
                or not 0 <= request.cache_ttl_seconds <= 86_400):
            raise ApiError("INVALID_CACHE_TTL", "API cache lifetime is outside its bound.")

        pairs: list[tuple[str, str | int | float]] = []
        for key, value in sorted(request.params.items()):
            if not isinstance(key, str) or not _PARAMETER.fullmatch(key):
                raise ApiError("INVALID_PARAMETERS", "An API query parameter name is invalid.")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ApiError("INVALID_PARAMETERS", "API query values must be simple scalar values.")
            if isinstance(value, float) and not math.isfinite(value):
                raise ApiError("INVALID_PARAMETERS", "API query values must be finite.")
            pairs.append((key, value))

        query = urlencode(pairs)
        url = f"{base_url}{request.path}" + (f"?{query}" if query else "")
        if len(url) > 2048:
            raise ApiError("INVALID_PARAMETERS", "The API request exceeds the URL length limit.")
        source = f"{base_url}{request.path}"
        cache_key = url
        now = time.monotonic()
        if request.cache_ttl_seconds:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    expires_at, data, cached_source = cached
                    if expires_at > now:
                        self._cache.move_to_end(cache_key)
                        return ApiFetch(deepcopy(data), cached_source, False)
                    del self._cache[cache_key]

        try:
            response = self._fetcher.fetch(url, timeout_seconds=6)
        except ResearchFetchError as error:
            if error.code == "HTTP_ERROR" and error.status_code == 429:
                code = "RATE_LIMITED"
            elif error.code == "HTTP_ERROR" and error.status_code == 404:
                code = "NOT_FOUND"
            elif error.code == "HTTP_ERROR" and error.status_code is not None and error.status_code >= 500:
                code = "UPSTREAM_UNAVAILABLE"
            else:
                code = {
                    "FETCH_TIMEOUT": "TIMEOUT",
                    "FETCH_FAILED": "NETWORK_ERROR",
                    "DNS_FAILED": "NETWORK_ERROR",
                    "RESPONSE_TOO_LARGE": "RESPONSE_TOO_LARGE",
                    "REDIRECT_LIMIT": "REDIRECT_REJECTED",
                    "REDIRECT_LOOP": "REDIRECT_REJECTED",
                    "SSRF_BLOCKED": "SSRF_BLOCKED",
                    "BLOCKED_SCHEME": "HTTPS_REQUIRED",
                }.get(error.code, "PROVIDER_ERROR")
            raise ApiError(code, "The public API request failed safely.") from None
        except (TimeoutError, OSError):
            raise ApiError("NETWORK_ERROR", "The public API request failed safely.") from None

        media_type = (response.content_type or "").split(";", 1)[0].strip().casefold()
        if media_type not in {"application/json", "application/problem+json"}:
            raise ApiError("INVALID_CONTENT_TYPE", "The API did not return JSON.")
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ApiError("MALFORMED_JSON", "The API returned malformed JSON.") from None

        if request.cache_ttl_seconds:
            with self._cache_lock:
                self._cache[cache_key] = (time.monotonic() + request.cache_ttl_seconds, deepcopy(data), source)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > _MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
        return ApiFetch(data, source, True)

    @property
    def cache_entries(self) -> int:
        with self._cache_lock:
            return len(self._cache)


def allowed_endpoint_ids() -> frozenset[str]:
    return frozenset(_ENDPOINTS)
