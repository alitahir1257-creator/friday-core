"""On-demand screenshot capture (Phase 8A.1).

Design rules:
- No daemon, no continuous capture, no screen recording. On-demand only.
- ``python-mss`` is imported lazily; without it capture reports NOT_INSTALLED
  instead of crashing the process.
- PNG encoding uses ``mss.tools.to_png`` (pure Python, ships with mss), so no
  Pillow/OpenCV dependency is needed for capture itself.
- Screenshots live in memory by default. A temp file is written only when the
  caller explicitly asks (``persist=True``), outside the repo, and the caller
  owns deletion. Nothing is ever stored permanently by default.
- All regions are bounded and validated before any capture happens.
"""

from __future__ import annotations

import os
import tempfile

from friday.vision.models import CaptureMetadata, ScreenRegion, utcnow


_MAX_DIMENSION = 8192
_MAX_PIXELS = 7680 * 4320  # hard 8K bound; the 8 GB target never needs more


class CaptureError(ValueError):
    """Deterministic capture failure (missing backend, bad region, ...)."""


def validate_region(region: ScreenRegion | None) -> ScreenRegion | None:
    """Reject unbounded or malformed regions before any capture happens."""
    if region is None:
        return None
    for name in ("x", "y", "width", "height", "monitor"):
        value = getattr(region, name)
        if type(value) is not int:
            raise CaptureError(f"Region {name} must be an integer.")
    if region.x < 0 or region.y < 0:
        raise CaptureError("Region origin must be non-negative.")
    if region.width <= 0 or region.height <= 0:
        raise CaptureError("Region dimensions must be positive.")
    if region.width > _MAX_DIMENSION or region.height > _MAX_DIMENSION:
        raise CaptureError(f"Region exceeds the {_MAX_DIMENSION}px bound.")
    if region.width * region.height > _MAX_PIXELS:
        raise CaptureError("Region exceeds the maximum pixel budget.")
    if region.monitor < 1:
        raise CaptureError("Monitor numbers start at 1.")
    return region


class CapturedImage:
    """In-memory screenshot plus normalized metadata."""

    def __init__(self, png_bytes: bytes, width: int, height: int,
                 metadata: CaptureMetadata) -> None:
        self.png_bytes = png_bytes
        self.width = width
        self.height = height
        self.metadata = metadata


class ScreenCapture:
    """Thin lazy wrapper around python-mss. Near-zero idle cost."""

    backend = "python-mss"

    def __init__(self) -> None:
        self._sct = None

    def _ensure(self):
        if self._sct is not None:
            return self._sct
        try:
            import mss
        except ImportError as error:
            raise CaptureError(
                "python-mss is not installed; screenshot capture is unavailable."
            ) from error
        self._sct = mss.mss()
        return self._sct

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            finally:
                self._sct = None

    def capture(
        self,
        region: ScreenRegion | None = None,
        monitor: int = 1,
        persist: bool = False,
    ) -> CapturedImage:
        """Capture one screenshot. Raises CaptureError on any failure."""
        region = validate_region(region)
        if type(monitor) is not int or monitor < 1:
            raise CaptureError("Monitor numbers start at 1.")
        sct = self._ensure()
        try:
            from mss import tools
        except ImportError as error:  # pragma: no cover - ships with mss
            raise CaptureError("python-mss tools are unavailable.") from error

        monitors = sct.monitors
        if monitor >= len(monitors):
            raise CaptureError(f"Monitor {monitor} is not available.")
        mon = monitors[monitor]
        if region is None:
            grab_box = {"left": mon["left"], "top": mon["top"],
                        "width": mon["width"], "height": mon["height"]}
            used_region = None
        else:
            grab_box = {"left": mon["left"] + region.x, "top": mon["top"] + region.y,
                        "width": region.width, "height": region.height}
            used_region = region

        try:
            shot = sct.grab(grab_box)
        except Exception as error:
            raise CaptureError(f"Screenshot capture failed: {type(error).__name__}.") from error

        png_bytes = tools.to_png(shot.rgb, shot.size)
        temp_path: str | None = None
        if persist:
            fd, temp_path = tempfile.mkstemp(prefix="friday-vision-", suffix=".png")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(png_bytes)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

        metadata = CaptureMetadata(
            width=shot.width, height=shot.height, region=used_region,
            monitor=monitor, timestamp=utcnow(), backend=self.backend,
            persisted=persist, temp_path=temp_path,
        )
        return CapturedImage(png_bytes, shot.width, shot.height, metadata)
