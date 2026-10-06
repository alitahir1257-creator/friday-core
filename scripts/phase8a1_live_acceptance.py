"""Phase 8A.1 local Windows acceptance (run on the target laptop only).

Run: .venv\\Scripts\\python.exe scripts\\phase8a1_live_acceptance.py

This script performs REAL capture/UIA/OCR on the actual Windows machine.
It must NOT be claimed as passed from GitHub alone: without a live Windows
desktop it exits with LOCAL_ACCEPTANCE_REQUIRED.

Two separate observations mirror the production policy (UIA first, OCR
fallback/on-demand) — the script never fights it:

  OBSERVATION 1 (ocr_mode="auto"):
    screenshot, active-window observation, UIA evidence, and proof that
    auto mode skips unnecessary OCR when UIA already covers the text.

  OBSERVATION 2 (ocr_mode="always"):
    explicit OCR path: availability, visible-text test, OCR blocks,
    UIA+OCR fusion structure, and OCR latency.

Checks (each prints PASS/FAIL/SKIP):
  1. screenshot capture via python-mss (full primary monitor)
  2. bounded region capture
  3. active window title resolution
  4. auto-mode observation: UIA evidence + auto-skips-unneeded-OCR proof
  5. always-mode observation: OCR blocks, known text, fusion, unavailable-normalized
  6. dialog summary shape (future 8B foundation)
  7. temp artifact cleanup (no leftover friday-vision-*.png in %TEMP%)
  8. latency of both observations (reported, not asserted)
  9. approximate RAM delta (reported, not asserted)
  10. no remote calls on either observation

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

# 4. OBSERVATION 1 — auto mode: UIA first, OCR skipped when UIA covers ---------
ocr_probe = manager._ocr_backend().probe()
print("   OCR status:", ocr_probe.value)
t0 = time.perf_counter()
obs_auto = manager.observe(window_target=None, ocr_mode="auto")
auto_latency_ms = (time.perf_counter() - t0) * 1000.0
check("4.uia-evidence", len(obs_auto.uia_elements) > 0)
if len(obs_auto.uia_elements) > 0:
    # Production policy proof: auto mode must NOT spend OCR when UIA covers text.
    check("4.auto-skips-unneeded-ocr", len(obs_auto.ocr_blocks) == 0)
else:
    check("4.auto-skips-unneeded-ocr", None)  # no UIA evidence; policy path not exercised
check("4.observation-shape",
      obs_auto.remote_calls == 0 and obs_auto.trust == "untrusted" and bool(obs_auto.timestamp)
      and len(obs_auto.provenance) > 0)

# 5. OBSERVATION 2 — always mode: explicit OCR path -----------------------------
t1 = time.perf_counter()
obs_ocr = manager.observe(window_target=None, ocr_mode="always")
ocr_latency_ms = (time.perf_counter() - t1) * 1000.0
if ocr_probe == OcrStatus.AVAILABLE:
    check("5.ocr-blocks", len(obs_ocr.ocr_blocks) > 0)
    found_known = KNOWN_LINE.casefold() in obs_ocr.fused_text.casefold()
    check("5.known-notepad-text", found_known)
    if not found_known:
        print("   hint: put Notepad in the foreground with the known line typed.")
    # Fusion structure: exactly one fused line per provenance item (no dupes).
    lines = [line for line in obs_ocr.fused_text.splitlines() if line.strip()]
    check("5.fusion-no-dupes", len(lines) == len(obs_ocr.provenance) and len(lines) > 0)
else:
    check("5.ocr-blocks", None)
    check("5.known-notepad-text", None)
    check("5.fusion-no-dupes", None)
    # Normalized result when OCR is unavailable: deterministic status, no crash.
    check("5.ocr-unavailable-normalized",
          obs_ocr.ocr_status in (OcrStatus.NOT_INSTALLED, OcrStatus.LOAD_FAILED, OcrStatus.OCR_REQUIRED)
          and obs_ocr.ocr_blocks == ())

# 6. dialog summary shape (future 8B foundation) ----------------------------------
dialog = summarize_dialog(obs_ocr)
check("6.dialog-summary", isinstance(dialog.get("buttons"), list))

# 7. temp cleanup ----------------------------------------------------------------
leftovers = list(Path(tempfile.gettempdir()).glob("friday-vision-*.png"))
check("7.temp-cleanup", len(leftovers) == 0)
if leftovers:
    print("   leftovers:", leftovers[:5])

# 8/9. latency + RAM (reported only) ----------------------------------------------
print(f"   auto observation latency: {auto_latency_ms:.1f} ms (reported, not asserted)")
print(f"   always-OCR observation latency: {ocr_latency_ms:.1f} ms (reported, not asserted)")
try:
    import psutil  # optional; absence is fine

    proc = psutil.Process(os.getpid())
    rss_mb = proc.memory_info().rss / (1024 * 1024)
    print(f"   process RSS: {rss_mb:.1f} MB (reported, not asserted)")
except ImportError:
    print("   psutil not installed; RAM not measured (reported, not asserted)")

# 10. no remote calls ---------------------------------------------------------------
check("10.no-remote-calls", obs_auto.remote_calls == 0 and obs_ocr.remote_calls == 0)

manager.close()

failed = [label for label, passed in checks if passed is False]
skipped = [label for label, passed in checks if passed is None]
print(f"\n{len(checks) - len(failed) - len(skipped)}/{len(checks)} passed, "
      f"{len(skipped)} skipped, {len(failed)} failed.")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print("PHASE 8A.1 LIVE ACCEPTANCE: DONE (see skips above)")
