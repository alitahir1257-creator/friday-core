"""Deterministic Phase 8A.2 grounding checks; no desktop or model required."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.tools.models import RiskLevel
from friday.tools.registry import ToolRegistry
from friday.tools.vision import register_vision_tools
from friday.capabilities.computer.windows import ControlInfo
from friday.vision.capture import CapturedImage, ScreenCapture
from friday.vision.evidence import EvidenceError
from friday.vision.models import (CaptureMetadata, OcrBlock, ScreenObservation,
                                 ScreenRegion, UiElement, VisionSource)
from friday.vision.manager import VisionManager
from friday.vision.targets import (box_within, candidates_from_observation, local_to_screen,
                                   resolve_target, screen_to_local, screen_to_window,
                                   validate_target, window_to_screen)


now = datetime.now(timezone.utc)
stamp = now.isoformat()
base = ScreenObservation(timestamp=stamp, region=None, dimensions=(500, 400),
    active_window="Fixture", capture_bounds=(0, 0, 500, 400),
    window_bounds=(50, 50, 400, 300), window_handle=7, remote_calls=0)


def uia(name, box):
    return UiElement(name, "Button", "", VisionSource.UIA, box)


def check(label, condition):
    print(f"{label}: {'PASS' if condition else 'FAIL'}")
    if not condition:
        raise AssertionError(label)


a = resolve_target("Save", replace(base, uia_elements=(uia("Save", (100, 100, 80, 30)),)))
check("A.exact-uia", a.status == "SUCCESS" and a.target.source == "UIA")
b = resolve_target("settings", replace(base, uia_elements=(uia("Settings", (100, 100, 80, 30)),)))
check("B.normalized-uia", b.status == "SUCCESS")
c = resolve_target("Apply", replace(base, ocr_blocks=(OcrBlock("Apply", .9, (100, 100, 80, 30)),)))
check("C.ocr-fallback", c.status == "SUCCESS" and c.target.source == "OCR")
fused = replace(base, uia_elements=(uia("Apply", (95, 95, 90, 40)),),
                ocr_blocks=(OcrBlock("Apply", .9, (100, 100, 80, 30)),))
d = resolve_target("Apply", fused)
check("D.fused", d.status == "SUCCESS" and d.target.source == "FUSED"
      and d.target.metadata["provenance"] == ["UIA", "OCR"])
dupe = replace(fused, uia_elements=(uia("Apply", (95, 95, 90, 40)),
                                    uia("Apply", (96, 96, 89, 39))))
check("E.duplicate", len(candidates_from_observation(dupe)) == 1)
ambiguous = replace(base, uia_elements=(uia("Open", (70, 70, 50, 20)),
                                        uia("Open", (250, 70, 50, 20))))
f = resolve_target("Open", ambiguous)
check("F.ambiguous", f.status == "AMBIGUOUS_TARGET" and len(f.candidates) == 2)
check("G.not-found", resolve_target("Absent", base).status == "NOT_FOUND")
check("H.center", a.target.center_x == 140 and a.target.center_y == 115
      and box_within((a.target.center_x, a.target.center_y, 1, 1), a.target.bbox))
crop = replace(base, dimensions=(200, 100), capture_bounds=(100, 100, 200, 100),
               ocr_blocks=(OcrBlock("Apply", .9, (10, 20, 80, 30)),))
i = resolve_target("Apply", crop)
check("I.region-offset", i.target.bbox == (110, 120, 80, 30)
      and screen_to_local(i.target.bbox, (100, 100), (200, 100)) == (10, 20, 80, 30)
      and window_to_screen((50, 70, 80, 30), base.window_bounds) == (100, 120, 80, 30)
      and screen_to_window((100, 120, 80, 30), base.window_bounds) == (50, 70, 80, 30))
bad = replace(crop, ocr_blocks=(OcrBlock("Apply", .9, (190, 20, 80, 30)),))
check("J.out-of-bounds", resolve_target("Apply", bad).status == "NOT_FOUND"
      and local_to_screen((-1, 0, 10, 10), (0, 0), (100, 100)) is None)
outside = replace(base, uia_elements=(uia("Outside", (5, 5, 30, 20)),))
check("K.active-boundary", resolve_target("Outside", outside).status == "NOT_FOUND"
      and resolve_target("Outside", outside, active_window_only=False).status == "SUCCESS")
old = replace(a.target, observation_timestamp=(now - timedelta(seconds=30)).isoformat())
check("L.stale", not validate_target(old, current_window=(7, base.window_bounds), now=now)
      and not validate_target(a.target, current_window=(8, base.window_bounds), now=now)
      and validate_target(a.target, current_window=(7, base.window_bounds), now=now))


class FakeManager:
    def find_target(self, query, *, active_window_only=True, max_candidates=5,
                    window_target=None):
        self.window_target = window_target
        observation = ambiguous if query == "Open" else replace(base, uia_elements=(uia("Save", (100, 100, 80, 30)),))
        return resolve_target(query, observation,
                              active_window_only=active_window_only, max_candidates=max_candidates)


registry = ToolRegistry()
fake_manager = FakeManager()
register_vision_tools(registry, lambda: fake_manager)
tool = registry.get("vision.find_target")
result = registry.execute("vision.find_target", {"query": "Save"}, caller="planner", privacy="private")
check("M.tool-registry", tool.risk_level == RiskLevel.READ_ONLY and result.success
      and result.structured_data["status"] == "SUCCESS"
      and result.structured_data["target"]["center"] == [140, 115])
ambiguous_tool = registry.execute("vision.find_target", {"query": "Open"}, caller="planner")
check("M.ambiguous-tool", ambiguous_tool.success and ambiguous_tool.structured_data["status"] == "AMBIGUOUS_TARGET"
      and len(ambiguous_tool.structured_data["candidates"]) == 2
      and set(ambiguous_tool.structured_data["candidates"][0]) == {"label", "source", "bbox", "confidence"})
explicit_tool = registry.execute("vision.find_target", {"query": "Save", "window": "Fixture"},
                                 caller="planner")
check("M.explicit-window-tool", explicit_tool.success and fake_manager.window_target == "Fixture"
      and explicit_tool.structured_data["status"] == "SUCCESS")
check("N.privacy", not tool.transmits_data and base.remote_calls == 0)
check("O.persistence", not hasattr(FakeManager(), "memory")
      and not any("screenshot" in key for key in result.structured_data))

from friday.capabilities.computer.windows import ControlInfo, WindowController

class FakeRect:
    left, top = 100, 100
    def width(self): return 80
    def height(self): return 30

class FakeControl:
    element_info = SimpleNamespace(name="Save", control_type="Button",
                                   automation_id="save", rectangle=FakeRect())
    clicked = False
    def is_visible(self): return True
    def is_enabled(self): return True
    def click_input(self): self.clicked = True

control = FakeControl()
window = SimpleNamespace(descendants=lambda: [control])
controller = WindowController.__new__(WindowController)
controller._window = lambda _target: window
info = controller.inspect_controls("Fixture")
clicked = controller.click_control("Fixture", name="Save")
check("P.computer-regression", info == [ControlInfo("Save", "Button", "save", (100, 100, 80, 30), True, True)]
      and control.clicked and clicked == "Clicked Save.")

class CountingVision(VisionManager):
    def __init__(self):
        super().__init__()
        self.calls = []
    def observe(self, *, ocr_mode="auto"):
        self.calls.append(ocr_mode)
        return replace(base, uia_elements=(uia("Save", (100, 100, 80, 30)),)) if ocr_mode == "auto" else replace(
            base, ocr_blocks=(OcrBlock("Apply", .9, (100, 100, 80, 30)),))

counting = CountingVision()
check("Q.uia-first", counting.find_target("Save").status == "SUCCESS" and counting.calls == ["auto"])
check("Q.ocr-on-miss", counting.find_target("Apply").status == "SUCCESS"
      and counting.calls == ["auto", "auto", "always"])
check("Q.reuse-observation", counting.find_target("Apply", observation=replace(base,
      ocr_blocks=(OcrBlock("Apply", .9, (100, 100, 80, 30)),))).status == "SUCCESS"
      and counting.calls == ["auto", "auto", "always"])
check("Q.uia-without-screenshot", resolve_target("Save", replace(base, dimensions=None,
      capture_bounds=None, uia_elements=(uia("Save", (100, 100, 80, 30)),))).status == "SUCCESS")

# Explicit HWND observation does not consult foreground state or resolve another window.
class ExplicitCapture:
    def __init__(self):
        self.calls = []

    def capture_window(self, hwnd, *, persist=False):
        self.calls.append((hwnd, persist))
        region = ScreenRegion(0, 0, 200, 100, monitor=1, label=f"window:{hwnd}")
        metadata = CaptureMetadata(200, 100, region, 1, stamp, "synthetic",
                                  persisted=persist, screen_origin=(10, 20))
        return CapturedImage(b"synthetic", 200, 100, metadata)

    def close(self):
        return


class ExplicitUIA:
    handles = []

    def inspect_controls_for_handle(self, hwnd, limit=100):
        self.handles.append(hwnd)
        return [ControlInfo("File", "MenuItem", "file", (15, 25, 30, 20), True, True)]


explicit_capture, explicit_uia = ExplicitCapture(), ExplicitUIA()
explicit_manager = VisionManager(capture=explicit_capture)
with patch("friday.vision.manager.active_window_identity", side_effect=AssertionError("foreground consulted")), \
     patch("friday.vision.manager.explicit_window_info", side_effect=[
         ("Notepad", (10, 20, 200, 100)), ("Notepad", (10, 20, 200, 100)),
     ]):
    explicit_observation = explicit_manager.observe_window(123, ocr_mode="never",
                                                           uia_controller=explicit_uia)
explicit_resolution = explicit_manager.find_target("File", observation=explicit_observation)
with patch("friday.vision.manager.active_window_identity", side_effect=AssertionError("foreground consulted")), \
     patch("friday.vision.manager.explicit_window_info", side_effect=[
         ("Notepad", (10, 20, 200, 100)), ("Notepad", (10, 20, 200, 100))
     ]), patch("friday.vision.manager.collect_uia", return_value=(
         UiElement("File", "MenuItem", "file", VisionSource.UIA, (15, 25, 30, 20)),
     )):
    explicit_direct_resolution = explicit_manager.find_target("File", window_handle=123)
explicit_ocr_resolution = explicit_manager.find_target(
    "FRIDAY VISION TARGET TEST",
    observation=replace(explicit_observation, uia_elements=(), ocr_blocks=(
        OcrBlock("FRIDAY VISION TARGET TEST", .97, (12, 14, 70, 12)),
    )),
)
check("R.explicit-hwnd-observation", explicit_observation.status == "SUCCESS"
      and explicit_observation.window_handle == 123
      and explicit_observation.window_bounds == (10, 20, 200, 100)
      and explicit_observation.capture_bounds == (10, 20, 200, 100)
      and explicit_capture.calls == [(123, False), (123, False)]
      and explicit_uia.handles == [123]
      and explicit_resolution.status == "SUCCESS"
      and explicit_resolution.target.bbox == (15, 25, 30, 20)
      and box_within(explicit_resolution.target.bbox, explicit_observation.window_bounds)
      and box_within((explicit_resolution.target.center_x, explicit_resolution.target.center_y, 1, 1),
                     explicit_resolution.target.bbox)
      and explicit_direct_resolution.status == "SUCCESS")
check("R.explicit-window-ocr-coordinates", explicit_ocr_resolution.status == "SUCCESS"
      and explicit_ocr_resolution.target.bbox == (22, 34, 70, 12)
      and explicit_ocr_resolution.target.normalized_label == "friday vision target test"
      and box_within(explicit_ocr_resolution.target.bbox, explicit_observation.window_bounds)
      and box_within((explicit_ocr_resolution.target.center_x, explicit_ocr_resolution.target.center_y, 1, 1),
                     explicit_ocr_resolution.target.bbox))
explicit_manager.close()

with patch("friday.vision.manager.active_window_identity", return_value=None):
    no_target_manager = VisionManager(capture=ExplicitCapture())
    required = no_target_manager.observe(ocr_mode="always")
    required_resolution = no_target_manager.find_target("File")
    with patch("friday.vision.manager.resolve_visible_window_title", return_value=None):
        ambiguous_window = no_target_manager.observe(window_target="Duplicate title")
    check("S.window-target-required", required.status == "WINDOW_TARGET_REQUIRED"
          and required_resolution.status == "WINDOW_TARGET_REQUIRED"
          and ambiguous_window.status == "WINDOW_TARGET_REQUIRED"
          and no_target_manager._capture.calls == [])
    no_target_manager.close()

title_capture = ExplicitCapture()
title_manager = VisionManager(capture=title_capture)
with patch("friday.vision.manager.active_window_identity", return_value=None), \
     patch("friday.vision.manager.resolve_visible_window_title", return_value=789), \
     patch("friday.vision.manager.explicit_window_info", side_effect=[
         ("Notepad", (10, 20, 200, 100)), ("Notepad", (10, 20, 200, 100))
     ]), patch("friday.vision.manager.collect_uia", return_value=(
         UiElement("File", "MenuItem", "file", VisionSource.UIA, (15, 25, 30, 20)),
     )):
    title_resolution = title_manager.find_target("File", window_target="Notepad")
check("S.exact-window-fallback", title_resolution.status == "SUCCESS"
      and title_resolution.target.window_handle == 789
      and title_capture.calls == [(789, False)])
title_manager.close()

stale_capture = ExplicitCapture()
stale_manager = VisionManager(capture=stale_capture)
with patch("friday.vision.manager.explicit_window_info",
           side_effect=EvidenceError("stale HWND")):
    try:
        stale_manager.observe_window(123)
        stale_rejected = False
    except EvidenceError:
        stale_rejected = True
check("T.stale-explicit-hwnd", stale_rejected and stale_capture.calls == [])
stale_manager.close()

window_capture = ScreenCapture()
window_capture._sct = SimpleNamespace(monitors=[
    {"left": -1920, "top": 0, "width": 3840, "height": 1080},
    {"left": -1920, "top": 0, "width": 1920, "height": 1080},
])
with patch("friday.vision.capture.explicit_window_info",
           return_value=("Notepad", (-1800, 100, 400, 200))), \
     patch.object(ScreenCapture, "capture", return_value="CAPTURED") as bounded_capture:
    captured = window_capture.capture_window(456)
check("U.explicit-window-capture-bounds", captured == "CAPTURED"
      and bounded_capture.call_args.kwargs == {
          "region": ScreenRegion(120, 100, 400, 200, monitor=1, label="window:456"),
          "monitor": 1, "persist": False,
      })
window_capture._sct = None
print("PHASE 8A.2 SYNTHETIC: ALL PASS")
