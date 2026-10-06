"""Deterministic fusion of UIA, DOM, and OCR evidence.

Rules:
- UIA and DOM are preferred; OCR is fallback/supplement.
- Obvious duplicates are merged by normalized text. No LLM is used for
  dedupe: plain casefold + whitespace collapsing, fully deterministic.
- Provenance is preserved on every item: nothing is merged silently.
- Screen text stays UNTRUSTED DATA; fusion never executes or interprets it.
"""

from __future__ import annotations

import re

from friday.vision.models import OcrBlock, UiElement, VisionSource


_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Canonical form used only for duplicate detection."""
    return _WHITESPACE.sub(" ", str(value).casefold()).strip()


def fuse(uia_elements: tuple[UiElement, ...] = (),
         dom_elements: tuple[UiElement, ...] = (),
         ocr_blocks: tuple[OcrBlock, ...] = ()) -> tuple[tuple[tuple[VisionSource, str], ...], str]:
    """Merge evidence into ((source, text), ...) plus deduped plain text.

    Preference order: UIA, then DOM, then OCR. An OCR block whose normalized
    text already appeared in UIA/DOM evidence is dropped as a duplicate; its
    provenance is not lost because the surviving item keeps its own source.
    """
    fused: list[tuple[VisionSource, str]] = []
    seen: set[str] = set()

    def add(source: VisionSource, text: str) -> None:
        cleaned = str(text).strip()
        if not cleaned:
            return
        key = normalize_text(cleaned)
        if not key or key in seen:
            return
        seen.add(key)
        fused.append((source, cleaned))

    for element in uia_elements:
        add(VisionSource.UIA, element.name)
    for element in dom_elements:
        add(VisionSource.DOM, element.name)
    for block in ocr_blocks:
        add(VisionSource.OCR, block.text)

    fused_text = "\n".join(text for _, text in fused)
    return tuple(fused), fused_text


def provenance_labels(fused: tuple[tuple[VisionSource, str], ...]) -> tuple[str, ...]:
    """Compact per-item provenance, e.g. ('UIA:Retry', 'OCR:Connection failed')."""
    return tuple(f"{source.value}:{text[:60]}" for source, text in fused)
