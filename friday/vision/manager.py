"""VisionManager: on-demand screen observation orchestrator (Phase 8A.1).

One coordinator, no framework:
  capture (mss, lazy) -> UIA/DOM evidence (existing controllers, preferred)
  -> OCR (RapidOCR adapter, lazy, fallback only) -> deterministic fusion
  -> normalized ScreenObservation.

Invariants:
- On-demand only. No daemon, no continuous capture, no recording.
- Near-zero idle cost: backends construct lazily and release() drops them.
- Local-only: remote_calls is always 0. No memory writes, no uploads, no
  OmniRoute/FreeLLMAPI/Cloudflare. PRIVATE stays fully local.
- Screenshots live in memory unless persist_screenshot=True is passed
  explicitly; temp files are deleted deterministically.
- Screen text is UNTRUSTED DATA: observation is evidence, never authority.
"""

from __future__ import annotations

import os

from friday.vision.capture import CaptureError, ScreenCapture, validate_region
from friday.vision.evidence import (EvidenceError, active_window_identity,
                                    active_window_title, collect_dom, collect_uia,
                                    explicit_window_info, resolve_visible_window_title)
from friday.vision.fusion import fuse, provenance_labels
from friday.vision.models import (
    OcrStatus,
    ScreenObservation,
    ScreenRegion,
    VisionSource,
    utcnow,
)
from friday.vision.ocr import OcrEngine


_OCR_MODES = ("auto", "always", "never")


def _coerce_region(region) -> ScreenRegion | None:
    if region is None or isinstance(region, ScreenRegion):
        return region
    if isinstance(region, dict):
        return ScreenRegion(
            x=int(region.get("x", 0)), y=int(region.get("y", 0)),
            width=int(region["width"]), height=int(region["height"]),
            monitor=int(region.get("monitor", 1)),
            label=str(region.get("label", "region")),
        )
    raise CaptureError("Region must be a ScreenRegion or a dict.")


class VisionManager:
    """Owns lazy vision backends and builds normalized observations."""

    def __init__(self, capture: ScreenCapture | None = None,
                 ocr: OcrEngine | None = None) -> None:
        self._capture = capture
        self._ocr = ocr
        self._grounded_targets: dict[str, tuple[object, ScreenObservation]] = {}

    def _capture_backend(self) -> ScreenCapture:
        if self._capture is None:
            self._capture = ScreenCapture()
        return self._capture

    def _ocr_backend(self) -> OcrEngine:
        if self._ocr is None:
            self._ocr = OcrEngine()
        return self._ocr

    def observe(
        self,
        *,
        region=None,
        monitor: int = 1,
        window_target: str | None = None,
        include_dom: bool = False,
        ocr_mode: str = "auto",
        persist_screenshot: bool = False,
        browser=None,
        uia_controller=None,
        window_handle: int | None = None,
    ) -> ScreenObservation:
        """Build one normalized screen observation. Never raises for backend
        problems; unavailable pieces are reported, not hidden."""
        if ocr_mode not in _OCR_MODES:
            raise ValueError(f"ocr_mode must be one of {_OCR_MODES}.")
        if window_handle is not None and window_target is not None:
            raise ValueError("Provide either window_handle or window_target, not both.")
        coerced = _coerce_region(region)
        validate_region(coerced)

        temp_path: str | None = None
        png_bytes: bytes | None = None
        dimensions: tuple[int, int] | None = None
        capture_bounds = None
        window_identity = None if window_handle is not None else active_window_identity()
        explicit_title = None
        if window_handle is not None:
            explicit_title, bounds = explicit_window_info(window_handle)
            window_identity = (window_handle, bounds)
        elif window_identity is None and window_target:
            window_handle = resolve_visible_window_title(window_target)
            if window_handle is not None:
                explicit_title, bounds = explicit_window_info(window_handle)
                window_identity = (window_handle, bounds)
        if window_identity is None:
            return ScreenObservation(timestamp=utcnow(), region=None, dimensions=None,
                                     active_window=None, remote_calls=0,
                                     status="WINDOW_TARGET_REQUIRED")
        if window_handle is not None and coerced is not None:
            raise ValueError("An explicit window handle cannot be combined with a screen region.")
        try:
            try:
                capture = self._capture_backend()
                if window_handle is None:
                    image = capture.capture(region=coerced, monitor=monitor,
                                            persist=persist_screenshot)
                else:
                    image = capture.capture_window(window_handle, persist=persist_screenshot)
                png_bytes = image.png_bytes
                dimensions = (image.width, image.height)
                capture_bounds = (*image.metadata.screen_origin, image.width, image.height)
                temp_path = image.metadata.temp_path
            except CaptureError:
                if window_handle is not None:
                    raise
                png_bytes, dimensions = None, None

            try:
                uia_elements = collect_uia(window_target, controller=uia_controller,
                                           window_handle=window_handle)
            except EvidenceError:
                if window_handle is not None:
                    _current_title, current_bounds = explicit_window_info(window_handle)
                    if window_identity != (window_handle, current_bounds):
                        raise EvidenceError("The explicit window changed during UIA inspection.")
                uia_elements = ()
            if window_handle is not None:
                active_window = explicit_title
            elif window_target is None:
                active_window = active_window_title()
            else:
                active_window = window_target

            dom_elements: tuple = ()
            if include_dom and window_handle is None:
                try:
                    dom_elements = collect_dom(browser=browser)
                except EvidenceError:
                    dom_elements = ()

            ocr_blocks: tuple = ()
            ocr_status = self._ocr.status if self._ocr is not None else OcrStatus.NOT_INSTALLED
            want_ocr = (
                ocr_mode == "always"
                or (ocr_mode == "auto" and not uia_elements and not dom_elements)
            )
            if want_ocr and png_bytes is not None:
                ocr_status, ocr_blocks = self._ocr_backend().read(png_bytes)
            elif want_ocr and png_bytes is None:
                ocr_status = OcrStatus.OCR_REQUIRED

            fused, fused_text = fuse(uia_elements, dom_elements, ocr_blocks)
            if window_handle is not None:
                _title, current_bounds = explicit_window_info(window_handle)
                if window_identity != (window_handle, current_bounds):
                    raise EvidenceError("The explicit window changed during observation.")
            elif window_identity != active_window_identity():
                window_identity = None  # foreground changed during observation
            return ScreenObservation(
                timestamp=utcnow(), region=coerced, dimensions=dimensions,
                active_window=active_window, uia_elements=uia_elements,
                dom_elements=dom_elements, ocr_blocks=ocr_blocks,
                fused_text=fused_text, ocr_status=ocr_status, remote_calls=0,
                provenance=provenance_labels(fused),
                capture_bounds=capture_bounds,
                window_bounds=window_identity[1] if window_identity else None,
                window_handle=window_identity[0] if window_identity else None,
            )
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def observe_window(self, hwnd: int, *, include_dom: bool = False,
                       ocr_mode: str = "auto", persist_screenshot: bool = False,
                       uia_controller=None) -> ScreenObservation:
        """Observe only the visible window identified by this explicit HWND."""
        return self.observe(include_dom=include_dom, ocr_mode=ocr_mode,
                            persist_screenshot=persist_screenshot,
                            uia_controller=uia_controller, window_handle=hwnd)

    def find_target(self, query: str, *, observation: ScreenObservation | None = None,
                    active_window_only: bool = True, max_candidates: int = 5,
                    window_handle: int | None = None, window_target: str | None = None):
        """Resolve existing evidence first; collect OCR only after a UIA miss."""
        from friday.vision.targets import TargetResolution, resolve_target

        if observation is not None:
            result = resolve_target(query, observation, active_window_only=active_window_only,
                                    max_candidates=max_candidates)
            return self._remember_target(result, observation)
        if window_handle is not None and window_target is not None:
            raise ValueError("Provide either window_handle or window_target, not both.")
        if window_handle is not None:
            observed = self.observe_window(window_handle, ocr_mode="auto")
        elif window_target is not None:
            observed = self.observe(window_target=window_target, ocr_mode="auto")
        else:
            observed = self.observe(ocr_mode="auto")
        if observed.status != "SUCCESS":
            return TargetResolution(observed.status)
        result = resolve_target(query, observed, active_window_only=active_window_only,
                                max_candidates=max_candidates)
        if result.status != "NOT_FOUND" or observed.ocr_blocks:
            return self._remember_target(result, observed)
        if window_handle is not None:
            fallback = self.observe_window(window_handle, ocr_mode="always")
        elif window_target is not None:
            fallback = self.observe(window_target=window_target, ocr_mode="always")
        else:
            fallback = self.observe(ocr_mode="always")
        if fallback.status != "SUCCESS":
            return TargetResolution(fallback.status)
        result = resolve_target(query, fallback, active_window_only=active_window_only,
                                max_candidates=max_candidates)
        return self._remember_target(result, fallback)

    def _remember_target(self, result, observation: ScreenObservation):
        if result.status == "SUCCESS" and result.target is not None:
            self._grounded_targets[result.target.id] = (result.target, observation)
            while len(self._grounded_targets) > 128:
                self._grounded_targets.pop(next(iter(self._grounded_targets)))
        return result

    def grounded_target(self, target_id: str, observation_id: str | None = None):
        """Return only targets created by this manager's local resolver."""
        cached = self._grounded_targets.get(target_id)
        if cached is None:
            return None
        target, _observation = cached
        return target if observation_id is None or target.observation_timestamp == observation_id else None

    def observation_for_target(self, target_id: str):
        cached = self._grounded_targets.get(target_id)
        return cached[1] if cached is not None else None

    def close(self) -> None:
        """Release backends so RAM returns to the OS."""
        self._grounded_targets.clear()
        if self._capture is not None:
            try:
                self._capture.close()
            finally:
                self._capture = None
        if self._ocr is not None:
            try:
                self._ocr.release()
            finally:
                self._ocr = None


def summarize_dialog(observation: ScreenObservation) -> dict:
    """Shape an observation for future Phase 8B troubleshooting.

    Returns window title, visible text, likely buttons, and sources as plain
    data. No diagnosis, no autonomous action: structured evidence only.
    """
    buttons: list[str] = []
    for element in (*observation.uia_elements, *observation.dom_elements):
        kind = element.kind.casefold()
        if "button" in kind and element.name and element.name not in buttons:
            buttons.append(element.name)
    sources = sorted({item.split(":", 1)[0] for item in observation.provenance
                      if ":" in item})
    return {
        "window_title": observation.active_window,
        "visible_text": observation.fused_text,
        "buttons": buttons,
        "sources": sources,
        "ocr_status": observation.ocr_status.value,
        "trust": observation.trust,
        "notice": observation.notice,
    }
