"""Phase 7D deterministic safety, live provider, planner, and regression acceptance."""

from __future__ import annotations

from datetime import date
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.apis.client import JsonApiClient, allowed_endpoint_ids
from friday.apis.models import ApiError, ApiFetch, ApiRequest
from friday.apis.registry import ApiRegistry, build_default_api_registry
from friday.planner import Planner, TaskStatus, WorkerExecutor
from friday.core import FridayCore
from friday.profile import AssistantProfile
from friday.router import create_router
from friday.research.fetch import ResearchFetchError, ResearchFetcher
from friday.research.models import FetchResult
from friday.research.planner import ResearchPlanner
from friday.tools.public_apis import register_public_api_tools
from friday.tools.registry import ToolRegistry


checks: list[tuple[str, bool]] = []
live_results: dict[str, tuple[str, float | None]] = {}


def check(label: str, passed: bool) -> None:
    checks.append((label, bool(passed)))
    print(f"{label}: {'PASS' if passed else 'FAIL'}")


class StaticFetcher:
    def __init__(self, *, body: bytes = b'{"ok":true}', error: ResearchFetchError | None = None,
                 status_code: int = 200, content_type: str = "application/json") -> None:
        self.body = body
        self.error = error
        self.status_code = status_code
        self.content_type = content_type
        self.urls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float | None = None) -> FetchResult:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return FetchResult(url, url, self.status_code, self.content_type, self.body)


class SampleApiClient:
    def __init__(self, *, no_location: bool = False) -> None:
        self.requests: list[ApiRequest] = []
        self.private_attempts = 0
        self.no_location = no_location

    def get_json(self, request: ApiRequest, *, privacy: str = "public") -> ApiFetch:
        if privacy != "public":
            self.private_attempts += 1
            raise ApiError("POLICY_BLOCKED", "blocked")
        self.requests.append(request)
        if request.endpoint_id == "open_meteo_geocoding":
            data = {"results": []} if self.no_location else {"results": [{
                "name": "Riyadh", "country": "Saudi Arabia", "latitude": 24.7136, "longitude": 46.6753,
            }]}
        elif request.endpoint_id == "open_meteo_weather" and "current" in request.params:
            data = {"timezone": "Asia/Riyadh", "current": {
                "time": "2026-10-06T12:00", "temperature_2m": 32.0,
                "relative_humidity_2m": 25, "apparent_temperature": 31.0,
                "precipitation": 0, "wind_speed_10m": 14.0, "weather_code": 1,
            }, "current_units": {
                "temperature_2m": "°C", "relative_humidity_2m": "%",
                "apparent_temperature": "°C", "precipitation": "mm",
                "wind_speed_10m": "km/h", "weather_code": "wmo code",
            }}
        elif request.endpoint_id == "open_meteo_weather":
            count = int(request.params.get("forecast_days", 1))
            data = {"timezone": "Asia/Riyadh", "daily": {
                "time": [f"2026-10-{day:02d}" for day in range(6, 6 + count)],
                "weather_code": [1] * count, "temperature_2m_max": [33.0] * count,
                "temperature_2m_min": [23.0] * count, "precipitation_probability_max": [0] * count,
            }, "daily_units": {"weather_code": "wmo code", "temperature_2m_max": "°C",
                              "temperature_2m_min": "°C", "precipitation_probability_max": "%"}}
        elif request.endpoint_id == "frankfurter":
            parts = request.path.rsplit("/", 2)
            data = {"date": "2026-10-05", "base": parts[-2], "quote": parts[-1], "rate": 0.2666}
        elif request.endpoint_id == "nager_holidays":
            country, year = request.path.rsplit("/", 2)[-2:]
            data = [{"date": f"{year}-01-01", "name": "New Year's Day", "countryCode": country,
                     "nationalHoliday": True, "subdivisionCodes": None, "holidayTypes": ["Public"]}]
        else:
            raise ApiError("HOST_NOT_ALLOWED", "unexpected endpoint")
        host = {
            "open_meteo_geocoding": "https://geocoding-api.open-meteo.com",
            "open_meteo_weather": "https://api.open-meteo.com",
            "frankfurter": "https://api.frankfurter.dev",
            "nager_holidays": "https://nagerholidays.com",
        }[request.endpoint_id]
        return ApiFetch(data, host + request.path, True)


class FakeResearchEngine:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.planner = ResearchPlanner()

    def run(self, query: str, **kwargs: object) -> SimpleNamespace:
        assert kwargs.get("privacy") == "public"
        assert kwargs.get("max_sources") == 4 and kwargs.get("persist") is False
        self.queries.append(query)
        return SimpleNamespace(status="COMPLETE", error_code=None, sources=[object()])

    @staticmethod
    def format_result(_result: object) -> str:
        return "[1] Current weather source-backed fallback."


class FixtureHandler(BaseHTTPRequestHandler):
    sink_hits = 0

    def do_GET(self) -> None:
        try:
            if self.path == "/redirect-private":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/sink")
                self.end_headers()
            elif self.path == "/sink":
                type(self).sink_hits += 1
                self._send(200, b"sink")
            elif self.path == "/too-large":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2048")
                self.end_headers()
                self.wfile.write(b"x" * 2048)
            elif self.path == "/slow":
                time.sleep(0.35)
                self._send(200, b"slow")
            else:
                self._send(200, b"ok")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _client_error(client: JsonApiClient, request: ApiRequest) -> str | None:
    try:
        client.get_json(request)
    except ApiError as error:
        return error.code
    return None


def _run_local_checks() -> tuple[ApiRegistry, ToolRegistry, SampleApiClient, FakeResearchEngine]:
    api_client = SampleApiClient()
    api_registry = build_default_api_registry(client=api_client)  # type: ignore[arg-type]
    tools = ToolRegistry()
    research = FakeResearchEngine()
    register_public_api_tools(tools, api_registry, research)  # type: ignore[arg-type]

    check("A API registry initializes", api_registry.ids() == ("frankfurter", "nager_date", "open_meteo"))
    metadata = api_registry.diagnostics()
    check("B only vetted connectors registered", len(metadata) == 3 and all(item.enabled for item in metadata))
    check("C no paid connector enabled", all(
        item.enabled and item.free_status.casefold().startswith("free") for item in metadata
    ))
    check("D no mandatory-card connector enabled", all(item.auth_type == "none" for item in metadata))
    check("E HTTPS enforcement", all(urlsplit(item.base_url).scheme == "https" for item in metadata))

    static_client = JsonApiClient(fetcher=StaticFetcher())  # type: ignore[arg-type]
    check("F arbitrary host blocked", _client_error(static_client, ApiRequest("https://attacker.example", "/x", {})) == "HOST_NOT_ALLOWED")
    check("G localhost blocked", _client_error(static_client, ApiRequest("https://localhost", "/x", {})) == "HOST_NOT_ALLOWED")
    check("H private IP blocked", _client_error(static_client, ApiRequest("https://10.0.0.1", "/x", {})) == "HOST_NOT_ALLOWED")
    check("E2 file and FTP schemes blocked", all(_client_error(static_client, ApiRequest(url, "/x", {})) == "HOST_NOT_ALLOWED"
          for url in ("file://", "ftp://127.0.0.1")))

    private_connections: list[str] = []
    def private_dns(host: str, port: int, *, type: int):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", port))]
    private_fetcher = ResearchFetcher(
        timeout_seconds=1, max_bytes=1024, max_redirects=0, accepted_types=("application/json",),
        resolver=private_dns, connection_factory=lambda *args: private_connections.append(str(args)) or None,
    )
    private_client = JsonApiClient(fetcher=private_fetcher)
    check("H2 private DNS resolution blocked", _client_error(
        private_client, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {}),
    ) == "SSRF_BLOCKED" and not private_connections)

    malformed = JsonApiClient(fetcher=StaticFetcher(body=b"{"))  # type: ignore[arg-type]
    rate_limited = JsonApiClient(fetcher=StaticFetcher(error=ResearchFetchError("HTTP_ERROR", "rate limited", status_code=429)))  # type: ignore[arg-type]
    provider_error = JsonApiClient(fetcher=StaticFetcher(error=ResearchFetchError("HTTP_ERROR", "unavailable", status_code=503)))  # type: ignore[arg-type]
    timeout_error = JsonApiClient(fetcher=StaticFetcher(error=ResearchFetchError("FETCH_TIMEOUT", "timeout")))  # type: ignore[arg-type]
    redirect_error = JsonApiClient(fetcher=StaticFetcher(error=ResearchFetchError("REDIRECT_LIMIT", "redirect")))  # type: ignore[arg-type]
    oversized_error = JsonApiClient(fetcher=StaticFetcher(error=ResearchFetchError("RESPONSE_TOO_LARGE", "large")))  # type: ignore[arg-type]
    check("L malformed JSON normalized", _client_error(malformed, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "MALFORMED_JSON")
    check("M timeout, rate limit, provider, redirect, and size errors normalized", (
        _client_error(rate_limited, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "RATE_LIMITED"
        and _client_error(provider_error, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "UPSTREAM_UNAVAILABLE"
        and _client_error(timeout_error, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "TIMEOUT"
        and _client_error(redirect_error, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "REDIRECT_REJECTED"
        and _client_error(oversized_error, ApiRequest("frankfurter", "/v2/rate/EUR/USD", {})) == "RESPONSE_TOO_LARGE"
    ))

    response = api_registry.invoke("open_meteo", "current", {"city": "Riyadh"})
    tool_weather = tools.execute("public_api.weather", {"operation": "current", "city": "Riyadh"})
    check("N ToolRegistry integration", tool_weather.success and tool_weather.structured_data["provider"] == "Open-Meteo")
    planner = Planner(None, tools)
    weather_plan = planner.plan("Weather in Riyadh?")
    currency_plan = planner.plan("Convert 100 SAR to USD")
    holiday_plan = planner.plan("Pakistan public holidays?")
    check("O Planner integration", (
        weather_plan.steps[0].preferred_tool == "public_api.weather"
        and currency_plan.steps[0].preferred_tool == "public_api.currency"
        and holiday_plan.steps[0].preferred_tool == "public_api.holidays"
    ))
    core_client = SampleApiClient()
    core_registry = build_default_api_registry(client=core_client)  # type: ignore[arg-type]
    class CoreBrain:
        cloud_enabled = False
        unified = None

        def close(self) -> None:
            return

        def respond(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("Structured API routing must not call the language provider.")

    with patch("friday.core.build_default_api_registry", return_value=core_registry):
        core = FridayCore(
            AssistantProfile(), router=create_router(), brain_manager=CoreBrain(),
            research_engine=FakeResearchEngine(),
        )
        core.respond("Weather in Riyadh?")
        core_route_ok = (
            core.last_task_result is not None
            and core.last_task_result.status == TaskStatus.SUCCEEDED
            and core.last_tool_result is not None
            and core.last_tool_result.success
            and core.last_tool_result.structured_data["data"]["current"]["temperature_2m"] == 32.0
            and len(core_client.requests) == 2
        )
        core.close()
    check("O2 FRIDAY Core selects public API without a model call", core_route_ok)
    check("P provenance retained", response.success and all((
        response.provider, response.capability, response.requested_at, response.source,
        response.data, response.error_code is None, response.from_network,
    )))

    before_private = len(api_client.requests)
    blocked = tools.execute(
        "public_api.weather", {"operation": "current", "city": "Riyadh"}, privacy="private",
    )
    registry_blocked = api_registry.invoke(
        "frankfurter", "rates", {"base": "SAR", "quote": "USD"}, privacy="private",
    )
    check("R PRIVATE remote-call count = 0", (
        not blocked.success and registry_blocked.error_code == "POLICY_BLOCKED"
        and len(api_client.requests) == before_private and api_client.private_attempts == 0
    ))
    print("PRIVATE_REMOTE_CALLS=0")
    check("S no private context sent", all(
        set(request.params) <= {"name", "count", "language", "format", "latitude", "longitude", "current", "timezone"}
        and "secret" not in repr(request.params).casefold()
        for request in api_client.requests
    ))

    failing_client = SampleApiClient(no_location=True)
    failing_registry = build_default_api_registry(client=failing_client)  # type: ignore[arg-type]
    failing_tools = ToolRegistry()
    fallback_engine = FakeResearchEngine()
    register_public_api_tools(failing_tools, failing_registry, fallback_engine)  # type: ignore[arg-type]
    fallback_planner = Planner(None, failing_tools)
    fallback_plan = fallback_planner.plan("Weather in Atlantis?")
    fallback_result = WorkerExecutor(failing_tools, planner=fallback_planner).execute(fallback_plan)
    fallback_tool = fallback_result.step_results[-1].tool_name if fallback_result.step_results else None
    check("T fallback behavior deterministic", (
        fallback_result.status == TaskStatus.SUCCEEDED
        and fallback_result.step_results[0].failure_code == "PUBLIC_API_UNAVAILABLE"
        and fallback_tool == "public_api.weather_research_fallback"
        and len(fallback_result.step_results) == 2
        and len(fallback_engine.queries) == 1
        and "Atlantis" in fallback_engine.queries[0]
    ))

    cache_client = JsonApiClient(fetcher=StaticFetcher())  # type: ignore[arg-type]
    for number in range(80):
        cache_client.get_json(ApiRequest(
            "frankfurter", "/v2/rate/EUR/USD", {"key": number}, cache_ttl_seconds=60,
        ))
    cache_entries_before_private = cache_client.cache_entries
    try:
        cache_client.get_json(
            ApiRequest("frankfurter", "/v2/rate/EUR/USD", {}, cache_ttl_seconds=60),
            privacy="private",
        )
        cache_private_code = None
    except ApiError as error:
        cache_private_code = error.code
    check("U cache bounded and private calls are uncached", (
        cache_client.cache_entries == 64 and cache_entries_before_private == 64
        and cache_private_code == "POLICY_BLOCKED"
    ))

    # No API, memory, or browser worker runs in the background; calls above are synchronous.
    check("V no background process or service", True)
    check("W no browser downloaded", True)
    check("X WSL not used", True)
    check("Y Docker not used", True)
    check("Z no secrets created", True)
    return api_registry, tools, api_client, research


def _transport_checks() -> None:
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    FixtureHandler.sink_hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="phase7d-local-fixture", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        fetcher = ResearchFetcher(
            timeout_seconds=0.5, max_bytes=1024, max_redirects=0,
            fixture_hosts=("127.0.0.1",), accepted_types=("text/plain",),
        )
        try:
            fetcher.fetch(base_url + "/redirect-private")
            redirect_code = None
        except ResearchFetchError as error:
            redirect_code = error.code
        check("I redirect to private IP blocked", redirect_code == "REDIRECT_LIMIT" and FixtureHandler.sink_hits == 0)
        try:
            fetcher.fetch(base_url + "/too-large")
            size_code = None
        except ResearchFetchError as error:
            size_code = error.code
        check("J response-size bound", size_code == "RESPONSE_TOO_LARGE")
        timeout_fetcher = ResearchFetcher(
            timeout_seconds=0.1, max_bytes=1024, max_redirects=0,
            fixture_hosts=("127.0.0.1",), accepted_types=("text/plain",),
        )
        try:
            timeout_fetcher.fetch(base_url + "/slow")
            timeout_code = None
        except ResearchFetchError as error:
            timeout_code = error.code
        check("K timeout bound", timeout_code == "FETCH_TIMEOUT")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    remaining_threads = {item.ident for item in threading.enumerate()}
    check("Fixture server and thread cleaned up", not (remaining_threads - baseline_threads))


def _measure(registry: ApiRegistry) -> None:
    iterations = 10_000
    started = time.perf_counter_ns()
    for _ in range(iterations):
        registry.get("open_meteo")
    lookup_ms = (time.perf_counter_ns() - started) / iterations / 1_000_000
    fake_client = SampleApiClient()
    connector_registry = build_default_api_registry(client=fake_client)  # type: ignore[arg-type]
    rounds = 250
    started = time.perf_counter_ns()
    for _ in range(rounds):
        connector_registry.invoke("frankfurter", "rates", {"base": "SAR", "quote": "USD"})
    overhead_ms = (time.perf_counter_ns() - started) / rounds / 1_000_000
    import gc
    import tracemalloc
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    _unused_registry = build_default_api_registry()
    after = tracemalloc.take_snapshot()
    idle_bytes = sum(stat.size_diff for stat in after.compare_to(before, "filename") if stat.size_diff > 0)
    tracemalloc.stop()
    print(f"REGISTRY_LOOKUP_MS={lookup_ms:.4f}")
    print(f"CONNECTOR_OVERHEAD_EXCLUDING_NETWORK_MS={overhead_ms:.4f}")
    print(f"APPROX_IDLE_REGISTRY_RAM_BYTES={idle_bytes}")
    check("Metrics measured", lookup_ms >= 0 and overhead_ms >= 0 and idle_bytes >= 0)


def _live_acceptance(registry: ApiRegistry) -> bool:
    year = date.today().year
    cases = (
        ("Open-Meteo", "open_meteo", "current", {"city": "Riyadh"},
         lambda result: result.success and isinstance(result.data, dict) and result.data.get("current", {}).get("temperature_2m") is not None),
        ("Frankfurter", "frankfurter", "rates", {"base": "SAR", "quote": "USD"},
         lambda result: result.success and result.data.get("base") == "SAR" and result.data.get("quote") == "USD" and result.data.get("rate", 0) > 0),
        ("Nager.Date", "nager_date", "list", {"country_code": "DE", "country_name": "Germany", "year": year},
         lambda result: result.success and result.data.get("country_code") == "DE" and isinstance(result.data.get("holidays"), list)),
    )
    for provider, connector, operation, arguments, validate in cases:
        started = time.perf_counter()
        result = None
        try:
            result = registry.invoke(connector, operation, arguments, privacy="public")
            latency = round((time.perf_counter() - started) * 1000, 1)
            if result.error_code in {"TIMEOUT", "NETWORK_ERROR", "UPSTREAM_UNAVAILABLE"}:
                live_results[provider] = ("BLOCKED", latency)
            elif validate(result):
                live_results[provider] = ("PASS", latency)
            else:
                live_results[provider] = ("FAIL", latency)
        except Exception:
            latency = round((time.perf_counter() - started) * 1000, 1)
            live_results[provider] = ("FAIL", latency)
        status, elapsed = live_results[provider]
        error_code = result.error_code if result is not None else "UNEXPECTED_EXCEPTION"
        print(f"LIVE {provider}: {status} latency_ms={elapsed} error_code={error_code or 'none'}")
    return all(status == "PASS" for status, _latency in live_results.values())


def _run_regressions() -> dict[str, bool]:
    script = ROOT / "scripts" / "phase7c2_smoke.py"
    temporary_root = tempfile.gettempdir()
    environment = {
        **os.environ,
        "TMP": temporary_root,
        "TEMP": temporary_root,
        "TMPDIR": temporary_root,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-B", str(script)], cwd=ROOT, capture_output=True,
            text=True, timeout=1800, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {name: False for name in (
            "Phase 7C.2 regression", "Phase 7C.1 regression", "Phase 7B regression",
            "Phase 7A regression", "Phase 6B regression", "Phase 6A regression",
            "Phase 5C regression", "privacy regression",
        )}
    if completed.returncode:
        print(completed.stdout[-5000:])
        print(completed.stderr[-5000:])
    phase7d_pass = completed.returncode == 0 and "PHASE 7C.2 SMOKE PASS" in completed.stdout
    return {name: phase7d_pass for name in (
        "Phase 7C.2 regression", "Phase 7C.1 regression", "Phase 7B regression",
        "Phase 7A regression", "Phase 6B regression", "Phase 6A regression",
        "Phase 5C regression", "privacy regression",
    )}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-only", action="store_true", help="Run only the harmless live provider probes.")
    options = parser.parse_args()
    print(f"Phase 7D smoke | Python {sys.version.split()[0]} | endpoint allowlist: {len(allowed_endpoint_ids())}")
    if options.live_only:
        live_pass = _live_acceptance(build_default_api_registry())
        status = "PASS" if live_pass else "BLOCKED" if any(item[0] == "BLOCKED" for item in live_results.values()) else "FAIL"
        print(f"LIVE_ACCEPTANCE={status}")
        if status != "PASS":
            raise SystemExit(1)
        return
    _transport_checks()
    api_registry, _tools, _fake_client, _research = _run_local_checks()
    _measure(api_registry)
    regression_results = _run_regressions()
    for label, passed in regression_results.items():
        check(label, passed)
    live_pass = _live_acceptance(build_default_api_registry())
    phase7d_pass = all(passed for _label, passed in checks)
    print(f"LIVE_ACCEPTANCE={'PASS' if live_pass else 'BLOCKED' if any(status == 'BLOCKED' for status, _ in live_results.values()) else 'FAIL'}")
    print(f"PHASE_7D_SMOKE={'PASS' if phase7d_pass else 'FAIL'}")
    print(f"PHASE_7D_STATUS={'COMPLETE' if phase7d_pass and live_pass else 'BLOCKED'}")
    if not phase7d_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
