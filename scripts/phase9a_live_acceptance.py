"""Bounded physical microphone/speaker acceptance for Phase 9A."""

from __future__ import annotations

from collections import deque
import argparse
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from friday.core import FridayCore
from friday.profile import AssistantProfile
from friday.voice.audio import AudioInput, AudioOutput, discover_devices
from friday.voice.session import normalize_transcript
from friday.voice.stt import SherpaOnnxSTT
from friday.voice.tts import PiperLocalTTS
from friday.voice.vad import EnergyVAD
from friday.voice.models import VoiceState


def capture_one_utterance(timeout_seconds: float = 12.0):
    frames: queue.Queue[tuple[np.ndarray, int]] = queue.Queue(maxsize=64)
    source = AudioInput()
    vad = None
    try:
        def on_frame(samples, sample_rate):
            item = (np.asarray(samples, dtype=np.float32).copy(), int(sample_rate))
            try:
                frames.put_nowait(item)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(item)
                except queue.Full:
                    pass

        source.start(on_frame)
        print("LIVE_MIC_PROMPT: Say 'Friday voice local acceptance' now (12 seconds).", flush=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                samples, sample_rate = frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if vad is None:
                vad = EnergyVAD(sample_rate=sample_rate, frame_ms=100, silence_end_seconds=0.8,
                                max_utterance_seconds=8.0)
            result = vad.feed(samples)
            if result.utterance is not None:
                return result.utterance, sample_rate, source.device_name
        return None, source.sample_rate, source.device_name
    finally:
        source.stop()


def play_local_acceptance() -> tuple[bool, float | None, str | None]:
    tts = PiperLocalTTS(output=AudioOutput())
    try:
        tts.speak("FRIDAY voice is working")
        deadline = time.monotonic() + 15.0
        while tts.is_speaking and time.monotonic() < deadline:
            time.sleep(0.02)
        if tts.is_speaking:
            tts.stop()
            return False, tts.last_first_audio_latency_ms, "PLAYBACK_TIMEOUT"
        return tts.last_error is None, tts.last_first_audio_latency_ms, tts.output.device_name or tts.last_error
    finally:
        tts.close()


def wait_for_turn(manager, turn: int, timeout_seconds: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session = manager.session
        if session is None or manager.state in {VoiceState.ERROR, VoiceState.IDLE}:
            return False
        if session.turn_count >= turn and manager.state == VoiceState.LISTENING:
            return True
        time.sleep(0.05)
    return False


def run_continuous_acceptance() -> tuple[str, list[str]]:
    catalog = discover_devices()
    if catalog.default_input is None or catalog.default_output is None:
        return "NOT_AVAILABLE", []
    core = FridayCore(AssistantProfile())
    manager = core.voice_manager
    events = deque(maxlen=32)
    manager.add_event_listener(events.append)
    try:
        state = manager.start_conversation(privacy="private")
        if state != VoiceState.LISTENING.value:
            return "FAIL_START", [event.name for event in events]

        print("LIVE_CONTINUOUS_PROMPT: Say 'Friday, say ready'. After FRIDAY finishes, say "
              "'Say second turn' without a wake word, then say 'stop listening'.", flush=True)
        if not wait_for_turn(manager, 1, timeout_seconds=60.0):
            return "FAIL_TURN_1", [event.name for event in events]
        print("LIVE_CONTINUOUS_TURN_1: PASS; now say 'Say second turn'.", flush=True)
        if not wait_for_turn(manager, 2, timeout_seconds=60.0):
            return "FAIL_TURN_2", [event.name for event in events]
        print("LIVE_CONTINUOUS_TURN_2: PASS; now say 'stop listening'.", flush=True)

        end_deadline = time.monotonic() + 40.0
        while time.monotonic() < end_deadline and manager.session is not None:
            time.sleep(0.05)
        ended_by_phrase = any(
            event.name == "session_ended" and event.data.get("reason") == "end_phrase"
            for event in events
        )
        return ("PASS" if ended_by_phrase else "FAIL_END_PHRASE"), [event.name for event in events]
    finally:
        core.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--continuous-only", action="store_true",
                        help="Run just the bounded private two-turn microphone acceptance.")
    args = parser.parse_args()
    if args.continuous_only:
        status, events = run_continuous_acceptance()
        print(f"LIVE_CONTINUOUS_RESULT: {status}; events={events}", flush=True)
        print(f"SUMMARY\tLIVE_CONTINUOUS\t{status}", flush=True)
        return 0 if status == "PASS" else 1

    catalog = discover_devices()
    print(f"DEVICES inputs={len(catalog.inputs)} outputs={len(catalog.outputs)} "
          f"default_input={catalog.default_input.name if catalog.default_input else None!r} "
          f"default_output={catalog.default_output.name if catalog.default_output else None!r}", flush=True)
    mic_status = "NOT_AVAILABLE"
    if catalog.default_input is not None:
        try:
            samples, sample_rate, device_name = capture_one_utterance()
            if samples is None:
                mic_status = "FAIL_TIMEOUT"
                print("LIVE_MIC_RESULT: no speech segment captured", flush=True)
            else:
                stt = SherpaOnnxSTT()
                try:
                    result = stt.transcribe(samples, sample_rate)
                finally:
                    stt.close()
                normalized = normalize_transcript(result.text)
                mic_status = "PASS" if "local" in normalized and "acceptance" in normalized else "FAIL_TRANSCRIPT"
                print(f"LIVE_MIC_RESULT: {mic_status}; device={device_name!r}; transcript={result.text!r}; "
                      f"stt_ms={result.duration_ms}", flush=True)
        except Exception as error:
            mic_status = f"FAIL_{type(error).__name__.upper()}"
            print(f"LIVE_MIC_RESULT: {mic_status}", flush=True)
    else:
        print("LIVE_MIC_RESULT: NOT_AVAILABLE", flush=True)

    tts_status = "NOT_AVAILABLE"
    first_audio_ms = None
    if catalog.default_output is not None:
        ok, first_audio_ms, output_detail = play_local_acceptance()
        tts_status = "PASS" if ok else "FAIL"
        print(f"LIVE_TTS_RESULT: {tts_status}; output={output_detail!r}; first_audio_ms={first_audio_ms}", flush=True)
    else:
        print("LIVE_TTS_RESULT: NOT_AVAILABLE", flush=True)

    continuous_status = "NOT_RUN"
    if mic_status == "PASS" and tts_status == "PASS":
        core = FridayCore()
        manager = core.voice_manager
        events = deque(maxlen=32)
        manager.add_event_listener(events.append)
        try:
            state = manager.start_conversation(privacy="private")
            if state != VoiceState.LISTENING.value:
                continuous_status = "FAIL_START"
            else:
                print("LIVE_CONTINUOUS_PROMPT: Say 'Friday, say ready'. After FRIDAY finishes, say "
                      "'Say second turn' without a wake word, then say 'stop listening'.", flush=True)
                first = wait_for_turn(manager, 1)
                if first:
                    print("LIVE_CONTINUOUS_TURN_1: PASS; now say 'Say second turn'.", flush=True)
                    second = wait_for_turn(manager, 2)
                    if second:
                        print("LIVE_CONTINUOUS_TURN_2: PASS; now say 'stop listening'.", flush=True)
                        end_deadline = time.monotonic() + 40.0
                        while time.monotonic() < end_deadline and manager.session is not None:
                            time.sleep(0.05)
                        ended_by_phrase = any(
                            event.name == "session_ended" and event.data.get("reason") == "end_phrase"
                            for event in events
                        )
                        continuous_status = "PASS" if ended_by_phrase else "FAIL_END_PHRASE"
                    else:
                        continuous_status = "FAIL_TURN_2"
                else:
                    continuous_status = "FAIL_TURN_1"
            print(f"LIVE_CONTINUOUS_RESULT: {continuous_status}; events="
                  f"{[event.name for event in events]}", flush=True)
        finally:
            core.close()
    else:
        print("LIVE_CONTINUOUS_RESULT: NOT_RUN (microphone/TTS acceptance did not pass)", flush=True)

    print(f"SUMMARY\tLIVE_MIC\t{mic_status}", flush=True)
    print(f"SUMMARY\tLIVE_TTS\t{tts_status}", flush=True)
    print(f"SUMMARY\tLIVE_CONTINUOUS\t{continuous_status}", flush=True)
    return 0 if mic_status == "PASS" and tts_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
