"""Opt-in compact evidence bundles; research is never persisted by default."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
import json
import os

from friday.paths import DATA_ROOT
from friday.research.models import ResearchRun


class ResearchEvidenceStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root if root is not None else os.environ.get("FRIDAY_RESEARCH_DATA_PATH")
        self.root = Path(configured) if configured else DATA_ROOT / "research"

    def save(self, run: ResearchRun) -> Path:
        folder = self.root / "runs" / run.id
        sources_folder = folder / "sources"
        sources_folder.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run.id,
            "status": run.status.value,
            "question": run.plan.question,
            "search_queries": list(run.plan.search_queries),
            "minimum_independent_sources": run.plan.minimum_independent_sources,
            "created_at": run.events[0].timestamp if run.events else "",
            "claims": [asdict(item) for item in run.claims],
            "citations": run.citations,
        }
        self._write(folder / "manifest.json", manifest)
        for source in run.sources:
            # Normalized text and metadata only; raw HTML is never persisted.
            self._write(sources_folder / f"{source.citation_id or source.id}.json", asdict(source))
        return folder

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
