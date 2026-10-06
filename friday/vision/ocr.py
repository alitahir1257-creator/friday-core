"""Optional lazy RapidOCR adapter (Phase 8A.1).

Design rules:
- The ``rapidocr_onnxruntime`` package is imported lazily. Without it the
  adapter reports NOT_INSTALLED instead of crashing.
- The engine is constructed lazily on first use; FRIDAY never loads an OCR
  model at startup.
- Local-only. The adapter makes zero network calls itself. Note honestly:
  RapidOCR fetches its ONNX model files on first inference, so the very
  first real OCR call may need network access unless the model cache was
  pre-seeded. FRIDAY only ever triggers that on an explicit observation
  request, never silently in the background.
- Bounded input: PNG bytes with a hard size cap; malformed input yields a
  deterministic OCR_REQUIRED status, never an unhandled traceback.
- Dependency footprint (torch/Pillow/numpy/onnxruntime versions and Windows
  suitability on the 8 GB target) still needs validation on the real machine:
  LOCAL_DEPENDENCY_VALIDATION_REQUIRED. OCR stays optional and modular so
  FRIDAY works fully without it (UIA/DOM first).
"""

from __future__ import annotations

from friday.vision.models import OcrBlock, OcrStatus, VisionSource


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


class OcrError(RuntimeError):
    """Deterministic OCR failure with a stable status attached."""

    def __init__(self, status: OcrStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _clamp_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _bbox_from_points(box: object) -> tuple[int, int, int, int]:
    """Accept [x1,y1,x2,y2] or 4-point polygons; return (x, y, w, h)."""
    if not isinstance(box, (list, tuple)):
        raise OcrError(OcrStatus.LOAD_FAILED, "Unexpected OCR box shape.")
    flat: list[float] = []
    for item in box:
        if isinstance(item, (list, tuple)):
            flat.extend(float(v) for v in item)
        else:
            flat.append(float(item))
    if len(flat) == 4:
        x1, y1, x2, y2 = flat
    elif len(flat) == 8:
        xs, ys = flat[0::2], flat[1::2]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    else:
        raise OcrError(OcrStatus.LOAD_FAILED, "Unexpected OCR box shape.")
    x, y = int(x1), int(y1)
    return (x, y, max(0, int(x2) - x), max(0, int(y2) - y))


def _describe_shape(value: object) -> str:
    """Short safe structural summary for diagnostics (no data dumped)."""
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} len {len(value)}"
    return type(value).__name__


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_box(value: object) -> bool:
    """A box is [x1,y1,x2,y2] or a 4-point polygon of numbers."""
    if not isinstance(value, (list, tuple)) or not value:
        return False
    try:
        flat: list[float] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                flat.extend(float(v) for v in item)
            else:
                flat.append(float(item))
    except (TypeError, ValueError):
        return False
    return len(flat) in (4, 8) and all(_is_number(v) for v in flat)


def _is_current_shape(result: object) -> bool:
    """rapidocr-onnxruntime 1.x: (rows, timing); rows = [[box, text, score], ...]."""
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return False
    rows = result[0]
    if not isinstance(rows, (list, tuple)):
        return False
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            return False
        box, text, score = row
        if not _is_box(box) or not isinstance(text, str) or not _is_number(score):
            return False
    return True


def _is_legacy_shape(result: object) -> bool:
    """Legacy: (boxes, texts, scores)."""
    if not isinstance(result, (list, tuple)) or len(result) != 3:
        return False
    boxes, texts, scores = result
    return (
        isinstance(boxes, (list, tuple))
        and isinstance(texts, (list, tuple))
        and isinstance(scores, (list, tuple))
        and all(_is_box(box) for box in boxes)
        and all(isinstance(text, str) for text in texts)
        and all(_is_number(score) for score in scores)
    )


def _normalize_current(rows) -> tuple[OcrBlock, ...]:
    blocks: list[OcrBlock] = []
    for box, text, score in rows:
        cleaned = str(text).strip()
        if not cleaned:
            continue
        blocks.append(OcrBlock(text=cleaned, confidence=_clamp_confidence(score),
                               bbox=_bbox_from_points(box), source=VisionSource.OCR))
    return tuple(blocks)


def _normalize_legacy(boxes, texts, scores) -> tuple[OcrBlock, ...]:
    blocks: list[OcrBlock] = []
    for index, text in enumerate(texts):
        cleaned = str(text).strip()
        if not cleaned:
            continue
        try:
            bbox = _bbox_from_points(boxes[index])
        except (IndexError, OcrError, ValueError, TypeError) as error:
            raise OcrError(OcrStatus.LOAD_FAILED,
                           f"Unexpected OCR box shape ({_describe_shape(boxes[index]) if index < len(boxes) else 'missing'}).") from error
        score = scores[index] if index < len(scores) else 0.0
        blocks.append(OcrBlock(text=cleaned, confidence=_clamp_confidence(score),
                               bbox=bbox, source=VisionSource.OCR))
    return tuple(blocks)


def normalize_ocr_output(result: object) -> tuple[OcrBlock, ...]:
    """Turn a RapidOCR result into OcrBlocks.

    Supported shapes:
      * current (rapidocr-onnxruntime 1.x): (rows, timing),
        where rows = [[box, text, score], ...] and timing is ignored;
      * legacy: (boxes, texts, scores).

    Pure and deterministic: no model, no I/O. Anything else raises OcrError
    with a short structural reason so callers get a stable status instead
    of a traceback.
    """
    if _is_current_shape(result):
        return _normalize_current(result[0])  # type: ignore[index]
    if _is_legacy_shape(result):
        boxes, texts, scores = result  # type: ignore[misc]
        return _normalize_legacy(boxes, texts, scores)
    raise OcrError(OcrStatus.LOAD_FAILED,
                   f"Unexpected OCR result shape ({_describe_shape(result)}).")


class OcrEngine:
    """Lazy local OCR. One job at a time; release() drops the engine."""

    def __init__(self) -> None:
        self._engine = None
        self._status = OcrStatus.NOT_INSTALLED
        self._detail = "Engine not probed yet."

    @property
    def status(self) -> OcrStatus:
        return self._status

    @property
    def detail(self) -> str:
        return self._detail

    def _import_engine_class(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as error:
            raise OcrError(OcrStatus.NOT_INSTALLED,
                           "rapidocr_onnxruntime is not installed.") from error
        return RapidOCR

    def probe(self) -> OcrStatus:
        """Check availability without running inference or loading models."""
        if self._engine is not None:
            self._status = OcrStatus.AVAILABLE
            return self._status
        try:
            engine_class = self._import_engine_class()
        except OcrError as error:
            self._status, self._detail = error.status, error.detail
            return self._status
        try:
            self._engine = engine_class()
        except Exception as error:
            self._status = OcrStatus.LOAD_FAILED
            self._detail = f"OCR engine failed to initialize: {type(error).__name__}."
            return self._status
        self._status = OcrStatus.AVAILABLE
        self._detail = "OCR engine ready (local-only)."
        return self._status

    def _decode_png(self, png_bytes: bytes):
        try:
            from PIL import Image
            import numpy as np
        except ImportError as error:
            raise OcrError(OcrStatus.LOAD_FAILED,
                           "Pillow and numpy are required to decode PNG input for OCR.") from error
        import io
        try:
            image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        except Exception as error:
            raise OcrError(OcrStatus.OCR_REQUIRED,
                           "Input is not a decodable PNG image.") from error
        return np.asarray(image)

    def read(self, png_bytes: bytes) -> tuple[OcrStatus, tuple[OcrBlock, ...]]:
        """Run OCR on PNG bytes. Returns (status, blocks); never raises for
        engine problems, only OcrError for unusable input is converted to a
        deterministic status."""
        if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
            return (OcrStatus.OCR_REQUIRED, ())
        if len(png_bytes) > _MAX_IMAGE_BYTES:
            return (OcrStatus.OCR_REQUIRED, ())
        if not bytes(png_bytes).startswith(_PNG_MAGIC):
            return (OcrStatus.OCR_REQUIRED, ())
        status = self.probe()
        if status != OcrStatus.AVAILABLE or self._engine is None:
            return (status, ())
        try:
            image = self._decode_png(bytes(png_bytes))
        except OcrError as error:
            return (error.status, ())
        try:
            result = self._engine(image)
        except Exception as error:
            self._status = OcrStatus.LOAD_FAILED
            self._detail = f"OCR inference failed: {type(error).__name__}."
            return (OcrStatus.LOAD_FAILED, ())
        if not result:
            return (OcrStatus.SUCCESS, ())
        try:
            blocks = normalize_ocr_output(result)
        except OcrError as error:
            return (error.status, ())
        return (OcrStatus.SUCCESS, blocks)

    def release(self) -> None:
        """Drop the engine so RAM returns to the OS. Near-zero idle cost."""
        self._engine = None
        self._status = OcrStatus.NOT_INSTALLED
        self._detail = "Engine released."
