"""Vision Lite public surface (Phase 8A.1).

Importing this package is always safe: only standard-library modules load at
import time. Windows-only and optional third-party backends (python-mss,
RapidOCR, pywinauto, Playwright) load lazily inside the functions that need
them.
"""

from __future__ import annotations

from friday.vision.manager import VisionManager, summarize_dialog
from friday.vision.models import (
    CaptureMetadata,
    OcrBlock,
    OcrStatus,
    ScreenObservation,
    ScreenRegion,
    UiElement,
    VisionSource,
)


def create_vision_manager() -> VisionManager:
    """Build a VisionManager with lazy backends (nothing loads yet)."""
    return VisionManager()


__all__ = [
    "CaptureMetadata",
    "OcrBlock",
    "OcrStatus",
    "ScreenObservation",
    "ScreenRegion",
    "UiElement",
    "VisionManager",
    "VisionSource",
    "create_vision_manager",
    "summarize_dialog",
]
