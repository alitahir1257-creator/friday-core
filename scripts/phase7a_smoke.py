"""Deterministic, offline Phase 7A research acceptance checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.automation import Automation, AutomationRunner, AutomationStatus, AutomationTrigger, RunStatus, TriggerType  # noqa: E402
from friday.model_policy import ModelPolicyEngine  # noqa: E402
from friday.planner import Planner, WorkerExecutor  # noqa: E402
from friday.research.engine import ResearchEngine  # noqa: E402
from friday.research.fetch import ResearchFetchError, ResearchFetcher  # noqa: E402
from friday.research.models import (  # noqa: E402
    ClaimKind,
    ExtractedPage,
    ResearchClaim,
    ResearchPlan,
    ResearchSource,
    ResearchStatus,
    SearchResult,
)
from friday.research.planner import ResearchPlanner  # noqa: E402
from friday.research.quality import is_duplicate  # noqa: E402
from friday.research.search import (  # noqa: E402
    BrowserSearchProvider,
    SearchProviderError,
    SearXNGSearchProvider,
    _normalize_browser_result_url,
    configured_search_providers,
)
from friday.research.store import ResearchEvidenceStore  # noqa: E402
from friday.research.synthesis import ResearchSynthesizer, validate_claims  # noqa: E402
from friday.research.urls import UnsafeResearchURL  # noqa: E402
from friday.tools.registry import ToolRegistry  # noqa: E402
from friday.tools.research import register_research_tools  # noqa: E402


QUOTE = "This fixture explains bounded research evidence with a deterministic public fact."
HTML_OFFICIAL = (
    '<!doctype html><html><head><title>Official bounded research</title>'
    '<meta name="author" content="FRIDAY Fixture">'
    '<meta property="article:published_time" content="2026-09-20">'
    '<link rel="canonical" href="/canonical-official"></head>'
    f'<body><nav>Navigation clutter</nav><main><h1>Official evidence</h1><p>{QUOTE}</p>'
    '<script>hidden script words</script></main></body></html>'
).encode()
HTML_DUPLICATE = (
    '<html><head><title>Official bounded research</title></head>'
    f'<body><main><h1>Official evidence</h1><p>{QUOTE}</p>'
    '<script>hidden script words</script></main></body></html>'
).encode()


class FixtureHandler(BaseHTTPRequestHandler):
    requests: list[str] = []
    request_headers: list[dict[str, str | None]] = []

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", **headers) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        self.requests.append(path)
        self.request_headers.append({
            "user-agent": self.headers.get("User-Agent"),
            "cookie": self.headers.get("Cookie"),
            "authorization": self.headers.get("Authorization"),
        })
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/official")
            self.end_headers()
        elif path.startswith("/redirect-chain/"):
            current = int(path.rsplit("/", 1)[1])
            self.send_response(302)
            self.send_header("Location", f"/redirect-chain/{current + 1}" if current < 5 else "/official")
            self.end_headers()
        elif path == "/official":
            self._send(200, HTML_OFFICIAL)
        elif path == "/duplicate":
            self._send(200, HTML_DUPLICATE)
        elif path == "/news":
            self._send(200, b"<html><body><main><h1>Fixture news</h1><p>Independent bounded research evidence from a second report.</p></main></body></html>")
        elif path == "/malformed":
            self._send(200, b"<html><head><title>Malformed evidence</title><body><main><h1>Unclosed page<p>Useful malformed HTML evidence remains extractable.")
        elif path == "/large":
            self._send(200, b"<html><body>" + b"x" * 5000 + b"</body></html>")
        elif path == "/binary":
            self._send(200, b"\x00\x01\x02", "application/octet-stream")
        elif path == "/pdf":
            self._send(200, b"%PDF-1.7\nfixture", "application/pdf")
        elif path == "/captcha":
            self._send(200, b"<html><body><main><h1>Captcha required</h1><p>Verify you are human.</p></main></body></html>")
        elif path == "/missing":
            self._send(404, b"<html><body>not found</body></html>")
        else:
            self._send(404, b"<html><body>unknown</body></html>")


class FixtureServer:
    def __enter__(self):
        FixtureHandler.requests = []
        FixtureHandler.request_headers = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.fetcher = ResearchFetcher(
            timeout_seconds=3,
            max_bytes=1024,
            fixture_hosts=("127.0.0.1",),
        )
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeProvider:
    name = "fixture"
    configured = True

    def __init__(self, rows, *, fail=False, on_search=None):
        self.rows = list(rows)
        self.fail = fail
        self.on_search = on_search
        self.queries: list[str] = []
        self.limits: list[int] = []

    def search(self, query, limit=8, *, timeout_seconds=10):
        self.queries.append(query)
        self.limits.append(limit)
        if self.on_search:
            self.on_search()
        if self.fail:
            raise RuntimeError("fixture provider failure")
        return list(self.rows[:limit])


class FixtureBrowserSearchProvider(BrowserSearchProvider):
    def __init__(self, rows=(), *, error_code=None, engine="duckduckgo"):
        super().__init__(engine)
        self.rows = list(rows)
        self.error_code = error_code
        self.queries: list[str] = []

    def _search_page(self, query, limit, timeout_seconds):
        self.queries.append(query)
        if self.error_code:
            raise SearchProviderError(self.error_code)
        return self.rows[:limit]


class FakeSynthesizer:
    def synthesize(self, _plan, sources, *, cancelled=None):
        if cancelled and cancelled():
            return []
        source = next((item for item in sources if item.accessible and QUOTE in item.text), None)
        if source is None:
            return []
        return [ResearchClaim(
            QUOTE,
            ClaimKind.SOURCE_DERIVED_FACT,
            (source.citation_id,),
            {source.citation_id: QUOTE},
        )]


class RecordingStore:
    def __init__(self):
        self.save_calls = 0

    def save(self, _run):
        self.save_calls += 1


class FixedPlanner:
    def plan(self, question, *, privacy="public", freshness="current", max_sources=8,
             timeout_budget_seconds=45, preferred_domains=(), excluded_domains=(), persist=False):
        return ResearchPlan(
            question=question,
            search_queries=("query-1", "query-2", "query-3", "query-4"),
            max_sources=max_sources,
            timeout_budget_seconds=timeout_budget_seconds,
            freshness=freshness,
            preferred_domains=tuple(preferred_domains),
            excluded_domains=tuple(excluded_domains),
            privacy=privacy,
            persist=persist,
        )


class MockResponse(BytesIO):
    status = 200

    def __init__(self, payload: bytes, content_type: str):
        super().__init__(payload)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"{name}: PASS")


def result(url: str, *, title="Official bounded research", snippet="bounded research evidence", rank=1):
    return SearchResult(title, url, snippet, "fixture", rank)


def source_for(citation: str, *, text=QUOTE, canonical="https://one.example/a", content_hash="hash"):
    return ResearchSource(
        id=citation, requested_url=canonical, final_url=canonical, canonical_url=canonical,
        title="Evidence", domain="one.example", author=None, published_at="2026-09-20",
        retrieved_at=datetime.now(timezone.utc).isoformat(), text=text, headings=(),
        content_hash=content_hash, source_type="primary", citation_id=citation,
    )


def main() -> None:
    network = {"public": 0}
    with FixtureServer() as fixture:
        base = fixture.base

        # A. Normalize a mocked SearXNG response without contacting a server.
        class MockOpener:
            def open(self, request, timeout):
                check("A SEARCH RESULT NORMALIZATION", request.method == "POST" and timeout <= 10)
                query = parse_qs(request.data.decode()).get("q", [""])[0]
                check("A query is passed through the configured provider contract", query == "python documentation")
                payload = json.dumps({"results": [{
                    "title": "<b>Official &amp; Python</b>",
                    "url": "https://docs.python.org/3/",
                    "content": "<em>Documentation</em> result",
                    "publishedDate": "2026-09-20",
                    "engines": ["fixture"],
                }]}).encode()
                return MockResponse(payload, "application/json")

        search_started = time.perf_counter()
        normalized = SearXNGSearchProvider("https://search.example", opener=MockOpener()).search("python documentation")
        search_ms = (time.perf_counter() - search_started) * 1000
        check("A normalized title, URL, snippet, source, rank", len(normalized) == 1 and normalized[0].title == "Official & Python" and normalized[0].url == "https://docs.python.org/3/" and normalized[0].snippet == "Documentation result" and normalized[0].source == "searxng" and normalized[0].rank == 1)
        configured = configured_search_providers({
            "FRIDAY_RESEARCH_SEARXNG_URL": "https://search.example",
            "FRIDAY_RESEARCH_BROWSER_SEARCH_ENGINE": "duckduckgo",
        })
        check("A healthy configured SearXNG is first", [item.name for item in configured] == ["searxng", "browser"])
        fallback = configured_search_providers({})
        check("A SearXNG unset selects the browser fallback", [item.name for item in fallback] == ["browser"] and fallback[0].configured)
        invalid_optional = configured_search_providers({"FRIDAY_RESEARCH_SEARXNG_URL": "file:///etc/passwd"})
        check("A invalid optional SearXNG is skipped", [item.name for item in invalid_optional] == ["browser"])
        check("A browser search strips tracking and unwraps result URLs", _normalize_browser_result_url(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F%3Futm_source%3Dddg%26v%3D1&rut=x",
            "duckduckgo", "https://html.duckduckgo.com/html/?q=python",
        ) == "https://docs.python.org/3/?v=1")
        unconfigured = ResearchEngine(search_providers=(), fetcher=fixture.fetcher).run("Research bounded fixture evidence")
        check("A explicitly empty provider list reports configuration required", unconfigured.status == ResearchStatus.SEARCH_CONFIGURATION_REQUIRED)
        browser_fallback = FixtureBrowserSearchProvider([result(base + "/official")])
        preferred = ResearchEngine(
            search_providers=(SearXNGSearchProvider("https://search.example", opener=MockOpener()), browser_fallback),
            fetcher=fixture.fetcher,
        ).search("Research python documentation")
        check("A healthy SearXNG wins before browser fallback", bool(preferred) and not browser_fallback.queries)
        challenge_run = ResearchEngine(
            search_providers=(FixtureBrowserSearchProvider(error_code="SEARCH_CHALLENGE"),),
            fetcher=fixture.fetcher,
        ).run("Research bounded fixture evidence")
        timeout_run = ResearchEngine(
            search_providers=(FixtureBrowserSearchProvider(error_code="SEARCH_TIMEOUT"),),
            fetcher=fixture.fetcher,
        ).run("Research bounded fixture evidence")
        empty_run = ResearchEngine(
            search_providers=(FixtureBrowserSearchProvider(),), fetcher=fixture.fetcher,
        ).run("Research bounded fixture evidence")
        check("A browser challenge, timeout, and no-results statuses normalize", challenge_run.status == ResearchStatus.SEARCH_CHALLENGE and timeout_run.status == ResearchStatus.SEARCH_TIMEOUT and empty_run.status == ResearchStatus.NO_RESULTS)

        # B, C, F, G. Static extraction, bounded redirect, malformed HTML, and body-size limit.
        fetch_started = time.perf_counter()
        page = ResearchEngine(fetcher=fixture.fetcher, enable_browser_fallback=False).fetch(base + "/official")
        fetch_ms = (time.perf_counter() - fetch_started) * 1000
        check("B FETCH PAGE", QUOTE in page.text and page.title == "Official bounded research" and page.canonical_url.endswith("/canonical-official") and page.author == "FRIDAY Fixture" and page.published_at == "2026-09-20")
        request_headers = FixtureHandler.request_headers[-1]
        check("B safe user agent without cookies or auth", "FRIDAY-Research/" in (request_headers["user-agent"] or "") and request_headers["cookie"] is None and request_headers["authorization"] is None)
        redirected = fixture.fetcher.fetch(base + "/redirect")
        check("C REDIRECT", redirected.final_url.endswith("/official") and FixtureHandler.requests.count("/redirect") == 1)
        try:
            fixture.fetcher.fetch(base + "/redirect-chain/0")
            redirect_limit_enforced = False
        except ResearchFetchError as error:
            redirect_limit_enforced = error.code == "REDIRECT_LIMIT"
        check("C redirect count is bounded", redirect_limit_enforced)
        malformed_result = ResearchEngine(
            search_providers=(FakeProvider([result(base + "/malformed")]),),
            fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).run("Research malformed bounded evidence")
        check("F MALFORMED HTML", any(not item.accessible and item.error_code == "EXTRACTION_FAILED" for item in malformed_result.sources))

        class FakeRenderer:
            calls = 0

            def render(self, url, *, timeout_seconds, max_text_chars):
                self.calls += 1
                return ExtractedPage(url, url, "Rendered fixture", (), None, None, QUOTE, "rendered-hash")

        renderer = FakeRenderer()
        rendered = ResearchEngine(fetcher=fixture.fetcher, browser_renderer=renderer).fetch(base + "/malformed")
        check("F controlled browser fallback", renderer.calls == 1 and rendered.title == "Rendered fixture")
        try:
            fixture.fetcher.fetch(base + "/large")
            large_rejected = False
        except ResearchFetchError as error:
            large_rejected = error.code == "RESPONSE_TOO_LARGE"
        check("G LARGE RESPONSE", large_rejected)
        try:
            fixture.fetcher.fetch(base + "/binary")
            binary_rejected = False
        except ResearchFetchError as error:
            binary_rejected = error.code == "UNSUPPORTED_CONTENT_TYPE"
        pdf_deferred = fixture.fetcher.fetch(base + "/pdf")
        check("DOWNLOAD SAFETY", binary_rejected and pdf_deferred.error_code == "DOCUMENT_PARSING_DEFERRED" and not pdf_deferred.body)

        # D/E. Canonical URL and content hash deduplication.
        engine = ResearchEngine(fetcher=fixture.fetcher, enable_browser_fallback=False)
        deduped = engine._dedupe_search_results([
            result(base + "/official#section", rank=1), result(base + "/official", rank=2),
        ], fixture_hosts=fixture.fetcher.fixture_hosts)
        check("D DUPLICATE URL", len(deduped) == 1)
        duplicate_page = engine.fetch(base + "/duplicate")
        original_source = source_for("S1", text=page.text, canonical=page.canonical_url, content_hash=page.content_hash)
        duplicate_source = source_for("S2", text=duplicate_page.text, canonical=duplicate_page.canonical_url, content_hash=duplicate_page.content_hash)
        check("E DUPLICATE CONTENT", page.content_hash == duplicate_page.content_hash and is_duplicate(duplicate_source, [original_source]))

        # H/I. Reject non-HTTP, loopback, private, and DNS-to-private targets before connecting.
        try:
            fixture.fetcher.fetch("file:///etc/passwd")
            unsafe_scheme_rejected = False
        except UnsafeResearchURL as error:
            unsafe_scheme_rejected = error.code == "BLOCKED_SCHEME"
        check("H UNSAFE SCHEME", unsafe_scheme_rejected)
        try:
            ResearchFetcher().fetch("https://example.org/page?access_token=private-value")
            credentials_rejected = False
        except UnsafeResearchURL as error:
            credentials_rejected = error.code == "BLOCKED_CREDENTIAL_URL"
        check("H credential-bearing URL", credentials_rejected)
        ssrf_rejected = True
        for url in ("http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/"):
            try:
                ResearchFetcher().fetch(url)
                ssrf_rejected = False
            except UnsafeResearchURL as error:
                ssrf_rejected = ssrf_rejected and error.code in {"SSRF_BLOCKED", "BLOCKED_PORT"}
        private_dns = ResearchFetcher(resolver=lambda *_args, **_kwargs: [(None, None, None, None, ("192.168.1.10", 80))])
        try:
            private_dns.fetch("http://example.test/")
            ssrf_rejected = False
        except ResearchFetchError as error:
            ssrf_rejected = ssrf_rejected and error.code == "SSRF_BLOCKED"
        check("I LOCAL/PRIVATE IP SSRF", ssrf_rejected)

        # J/K. Claims map to real source IDs and unsupported claims cannot gain provenance.
        citation_source = source_for("S1")
        valid = validate_claims(json.dumps({"claims": [{
            "kind": "SOURCE_DERIVED_FACT", "text": QUOTE, "citations": ["S1"],
            "evidence_quotes": {"S1": QUOTE},
        }]}), [citation_source])
        check("J CITATION PROVENANCE", len(valid) == 1 and valid[0].citation_ids == ("S1",) and valid[0].evidence_quotes["S1"] == QUOTE)
        synthesis_claim = validate_claims(json.dumps({"claims": [{
            "kind": "MODEL_SYNTHESIS", "text": "The page supports a conclusion about bounded research.",
            "citations": ["S1"], "evidence_quotes": {"S1": QUOTE},
        }]}), [citation_source])
        check("J model synthesis remains distinctly labeled", len(synthesis_claim) == 1 and synthesis_claim[0].kind == ClaimKind.MODEL_SYNTHESIS and synthesis_claim[0].citation_ids == ("S1",))
        unsupported = validate_claims(json.dumps({"claims": [{
            "kind": "SOURCE_DERIVED_FACT", "text": "A claim with no source.", "citations": [], "evidence_quotes": {},
        }]}), [citation_source])
        check("K UNSUPPORTED CLAIM", len(unsupported) == 1 and unsupported[0].kind == ClaimKind.UNSUPPORTED_UNVERIFIED and not unsupported[0].citation_ids)

        class SynthesisBrainSpy:
            cloud_enabled = True
            policy_engine = ModelPolicyEngine()

            def __init__(self):
                self.call = None

            def respond(self, command, messages, **kwargs):
                self.call = (command, messages, kwargs)
                return SimpleNamespace(text=json.dumps({"claims": [{
                    "kind": "SOURCE_DERIVED_FACT", "text": QUOTE, "citations": ["S1"],
                    "evidence_quotes": {"S1": QUOTE},
                }]}))

        private_brain = SynthesisBrainSpy()
        private_plan = ResearchPlan("bounded evidence", (), privacy="private", sanitized=True)
        synthesized = ResearchSynthesizer(private_brain).synthesize(private_plan, [citation_source])
        _prompt, history, synthesis_kwargs = private_brain.call
        check("PRIVATE synthesis uses BrainManager locally without knowledge retrieval", bool(synthesized) and not history and synthesis_kwargs["include_knowledge_context"] is False and synthesis_kwargs["model_selection"].preferred_backend == "local" and synthesis_kwargs["request_metadata"].effective_privacy == "private")

        # L. Hard limits are enforced at model and orchestration boundaries.
        invalid_plan_rejected = False
        try:
            ResearchPlan("question", ("q1",), max_search_queries=5)
        except ValueError:
            invalid_plan_rejected = True
        query_counter = FakeProvider([])
        ResearchEngine(
            search_providers=(query_counter,), fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(),
            planner=FixedPlanner(), enable_browser_fallback=False,
        ).run("bounded topic", max_sources=2)
        source_budget_provider = FakeProvider([
            result(base + "/official", title="Official bounded research", rank=1),
            result(base + "/news", title="Fixture bounded evidence", rank=2),
            result(base + "/duplicate", title="A third bounded evidence page", rank=3),
        ])
        source_budget = ResearchEngine(
            search_providers=(source_budget_provider,), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).run("Compare bounded source evidence", max_sources=2)
        check("L query budget", invalid_plan_rejected and len(query_counter.queries) == 4 and query_counter.limits == [8] * 4)
        candidate_budget_provider = FakeProvider([
            result(base + path, rank=index)
            for index, path in enumerate(("/official", "/news", "/duplicate", "/malformed", "/captcha", "/missing"), 1)
        ])
        ResearchEngine(
            search_providers=(candidate_budget_provider,), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), planner=FixedPlanner(), enable_browser_fallback=False,
        ).run("bounded topic", max_sources=1)
        check("L run-wide candidate budget", len(candidate_budget_provider.queries) == 2 and candidate_budget_provider.limits == [8, 2])
        check("L source budget and independent-source threshold", len(source_budget.sources) == 2 and source_budget.status == ResearchStatus.INSUFFICIENT_EVIDENCE)

        # M. Cancellation during search prevents all later fetches and synthesis.
        cancelled_flag = threading.Event()
        cancelling_provider = FakeProvider([result(base + "/official")], on_search=cancelled_flag.set)
        fetches_before_cancel = FixtureHandler.requests.count("/official")
        cancel_run = ResearchEngine(
            search_providers=(cancelling_provider,), fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(),
            planner=FixedPlanner(), enable_browser_fallback=False,
        ).run("Research bounded fixture evidence", cancelled=cancelled_flag.is_set)
        check("M CANCELLATION", cancel_run.status == ResearchStatus.CANCELLED and len(cancelling_provider.queries) == 1 and FixtureHandler.requests.count("/official") == fetches_before_cancel)
        check("M cancellation prevents remaining work", not any(item.event in {"source_selected", "source_fetched", "research_synthesizing"} for item in cancel_run.events))

        # N/O. Provider failures normalize and do not hide a healthy provider or later sources.
        failed_provider = FakeProvider([], fail=True)
        healthy_provider = FakeProvider([result(base + "/official")])
        fallback_results = ResearchEngine(
            search_providers=(failed_provider, healthy_provider), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).search("Research bounded research evidence")
        check("N SEARCH FAILURE", bool(fallback_results) and len(failed_provider.queries) == 1 and len(healthy_provider.queries) == 1)
        total_failure = ResearchEngine(
            search_providers=(FakeProvider([], fail=True),), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).run("Research bounded research evidence")
        check("N normalized provider error", total_failure.status == ResearchStatus.SEARCH_UNAVAILABLE and total_failure.error_code == "SEARCH_UNAVAILABLE")
        failure_engine = ResearchEngine(
            search_providers=(FakeProvider([result(base + "/missing", title="bounded research evidence", rank=1), result(base + "/official", rank=2)]),),
            fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        )
        partial = failure_engine.run("Research bounded research evidence")
        check("O FETCH FAILURE", partial.status == ResearchStatus.COMPLETE and any(not item.accessible for item in partial.sources) and any(item.accessible for item in partial.sources))

        # P. No usable result is an explicit insufficient state.
        empty = ResearchEngine(search_providers=(FakeProvider([]),), fetcher=fixture.fetcher, synthesizer=FakeSynthesizer()).run("Research an unknown topic")
        check("P NO_RESULTS", empty.status == ResearchStatus.NO_RESULTS and "NO_RESULTS" in empty.answer)
        blocked_page = ResearchEngine(
            search_providers=(FakeProvider([result(base + "/captcha", title="Captcha verification page", snippet="verify you are human")]),),
            fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).run("Research captcha verification page")
        check("ACCESS SAFETY CAPTCHA marked inaccessible", len(blocked_page.sources) == 1 and not blocked_page.sources[0].accessible and blocked_page.sources[0].error_code == "CAPTCHA_OR_ANTI_BOT_REQUIRED")

        # Q. Private context is stripped before the only external boundary (the mock provider).
        private_query = "Research bounded fixture evidence using my private ATSUM contract"
        sanitized = ResearchPlanner.public_query(private_query, privacy="private")
        sanitized_example = ResearchPlanner.public_query(
            "Using my private ATSUM contract, research Saudi 3PL market rates", privacy="private",
        )
        private_provider = FakeProvider([result(base + "/official")])
        store = RecordingStore()
        private_engine = ResearchEngine(
            search_providers=(private_provider,), fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(),
            evidence_store=store, enable_browser_fallback=False,
        )
        private_run = private_engine.run(private_query, privacy="private")
        leaked = sum(1 for query in private_provider.queries if any(secret in query.casefold() for secret in ("atsum", "contract", "private")))
        check("Q PRIVACY", sanitized == "bounded fixture evidence" and sanitized_example == "Saudi 3PL market rates" and private_run.status == ResearchStatus.COMPLETE and leaked == 0)
        local_only_provider = FakeProvider([result(base + "/official")])
        local_only = ResearchEngine(search_providers=(local_only_provider,), fetcher=fixture.fetcher).run("Research Saudi rates", privacy="local_only")
        check("Q LOCAL_ONLY has no remote query", local_only.status == ResearchStatus.INSUFFICIENT_EVIDENCE and not local_only_provider.queries)

        # R. Registry invocation carries research.run through its normal tool path.
        browser_tool_provider = FixtureBrowserSearchProvider([result(base + "/official")])
        tool_engine = ResearchEngine(
            search_providers=(browser_tool_provider,), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        )
        registry = ToolRegistry()
        register_research_tools(registry, tool_engine)
        tool_result = registry.execute(
            "research.run", {"question": "Research bounded fixture evidence"},
            caller="phase7a_smoke", privacy="public",
        )
        check("R RESEARCH TOOL", tool_result.success and "SOURCE_DERIVED_FACT" in tool_result.summary and "[S1]" in tool_result.summary and len(browser_tool_provider.queries) == 1)

        # S/T. Existing Planner and AutomationRunner use the same registered ToolRegistry capability.
        planner = Planner(None, registry, ModelPolicyEngine())
        plan = planner.plan("Research current local AI agent frameworks and compare them.", privacy="public")
        check("S PLANNER RESEARCH", len(plan.steps) == 1 and plan.steps[0].capability == "research" and plan.steps[0].preferred_tool == "research.run")
        worker = WorkerExecutor(registry, planner=planner)
        now = datetime.now(timezone.utc)
        automation = Automation(
            id="phase7a-smoke", name="fixture research", description="offline compatibility smoke",
            enabled=True, privacy="public", namespace="global",
            trigger=AutomationTrigger(TriggerType.ONE_TIME, run_at=now + timedelta(hours=1)),
            task_goal="Research bounded fixture evidence", created_at=now, updated_at=now,
            next_run_at=None, last_run_at=None, status=AutomationStatus.ACTIVE,
        )
        automation_result = AutomationRunner(planner, worker).execute(automation, lambda: False)
        check("T AUTOMATION COMPATIBILITY", automation_result.status == RunStatus.COMPLETED and automation_result.tool_count == 1)

        # No evidence promotion: persistence is opt-in and was not requested above.
        check("PROMOTION", store.save_calls == 0)

        with tempfile.TemporaryDirectory(prefix="phase7a-evidence-", dir=ROOT) as temp_root:
            evidence_store = ResearchEvidenceStore(root=temp_root)
            persisted = ResearchEngine(
                search_providers=(FakeProvider([result(base + "/official")]),),
                fetcher=fixture.fetcher, synthesizer=FakeSynthesizer(),
                evidence_store=evidence_store, enable_browser_fallback=False,
            ).run("Research bounded fixture evidence", persist=True)
            manifest_path = Path(temp_root) / "runs" / persisted.id / "manifest.json"
            source_path = Path(temp_root) / "runs" / persisted.id / "sources" / "S1.json"
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_text = source_path.read_text(encoding="utf-8")
            source_data = json.loads(source_text)
            check("EVIDENCE STORE explicit persistence", persisted.status == ResearchStatus.COMPLETE and "S1" in manifest_data["citations"] and QUOTE in source_data["text"] and "<html" not in source_text.casefold())

        # Resource checks are fixture-only; no paid or public service is contacted.
        tracemalloc.start()
        overhead_start = time.perf_counter()
        overhead = ResearchEngine(
            search_providers=(FakeProvider([result(base + "/official")]),), fetcher=fixture.fetcher,
            synthesizer=FakeSynthesizer(), enable_browser_fallback=False,
        ).run("Research bounded fixture evidence")
        orchestration_ms = (time.perf_counter() - overhead_start) * 1000
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        check("RESOURCE orchestration smoke", overhead.status == ResearchStatus.COMPLETE)
        print("resource_metrics=" + json.dumps({
            "search_normalization_ms": round(search_ms, 2),
            "fixture_fetch_extraction_ms": round(fetch_ms, 2),
            "orchestration_overhead_ms_excluding_model": round(orchestration_ms, 2),
            "approx_peak_python_heap_bytes": peak,
            "fixture_http_requests": len(FixtureHandler.requests),
            "external_network_calls": network["public"],
        }, sort_keys=True))
        print("phase7a_smoke=PASS")


if __name__ == "__main__":
    main()
