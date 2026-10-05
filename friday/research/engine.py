"""Bounded source collection, evidence deduplication, synthesis, and provenance."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from time import perf_counter
from typing import Any
import re

from friday.research.browser import PlaywrightResearchRenderer, ResearchBrowserRenderer
from friday.research.extract import extract_page
from friday.research.fetch import ResearchFetchError, ResearchFetcher
from friday.research.models import (
    ClaimKind,
    ExtractedPage,
    FetchResult,
    ResearchClaim,
    ResearchEvent,
    ResearchPlan,
    ResearchRun,
    ResearchSource,
    ResearchStatus,
    SearchResult,
)
from friday.research.planner import ResearchPlanner, ResearchQueryError
from friday.research.quality import classify_source, is_duplicate, rank_search_results, source_signals
from friday.research.search import SearchProvider, SearchProviderError, configured_search_providers
from friday.research.store import ResearchEvidenceStore
from friday.research.synthesis import ResearchSynthesizer
from friday.research.urls import UnsafeResearchURL, domain_of, normalize_url


class ResearchEngine:
    def __init__(
        self,
        brain_manager: object | None = None,
        *,
        search_providers: Sequence[SearchProvider] | None = None,
        fetcher: ResearchFetcher | None = None,
        synthesizer: Any | None = None,
        browser_renderer: ResearchBrowserRenderer | None = None,
        enable_browser_fallback: bool = True,
        planner: ResearchPlanner | None = None,
        evidence_store: ResearchEvidenceStore | None = None,
        event_sink: Callable[[ResearchEvent], None] | None = None,
    ) -> None:
        self.search_providers = tuple(search_providers) if search_providers is not None else configured_search_providers()
        self.fetcher = fetcher or ResearchFetcher()
        self.synthesizer = synthesizer or (ResearchSynthesizer(brain_manager) if brain_manager is not None else None)
        self.browser_renderer = browser_renderer
        self.enable_browser_fallback = bool(enable_browser_fallback)
        self.planner = planner or ResearchPlanner()
        self.evidence_store = evidence_store or ResearchEvidenceStore()
        self.event_sink = event_sink
        self._runs: dict[str, ResearchRun] = {}
        self._lock = RLock()

    def get_run(self, run_id: str) -> ResearchRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        if run is None or run.status in {ResearchStatus.COMPLETE, ResearchStatus.INSUFFICIENT_EVIDENCE,
                                         ResearchStatus.SEARCH_CONFIGURATION_REQUIRED, ResearchStatus.SEARCH_UNAVAILABLE,
                                         ResearchStatus.CANCELLED, ResearchStatus.FAILED}:
            return False
        run.cancel()
        return True

    def search(
        self,
        query: str,
        *,
        privacy: str = "public",
        max_results: int = 8,
        timeout_seconds: float = 10.0,
    ) -> list[SearchResult]:
        if not 1 <= max_results <= 8:
            raise ValueError("max_results must be from 1 to 8.")
        safe_query = self.planner.public_query(query, privacy=privacy)
        if not safe_query:
            raise SearchProviderError("PRIVATE_QUERY_BLOCKED", "No safe public query could be separated from the request.")
        clean_query = self.planner._minimal(safe_query)
        for provider in self.search_providers[:4]:
            try:
                rows = provider.search(clean_query, max_results, timeout_seconds=timeout_seconds)
            except SearchProviderError:
                continue
            except Exception:
                continue
            if rows:
                return rank_search_results(
                    clean_query, rows, limit=max_results, fixture_hosts=self.fetcher.fixture_hosts,
                )
        if not self.search_providers:
            raise SearchProviderError("SEARCH_CONFIGURATION_REQUIRED", "Set FRIDAY_RESEARCH_SEARXNG_URL to use search.")
        raise SearchProviderError("SEARCH_UNAVAILABLE")

    def fetch(self, url: str, *, timeout_seconds: float | None = None) -> ExtractedPage:
        fetched = self.fetcher.fetch(url, timeout_seconds=timeout_seconds)
        if fetched.error_code:
            raise ResearchFetchError(fetched.error_code, "Rich document parsing is deferred to Phase 7B.")
        try:
            return extract_page(fetched)
        except (ValueError, UnicodeError):
            if not self.enable_browser_fallback:
                raise ResearchFetchError("EXTRACTION_FAILED", "Static page extraction did not find usable text.") from None
            renderer = self.browser_renderer or PlaywrightResearchRenderer()
            return renderer.render(
                fetched.final_url,
                timeout_seconds=min(self.fetcher.timeout_seconds, timeout_seconds or self.fetcher.timeout_seconds),
                max_text_chars=100_000,
            )

    def run(
        self,
        question: str,
        *,
        privacy: str = "public",
        freshness: str = "current",
        max_sources: int = 8,
        timeout_budget_seconds: float = 45.0,
        preferred_domains: Sequence[str] = (),
        excluded_domains: Sequence[str] = (),
        source_urls: Sequence[str] = (),
        persist: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> ResearchRun:
        started = perf_counter()
        try:
            plan = self.planner.plan(
                question,
                privacy=privacy,
                freshness=freshness,
                max_sources=max_sources,
                timeout_budget_seconds=timeout_budget_seconds,
                preferred_domains=preferred_domains,
                excluded_domains=excluded_domains,
                persist=persist,
            )
        except ResearchQueryError as error:
            plan = ResearchPlan(
                question="Private or local-only research request",
                search_queries=(),
                max_sources=max_sources,
                timeout_budget_seconds=timeout_budget_seconds,
                freshness=freshness,
                privacy=privacy,
                persist=False,
                sanitized=False,
            )
            run = self._new_run(plan)
            run.status = ResearchStatus.INSUFFICIENT_EVIDENCE
            run.error_code = error.code
            if privacy == "local_only":
                run.error_code = "LOCAL_ONLY_NO_EXTERNAL_SEARCH"
                run.answer = "No external search was performed because the request was local-only."
            else:
                run.answer = "No safe public search query could be separated from this request. No external search was performed."
            self._emit(run, "research_started")
            self._emit(run, "research_completed", status=run.status.value)
            return run
        run = self._new_run(plan)
        self._emit(run, "research_started")
        deadline = started + plan.timeout_budget_seconds

        def is_cancelled() -> bool:
            return run.cancelled() or bool(cancelled is not None and cancelled())

        def timed_out() -> bool:
            return perf_counter() >= deadline

        if is_cancelled():
            return self._cancel_run(run)
        if privacy == "local_only":
            run.status = ResearchStatus.INSUFFICIENT_EVIDENCE
            run.error_code = "LOCAL_ONLY_NO_EXTERNAL_SEARCH"
            run.answer = "No external search was performed because the request is local-only."
            self._emit(run, "research_completed", status=run.status.value)
            return run

        selected: list[SearchResult] = []
        search_call_count = 0
        if source_urls:
            if privacy != "public" and not plan.sanitized:
                run.status = ResearchStatus.INSUFFICIENT_EVIDENCE
                run.error_code = "PRIVATE_QUERY_BLOCKED"
                run.answer = "No external page was fetched because the request could not be sanitized."
                self._emit(run, "research_completed", status=run.status.value)
                return run
            for index, url in enumerate(source_urls[:plan.max_sources], 1):
                if is_cancelled():
                    return self._cancel_run(run)
                if timed_out():
                    run.error_code = "TIME_BUDGET_EXHAUSTED"
                    break
                selected.append(SearchResult(
                    title="User-selected source", url=str(url), snippet="", source="user_selected", rank=index,
                ))
        else:
            if not self.search_providers:
                run.status = ResearchStatus.SEARCH_CONFIGURATION_REQUIRED
                run.error_code = "SEARCH_CONFIGURATION_REQUIRED"
                run.answer = "LIVE_SEARCH_CONFIGURATION_REQUIRED: set FRIDAY_RESEARCH_SEARXNG_URL to a zero-cost SearXNG instance."
                self._emit(run, "research_completed", status=run.status.value)
                return run
            for query_index, query in enumerate(plan.search_queries, 1):
                if is_cancelled():
                    return self._cancel_run(run)
                if timed_out():
                    run.error_code = "TIME_BUDGET_EXHAUSTED"
                    break
                self._emit(run, "search_started", index=query_index, total=len(plan.search_queries))
                found = False
                for provider in self.search_providers[:plan.max_search_queries]:
                    if search_call_count >= plan.max_search_queries:
                        break
                    if is_cancelled():
                        return self._cancel_run(run)
                    if timed_out():
                        break
                    remaining = max(0.1, deadline - perf_counter())
                    search_call_count += 1
                    try:
                        rows = provider.search(query, plan.max_sources, timeout_seconds=min(10.0, remaining))
                    except SearchProviderError as error:
                        run.error_code = error.code
                        continue
                    except Exception:
                        run.error_code = "SEARCH_PROVIDER_ERROR"
                        continue
                    normalized = rank_search_results(
                        query, rows, limit=plan.max_sources,
                        preferred_domains=plan.preferred_domains,
                        excluded_domains=plan.excluded_domains,
                        freshness=plan.freshness,
                        fixture_hosts=self.fetcher.fixture_hosts,
                    )
                    if normalized:
                        selected.extend(normalized)
                        found = True
                        break
                self._emit(run, "search_completed", index=query_index, total=len(plan.search_queries), status="RESULTS" if found else "EMPTY_OR_FAILED")
                # One deterministic query is the normal path; future query expansion remains capped at four.
                if found and query_index >= len(plan.search_queries):
                    break
        if is_cancelled():
            return self._cancel_run(run)
        if not selected and run.error_code and run.error_code.startswith("SEARCH_"):
            run.status = ResearchStatus.SEARCH_UNAVAILABLE
            run.answer = "INSUFFICIENT_EVIDENCE: configured search providers failed or returned no usable results."
            self._emit(run, "research_completed", status=run.status.value)
            return run

        selected = self._dedupe_search_results(selected, fixture_hosts=self.fetcher.fixture_hosts)[:plan.max_sources]
        seen_requested: set[str] = set()
        fetched_count = 0
        browser_fallbacks = 0
        for index, item in enumerate(selected, 1):
            if is_cancelled():
                return self._cancel_run(run)
            if timed_out():
                run.error_code = "TIME_BUDGET_EXHAUSTED"
                break
            try:
                requested = normalize_url(item.url, fixture_hosts=self.fetcher.fixture_hosts)
            except UnsafeResearchURL as error:
                self._emit(run, "source_failed", index=index, total=len(selected), status=error.code)
                continue
            if requested in seen_requested or fetched_count >= plan.max_sources:
                continue
            seen_requested.add(requested)
            fetched_count += 1
            self._emit(run, "source_selected", index=fetched_count, total=min(plan.max_sources, len(selected)))
            if is_cancelled():
                return self._cancel_run(run)
            remaining = deadline - perf_counter()
            if remaining <= 0:
                run.error_code = "TIME_BUDGET_EXHAUSTED"
                break
            try:
                fetched = self.fetcher.fetch(requested, timeout_seconds=min(self.fetcher.timeout_seconds, remaining))
                if fetched.error_code:
                    source = self._inaccessible_source(item, requested, fetched, fetched.error_code, len(run.sources) + 1)
                    self._append_source(run, source)
                    self._emit(run, "source_failed", index=fetched_count, total=min(plan.max_sources, len(selected)), status=fetched.error_code)
                    continue
                try:
                    page = extract_page(fetched)
                except (ValueError, UnicodeError):
                    if not self.enable_browser_fallback or browser_fallbacks >= min(plan.max_browser_fallbacks, 2):
                        raise ResearchFetchError("EXTRACTION_FAILED", "Static page extraction did not find usable text.") from None
                    if is_cancelled():
                        return self._cancel_run(run)
                    renderer = self.browser_renderer or PlaywrightResearchRenderer()
                    browser_fallbacks += 1
                    page = renderer.render(
                        fetched.final_url,
                        timeout_seconds=min(self.fetcher.timeout_seconds, max(0.1, deadline - perf_counter())),
                        max_text_chars=100_000,
                    )
                    if is_cancelled():
                        return self._cancel_run(run)
                source = self._source(item, requested, fetched, page, len(run.sources) + 1)
                access_block = self._access_block(page.title, page.text)
                if access_block:
                    inaccessible = replace(source, text="", content_hash="", accessible=False, error_code=access_block)
                    self._append_source(run, inaccessible)
                    self._emit(run, "source_failed", index=fetched_count, total=min(plan.max_sources, len(selected)), status=access_block)
                    continue
                if self._relevant(source, plan.question) and not is_duplicate(source, run.sources):
                    self._append_source(run, source)
                    self._emit(run, "source_fetched", index=fetched_count, total=min(plan.max_sources, len(selected)), status="OK")
                elif not is_duplicate(source, run.sources):
                    self._emit(run, "source_failed", index=fetched_count, total=min(plan.max_sources, len(selected)), status="LOW_RELEVANCE")
                if self._enough_sources(run, plan):
                    break
            except ResearchFetchError as error:
                inaccessible = self._failed_source(item, requested, error.code, len(run.sources) + 1)
                self._append_source(run, inaccessible)
                self._emit(run, "source_failed", index=fetched_count, total=min(plan.max_sources, len(selected)), status=error.code)
            except Exception:
                inaccessible = self._failed_source(item, requested, "FETCH_FAILED", len(run.sources) + 1)
                self._append_source(run, inaccessible)
                self._emit(run, "source_failed", index=fetched_count, total=min(plan.max_sources, len(selected)), status="FETCH_FAILED")

        if is_cancelled():
            return self._cancel_run(run)
        accessible = [source for source in run.sources if source.accessible]
        if timed_out():
            run.error_code = run.error_code or "TIME_BUDGET_EXHAUSTED"
        if accessible and self.synthesizer is not None and not timed_out():
            if is_cancelled():
                return self._cancel_run(run)
            self._emit(run, "research_synthesizing", total=len(accessible))
            try:
                run.claims = self.synthesizer.synthesize(plan, accessible, cancelled=is_cancelled)
            except Exception:
                run.error_code = run.error_code or "SYNTHESIS_UNAVAILABLE"
                run.claims = []
            if is_cancelled():
                return self._cancel_run(run)
        valid_claims = [claim for claim in run.claims if claim.kind in {ClaimKind.SOURCE_DERIVED_FACT, ClaimKind.MODEL_SYNTHESIS} and claim.citation_ids]
        enough = self._enough_sources(run, plan)
        if valid_claims and enough and not timed_out():
            run.status = ResearchStatus.COMPLETE
            run.answer = "Research synthesis completed with source-linked claims."
        else:
            if timed_out():
                run.error_code = run.error_code or "TIME_BUDGET_EXHAUSTED"
            run.status = ResearchStatus.INSUFFICIENT_EVIDENCE
            run.answer = "INSUFFICIENT_EVIDENCE: the collected sources did not support enough citation-validated claims."
        run.citations = {
            source.citation_id: {"title": source.title, "url": source.final_url, "requested_url": source.requested_url}
            for source in run.sources if source.citation_id
        }
        if plan.persist:
            self.evidence_store.save(run)
        self._emit(run, "research_completed", status=run.status.value, duration_ms=round((perf_counter() - started) * 1000, 2))
        return run

    def format_result(self, run: ResearchRun) -> str:
        lines = [f"Research status: {run.status.value}"]
        if run.error_code:
            lines.append(f"Status detail: {run.error_code}")
        if run.status == ResearchStatus.SEARCH_CONFIGURATION_REQUIRED:
            lines.append("LIVE_SEARCH_CONFIGURATION_REQUIRED: Configure a zero-cost SearXNG endpoint with FRIDAY_RESEARCH_SEARXNG_URL.")
        if run.status == ResearchStatus.CANCELLED:
            lines.append("Research was cancelled before remaining searches, fetches, or synthesis could start.")
        if run.status == ResearchStatus.INSUFFICIENT_EVIDENCE:
            lines.append(run.answer or "INSUFFICIENT_EVIDENCE")
        if run.status in {ResearchStatus.COMPLETE, ResearchStatus.INSUFFICIENT_EVIDENCE}:
            for claim in run.claims:
                citations = " ".join(f"[{item}]" for item in claim.citation_ids)
                lines.append(f"{claim.kind.value}: {claim.text}" + (f" {citations}" if citations else ""))
        lines.append("Sources:")
        if not run.sources:
            lines.append("(none)")
        for source in run.sources:
            label = source.citation_id or source.id
            if source.accessible:
                published = source.published_at or "publication date unknown"
                lines.append(f"[{label}] {source.title} (published: {published}; retrieved: {source.retrieved_at}) — {source.final_url}")
            else:
                lines.append(f"[{label}] INACCESSIBLE ({source.error_code or 'FETCH_FAILED'}) — {source.requested_url}")
        return "\n".join(lines)[:20_000]

    def _new_run(self, plan: ResearchPlan) -> ResearchRun:
        run = ResearchRun(plan=plan)
        run.status = ResearchStatus.RUNNING
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > 32:
                self._runs.pop(next(iter(self._runs)))
        return run

    def _emit(self, run: ResearchRun, event: str, *, index: int | None = None,
              total: int | None = None, status: str | None = None,
              duration_ms: float | None = None) -> None:
        item = ResearchEvent(event, run.id, index=index, total=total, status=status, duration_ms=duration_ms)
        run.events.append(item)
        if self.event_sink is not None:
            try:
                self.event_sink(item)
            except Exception:
                pass

    def _cancel_run(self, run: ResearchRun) -> ResearchRun:
        run.status = ResearchStatus.CANCELLED
        run.error_code = "CANCELLED"
        run.answer = "Research was cancelled. No further research work was started."
        self._emit(run, "research_cancelled", status=run.status.value)
        return run

    @staticmethod
    def _dedupe_search_results(rows: list[SearchResult], *, fixture_hosts: frozenset[str] = frozenset()) -> list[SearchResult]:
        seen: set[str] = set()
        output = []
        for row in rows:
            try:
                url = normalize_url(row.url, fixture_hosts=fixture_hosts)
            except UnsafeResearchURL:
                continue
            if url in seen:
                continue
            seen.add(url)
            output.append(replace(row, url=url))
        return output

    def _source(self, item: SearchResult, requested: str, fetched: FetchResult,
                page: ExtractedPage, index: int) -> ResearchSource:
        final = normalize_url(fetched.final_url, fixture_hosts=self.fetcher.fixture_hosts)
        try:
            canonical = normalize_url(page.canonical_url, fixture_hosts=self.fetcher.fixture_hosts)
        except UnsafeResearchURL:
            canonical = final
        citation = f"S{index}"
        preliminary = ResearchSource(
            id=citation,
            requested_url=requested,
            final_url=final,
            canonical_url=canonical,
            title=page.title[:500],
            domain=domain_of(final),
            author=page.author,
            published_at=page.published_at or item.published_at,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            text=page.text,
            headings=page.headings,
            content_hash=page.content_hash,
            source_type="user_selected" if item.source == "user_selected" else classify_source(domain_of(final)),
            reliability_metadata={
                "search_provider": item.source,
                "search_rank": item.rank,
                "relevance_terms_matched": item.metadata.get("relevance_terms_matched"),
                "publication_date_exposed": bool(page.published_at or item.published_at),
            },
            citation_id=citation,
            content_type=fetched.content_type,
        )
        return replace(preliminary, reliability_metadata={**dict(preliminary.reliability_metadata), **source_signals(preliminary)})

    def _failed_source(self, item: SearchResult, requested: str, error_code: str, index: int) -> ResearchSource:
        return ResearchSource(
            id=f"S{index}", requested_url=requested, final_url=requested, canonical_url=requested,
            title=item.title or requested, domain=domain_of(requested), author=None,
            published_at=item.published_at, retrieved_at=datetime.now(timezone.utc).isoformat(),
            text="", headings=(), content_hash="", source_type=classify_source(domain_of(requested)),
            reliability_metadata={"search_provider": item.source, "search_rank": item.rank},
            citation_id=f"S{index}", accessible=False, error_code=error_code,
        )

    def _inaccessible_source(self, item: SearchResult, requested: str, fetched: FetchResult,
                             error_code: str, index: int) -> ResearchSource:
        failed = self._failed_source(item, requested, error_code, index)
        return replace(failed, final_url=fetched.final_url, content_type=fetched.content_type)

    def _append_source(self, run: ResearchRun, source: ResearchSource) -> None:
        if is_duplicate(source, run.sources):
            return
        run.sources.append(source)

    @staticmethod
    def _relevant(source: ResearchSource, question: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]{3,}", question.casefold()))
        if not tokens:
            return bool(source.text)
        source_tokens = set(re.findall(r"[a-z0-9]{3,}", f"{source.title} {source.text[:8000]}".casefold()))
        return bool(tokens & source_tokens)

    @staticmethod
    def _access_block(title: str, text: str) -> str | None:
        sample = f"{title} {text[:3000]}".casefold()
        if re.search(r"\b(?:verify you are human|complete the captcha|captcha required|checking your browser|automated requests detected)\b", sample):
            return "CAPTCHA_OR_ANTI_BOT_REQUIRED"
        if re.search(r"\b(?:subscribe to continue|subscribe to read|subscription required|paywall|purchase access to continue)\b", sample):
            return "PAYWALL_REQUIRED"
        if re.search(r"\b(?:sign in to continue|log in to continue|authentication required|members only)\b", sample):
            return "AUTHENTICATION_REQUIRED"
        return None

    def _enough_sources(self, run: ResearchRun, plan: ResearchPlan) -> bool:
        available = [item for item in run.sources if item.accessible]
        domains = {item.domain for item in available}
        needed = plan.minimum_independent_sources
        return len(available) >= needed and len(domains) >= needed
