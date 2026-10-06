"""Phase 8A.1 local Windows acceptance (run on the target laptop only).

Run: .venv\\Scripts\\python.exe scripts\\phase8a1_live_acceptance.py

This script performs REAL capture/UIA/OCR on the actual Windows machine.
It must NOT be claimed as passed from GitHub alone: without a live Windows
desktop it exits with LOCAL_ACCEPTANCE_REQUIRED.

Checks (each prints PASS/FAIL/SKIP):
  1. screenshot capture via python-mss (full primary monitor)
  2. bounded region capture
  3. active window title resolution
  4. UIA evidence on a known window (Notepad with known text)
  5. OCR engine probe + OCR on the Notepad screenshot (if RapidOCR installed)
  6. fused observation structure + provenance
  7. temp artifact cleanup (no leftover friday-vision-*.png in %TEMP%)
  8. latency of one observation (reported, not asserted)
  9. approximate RAM delta (reported, not asserted)
  10. no remote calls (observation.remote_calls == 0)

Preparation on the laptop before running:
  - Open Notepad, type the line: FRIDAY VISION ACCEPTANCE 8A1
  - Keep Notepad the foreground window, then run this script.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

if sys.platform != "win32":
    print("LOCAL_ACCEPTANCE_REQUIRED: this script needs a live Windows desktop.")
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.vision.capture import CaptureError, ScreenCapture
from friday.vision.evidence import active_window_title
from friday.vision.manager import VisionManager, summarize_dialog
from friday.vision.models import OcrStatus, ScreenRegion


checks: list[tuple[str, bool | None]] = []


def check(label: str, passed: bool | None) -> None:
    checks.append((label, passed))
    print(f"{label}: {'PASS' if passed is True else 'SKIP' if passed is None else 'FAIL'}")


KNOWN_LINE = "FRIDAY VISION ACCEPTANCE 8A1"

# 1. full-screen capture ------------------------------------------------------
manager = VisionManager()
try:
    image = manager._capture_backend().capture()
    check("1.screenshot-capture", image.width > 0 and image.height > 0 and len(image.png_bytes) > 100)
except CaptureError as error:
    print("   capture error:", error)
    check("1.screenshot-capture", False)

# 2. bounded region ------------------------------------------------------------
try:
    region = ScreenRegion(x=0, y=0, width=800, height=600, monitor=1, label="acceptance")
    small = manager._capture_backend().capture(region=region)
    check("2.bounded-region", small.width == 800 and small.height == 600)
except CaptureError as error:
    print("   region error:", error)
    check("2.bounded-region", False)

# 3. active window --------------------------------------------------------------
title = active_window_title()
check("3.active-window", bool(title))
print("   active window:", title)

# 4/5/6. observation with UIA + OCR ----------------------------------------------
ocr_probe = manager._ocr_backend().probe()
print("   OCR status:", ocr_probe.value)
t0 = time.perf_counter()
obs = manager.observe(window_target=None, ocr_mode="auto")
latency_ms = (time.perf_counter() - t0) * 1000.0
check("4.uia-evidence", len(obs.uia_elements) > 0)
if ocr_probe == OcrStatus.AVAILABLE:
    check("5.ocr-blocks", len(obs.ocr_blocks) > 0)
    found_known = KNOWN_LINE.casefold() in obs.fused_text.casefold()
    check("5.known-notepad-text", found_known)
    if not found_known:
        print("   hint: put Notepad in the foreground with the known line typed.")
else:
    check("5.ocr-blocks", None)
    check("5.known-notepad-text", None)
check("6.observation-shape",
      obs.remote_calls == 0 and obs.trust == "untrusted" and bool(obs.timestamp)
      and len(obs.provenance) > 0)
dialog = summarize_dialog(obs)
check("6.dialog-summary", isinstance(dialog.get("buttons"), list))

# 7. temp cleanup ----------------------------------------------------------------
leftovers = list(Path(tempfile.gettempdir()).glob("friday-vision-*.png"))
check("7.temp-cleanup", len(leftovers) == 0)
if leftovers:
    print("   leftovers:", leftovers[:5])

# 8/9. latency + RAM (reported only) ----------------------------------------------
print(f"   observation latency: {latency_ms:.1f} ms (reported, not asserted)")
try:
    import psutil  # optional; absence is fine

    proc = psutil.Process(os.getpid())
    rss_mb = proc.memory_info().rss / (1024 * 1024)
    print(f"   process RSS: {rss_mb:.1f} MB (reported, not asserted)")
except ImportError:
    print("   psutil not installed; RAM not measured (reported, not asserted)")

# 10. no remote calls ---------------------------------------------------------------
check("10.no-remote-calls", obs.remote_calls == 0)

manager.close()

failed = [label for label, passed in checks if passed is False]
skipped = [label for label, passed in checks if passed is None]
print(f"\n{len(checks) - len(failed) - len(skipped)}/{len(checks)} passed, "
      f"{len(skipped)} skipped, {len(failed)} failed.")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print("PHASE 8A.1 LIVE ACCEPTANCE: DONE (see skips above)")
