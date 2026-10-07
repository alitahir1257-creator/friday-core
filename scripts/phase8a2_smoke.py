"""Deterministic Phase 8A.2 grounding checks; no desktop or model required."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.tools.models import RiskLevel
from friday.tools.registry import ToolRegistry
from friday.tools.vision import register_vision_tools
from friday.vision.models import OcrBlock, ScreenObservation, UiElement, VisionSource
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
    def find_target(self, query, *, active_window_only=True, max_candidates=5):
        observation = ambiguous if query == "Open" else replace(base, uia_elements=(uia("Save", (100, 100, 80, 30)),))
        return resolve_target(query, observation,
                              active_window_only=active_window_only, max_candidates=max_candidates)


registry = ToolRegistry()
register_vision_tools(registry, FakeManager)
tool = registry.get("vision.find_target")
result = registry.execute("vision.find_target", {"query": "Save"}, caller="planner", privacy="private")
check("M.tool-registry", tool.risk_level == RiskLevel.READ_ONLY and result.success
      and result.structured_data["status"] == "SUCCESS"
      and result.structured_data["target"]["center"] == [140, 115])
ambiguous_tool = registry.execute("vision.find_target", {"query": "Open"}, caller="planner")
check("M.ambiguous-tool", ambiguous_tool.success and ambiguous_tool.structured_data["status"] == "AMBIGUOUS_TARGET"
      and len(ambiguous_tool.structured_data["candidates"]) == 2
      and set(ambiguous_tool.structured_data["candidates"][0]) == {"label", "source", "bbox", "confidence"})
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
print("PHASE 8A.2 SYNTHETIC: ALL PASS")
