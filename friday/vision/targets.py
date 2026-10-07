"""Local, deterministic screen grounding. All target boxes use screen pixels (x, y, w, h)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha1
import re

from friday.vision.evidence import active_window_identity
from friday.vision.models import ScreenObservation


Box = tuple[int, int, int, int]
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize_label(text: str) -> str:
    return _SPACE.sub(" ", _PUNCT.sub(" ", text.casefold())).strip()


def _compact_label(text: str) -> str:
    return normalize_label(text).replace(" ", "")


def box_within(box: Box, bounds: Box) -> bool:
    if any(type(v) is not int for v in (*box, *bounds)):
        return False
    x, y, w, h = box
    bx, by, bw, bh = bounds
    return w > 0 and h > 0 and bw > 0 and bh > 0 and x >= bx and y >= by and x + w <= bx + bw and y + h <= by + bh


def local_to_screen(box: Box, origin: tuple[int, int], dimensions: tuple[int, int]) -> Box | None:
    if not box_within(box, (0, 0, *dimensions)) or any(type(v) is not int for v in origin):
        return None
    return (box[0] + origin[0], box[1] + origin[1], box[2], box[3])


def screen_to_local(box: Box, origin: tuple[int, int], dimensions: tuple[int, int]) -> Box | None:
    local = (box[0] - origin[0], box[1] - origin[1], box[2], box[3])
    return local if box_within(local, (0, 0, *dimensions)) else None


def window_to_screen(box: Box, window_bounds: Box) -> Box | None:
    return local_to_screen(box, window_bounds[:2], window_bounds[2:])


def screen_to_window(box: Box, window_bounds: Box) -> Box | None:
    return screen_to_local(box, window_bounds[:2], window_bounds[2:])


def _same_place(a: Box, b: Box) -> bool:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    overlap = max(0, right - left) * max(0, bottom - top)
    return overlap > 0 and overlap / min(a[2] * a[3], b[2] * b[3]) >= 0.5


@dataclass(frozen=True)
class GroundedTarget:
    id: str
    label: str
    normalized_label: str
    bbox: Box
    center_x: int
    center_y: int
    source: str
    confidence: float
    window_title: str | None
    visible: bool
    enabled: bool | None
    control_type: str | None
    observation_timestamp: str
    window_handle: int | None
    window_bounds: Box | None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TargetResolution:
    status: str
    target: GroundedTarget | None = None
    candidates: tuple[GroundedTarget, ...] = ()


def _target(label: str, box: Box, source: str, confidence: float,
            observation: ScreenObservation, *, enabled=None, control_type=None,
            metadata=None) -> GroundedTarget:
    key = f"{observation.timestamp}|{source}|{box}|{label}"
    return GroundedTarget(
        id=sha1(key.encode("utf-8")).hexdigest()[:16], label=label,
        normalized_label=normalize_label(label), bbox=box,
        center_x=box[0] + box[2] // 2, center_y=box[1] + box[3] // 2,
        source=source, confidence=confidence, window_title=observation.active_window,
        visible=True, enabled=enabled, control_type=control_type,
        observation_timestamp=observation.timestamp, window_handle=observation.window_handle,
        window_bounds=observation.window_bounds, metadata=metadata or {},
    )


def candidates_from_observation(observation: ScreenObservation,
                                *, active_window_only: bool = True) -> tuple[GroundedTarget, ...]:
    if active_window_only and (observation.window_bounds is None or observation.window_handle is None):
        return ()
    bounds = observation.window_bounds if active_window_only else observation.capture_bounds
    if bounds is None:
        return ()
    results: list[GroundedTarget] = []
    for element in observation.uia_elements:
        if not element.name.strip() or not element.visible or element.bbox is None:
            continue
        if not box_within(element.bbox, bounds) or (observation.capture_bounds is not None and
            not box_within(element.bbox, observation.capture_bounds)):
            continue
        candidate = _target(element.name.strip(), element.bbox, "UIA", 1.0, observation,
                            enabled=element.enabled, control_type=element.kind or None,
                            metadata={"automation_id": element.identifier, "provenance": ["UIA"]})
        if not any(other.normalized_label == candidate.normalized_label and
                   _same_place(other.bbox, candidate.bbox) for other in results):
            results.append(candidate)
    if observation.capture_bounds is None or observation.dimensions is None:
        return tuple(results)
    origin = observation.capture_bounds[:2]
    for block in observation.ocr_blocks:
        box = local_to_screen(block.bbox, origin, observation.dimensions)
        if box is None or not box_within(box, bounds) or not block.text.strip():
            continue
        label = block.text.strip()
        normalized = normalize_label(label)
        compact = _compact_label(label)
        match_index = next((i for i, item in enumerate(results)
                            if (item.normalized_label == normalized
                                or (item.source in ("UIA", "FUSED")
                                    and _compact_label(item.label) == compact))
                            and _same_place(item.bbox, box)), None)
        if match_index is not None:
            item = results[match_index]
            if item.source == "UIA":
                results[match_index] = replace(item, source="FUSED",
                    metadata={**item.metadata, "provenance": ["UIA", "OCR"],
                              "ocr_label": label, "ocr_bbox": box, "ocr_confidence": block.confidence})
            continue
        editable_matches = [
            element for element in observation.uia_elements
            if element.visible and element.enabled is not False and element.bbox is not None
            and element.kind.casefold() in {"edit", "combobox", "document"}
            and box_within(element.bbox, bounds) and _same_place(element.bbox, box)
        ]
        if len(editable_matches) == 1:
            element = editable_matches[0]
            results.append(_target(
                label, box, "FUSED", block.confidence, observation,
                enabled=element.enabled, control_type=element.kind,
                metadata={"provenance": ["OCR", "UIA"], "ocr_label": label,
                          "ocr_bbox": box, "ocr_confidence": block.confidence,
                          "automation_id": element.identifier, "control_bbox": element.bbox},
            ))
            continue
        results.append(_target(label, box, "OCR", block.confidence, observation,
                               metadata={"provenance": ["OCR"]}))
    return tuple(results)


def resolve_target(query: str, observation: ScreenObservation, *,
                   active_window_only: bool = True, max_candidates: int = 5) -> TargetResolution:
    if observation.status != "SUCCESS":
        return TargetResolution(observation.status)
    if not query.strip() or max_candidates < 1:
        return TargetResolution("NOT_FOUND")
    if ((not active_window_only and observation.capture_bounds is None) or
        (active_window_only and (observation.window_bounds is None or observation.window_handle is None))):
        return TargetResolution("OBSERVATION_UNAVAILABLE")
    normalized = normalize_label(query)
    if not normalized:
        return TargetResolution("NOT_FOUND")
    ranked: list[tuple[int, GroundedTarget]] = []
    for item in candidates_from_observation(observation, active_window_only=active_window_only):
        uia = item.source in ("UIA", "FUSED")
        exact = query.strip() == item.label
        if exact or normalized == item.normalized_label:
            ranked.append(((0 if uia else 2) + (0 if exact else 1), item))
        elif item.source == "OCR" and _compact_label(query) == _compact_label(item.label):
            ranked.append((4, item))
    if not ranked:
        return TargetResolution("NOT_FOUND")
    best = min(rank for rank, _ in ranked)
    matches = tuple(item for rank, item in ranked if rank == best)
    if len(matches) > 1:
        return TargetResolution("AMBIGUOUS_TARGET", candidates=matches[:max_candidates])
    return TargetResolution("SUCCESS", target=matches[0])


def validate_target(target: GroundedTarget, *, max_age_seconds: float = 5.0,
                    current_window: tuple[int, Box] | None = None,
                    now: datetime | None = None) -> bool:
    """Action boundary: recheck age, foreground HWND, rectangle and center."""
    try:
        observed = datetime.fromisoformat(target.observation_timestamp)
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    except (ValueError, TypeError):
        return False
    window = current_window if current_window is not None else active_window_identity()
    return (0 <= age <= max_age_seconds and window is not None
            and target.window_handle == window[0] and target.window_bounds == window[1]
            and box_within(target.bbox, window[1])
            and box_within((target.center_x, target.center_y, 1, 1), target.bbox))
