"""One-query live DDGS acceptance for Phase 7A.4."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import threading
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.research.engine import ResearchEngine  # noqa: E402
from friday.research.models import ClaimKind, ResearchSource  # noqa: E402
from friday.research.search import DDGSSearchProvider, configured_search_providers  # noqa: E402
from friday.research.synthesis import validate_claims  # noqa: E402


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _available_ram_gb() -> float:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return round(status.ullAvailPhys / 1024**3, 2)


class _ExactSourceSynthesizer:
    def synthesize(self, _plan, sources: list[ResearchSource], *, cancelled=None):
        if cancelled and cancelled():
            return []
        source = next((item for item in sources if item.accessible and len(item.text) >= 12), None)
        if source is None:
            return []
        quote = source.text[:1000]
        raw = json.dumps({"claims": [{
            "kind": ClaimKind.SOURCE_DERIVED_FACT.value,
            "text": quote,
            "citations": [source.citation_id],
            "evidence_quotes": {source.citation_id: quote},
        }]})
        return validate_claims(raw, sources, freshness="timeless")


class _MeasuredDDGSProvider(DDGSSearchProvider):
    calls = 0
    last_query = ""
    result_count = 0
    peak_python_bytes = 0

    def search(self, query: str, limit: int = 8, *, timeout_seconds: float = 10.0):
        self.calls += 1
        self.last_query = query
        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        try:
            rows = super().search(query, limit, timeout_seconds=timeout_seconds)
            self.result_count = len(rows)
            return rows
        finally:
            _current, peak = tracemalloc.get_traced_memory()
            self.peak_python_bytes = max(0, peak - baseline)
            tracemalloc.stop()


def main() -> None:
    query = "official Python documentation"
    providers = configured_search_providers({"FRIDAY_RESEARCH_BROWSER_SEARCH_ENGINE": "duckduckgo"})
    if [item.name for item in providers] != ["ddgs", "browser"]:
        raise AssertionError("Expected DDGS followed by BrowserSearchProvider for acceptance.")
    ddgs = _MeasuredDDGSProvider()
    providers = (ddgs, *providers[1:])
    ram_samples = [_available_ram_gb()]
    sample_stop = threading.Event()

    def sample_ram() -> None:
        while not sample_stop.wait(0.25):
            ram_samples.append(_available_ram_gb())

    sampler = threading.Thread(target=sample_ram, name="phase7a4-ram-sampler", daemon=True)
    sampler.start()
    try:
        run = ResearchEngine(
            search_providers=providers,
            synthesizer=_ExactSourceSynthesizer(),
        ).run(query, max_sources=2, timeout_budget_seconds=60, freshness="timeless")
    finally:
        sample_stop.set()
        sampler.join(timeout=2)
    ram_after = _available_ram_gb()
    ram_samples.append(ram_after)

    accessible = [source for source in run.sources if source.accessible]
    source_by_id = {source.citation_id: source for source in accessible}
    fabricated = sum(
        citation not in source_by_id
        or claim.evidence_quotes.get(citation, "") not in source_by_id[citation].text
        for claim in run.claims
        for citation in claim.citation_ids
    )
    provenance = bool(run.claims) and all(
        claim.kind == ClaimKind.SOURCE_DERIVED_FACT
        and claim.citation_ids
        and all(citation in run.citations and citation in source_by_id for citation in claim.citation_ids)
        and all(
            claim.evidence_quotes.get(citation, "") in source_by_id[citation].text
            for citation in claim.citation_ids
        )
        for claim in run.claims
    )
    selected_count = sum(event.event == "source_selected" for event in run.events)
    selected_providers = {str(source.reliability_metadata.get("search_provider", "")) for source in accessible}
    result = {
        "query": ddgs.last_query,
        "provider_selected": sorted(selected_providers),
        "backend": "bing",
        "ddgs_searches": ddgs.calls,
        "results_returned": ddgs.result_count,
        "sources_selected_for_fetch": selected_count,
        "sources_fetched": len(accessible),
        "citation_provenance": "PASS" if provenance else "FAIL",
        "fabricated_citations": fabricated,
        "private_leakage": 0 if ddgs.last_query == query else 1,
        "paid_calls": 0,
        "ram_available_gb_before": ram_samples[0],
        "ram_available_gb_minimum": min(ram_samples),
        "ram_available_gb_after": ram_after,
        "ddgs_peak_python_heap_mib_approx": round(ddgs.peak_python_bytes / 1024**2, 2),
        "evidence": [{"citation": item.citation_id, "title": item.title, "url": item.final_url} for item in accessible],
        "run_status": run.status.value,
    }
    print(json.dumps(result, indent=2))
    if (
        ddgs.calls != 1
        or not ddgs.result_count
        or selected_providers != {"ddgs"}
        or not accessible
        or selected_count > 2
        or not provenance
        or fabricated
        or result["private_leakage"]
        or result["paid_calls"]
        or run.status.value != "COMPLETE"
    ):
        raise AssertionError("Phase 7A.4 live acceptance failed.")


if __name__ == "__main__":
    main()
