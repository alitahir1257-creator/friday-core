"""Deterministic local voice checks; no microphone or cloud access is required."""

from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import gc
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from friday.brain_manager import BrainReply, BrainRequestMetadata
from friday.capabilities.registry import Risk
from friday.core import FridayCore
from friday.profile import AssistantProfile
from friday.tools.models import ToolResult
from friday.voice import (
    AudioDevice,
    DeviceCatalog,
    TranscriptResult,
    VoiceConfig,
    VoiceManager,
    VoiceMode,
    VoiceState,
)
from friday.voice.audio import discover_devices
from friday.voice.models import STT_MODEL_DIR, VOICE_CACHE_DIR, WAKE_MODEL_DIR
from friday.voice.session import is_end_phrase, is_tts_echo, normalize_transcript
from friday.voice.stt import SherpaOnnxSTT
from friday.voice.tts import PiperLocalTTS
from friday.voice.vad import EnergyVAD
from friday.voice.wake import SherpaWakeWord


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_600
CATALOG = DeviceCatalog(
    inputs=(AudioDevice(0, "Synthetic microphone", 1, SAMPLE_RATE),),
    outputs=(AudioDevice(1, "Synthetic speaker", 1, SAMPLE_RATE),),
    default_input=AudioDevice(0, "Synthetic microphone", 1, SAMPLE_RATE),
    default_output=AudioDevice(1, "Synthetic speaker", 1, SAMPLE_RATE),
    available=True,
)


class FakeAudioInput:
    def __init__(self) -> None:
        self.device_name = "Synthetic microphone"
        self.callback = None
        self.persistence_writes = 0
        self.starts = 0
        self.stops = 0

    def start(self, callback) -> None:
        self.callback = callback
        self.starts += 1

    def stop(self) -> None:
        self.callback = None
        self.stops += 1


class FakeSTT:
    engine = "fixture-stt"
    available = True

    def __init__(self, texts=()) -> None:
        self.texts = deque(texts)
        self.seen_audio = 0

    def transcribe(self, samples, sample_rate=SAMPLE_RATE):
        self.seen_audio += len(samples)
        text = self.texts.popleft() if self.texts else "test voice turn"
        return TranscriptResult(text, None, None, 1.0, len(samples) * 1000 / sample_rate, self.engine)

    def close(self) -> None:
        return


class FakeTTS:
    engine = "fixture-tts"
    available = True
    voice_name = "fixture-voice"

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self._lock = threading.RLock()
        self._speaking = False
        self._stop = threading.Event()
        self._worker = None
        self.stop_calls = 0
        self.last_first_audio_latency_ms = 0.1
        self.last_error = None

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def speak(self, _text: str) -> None:
        with self._lock:
            if self._speaking:
                raise RuntimeError("TTS_BUSY")
            self._speaking = True
            self._stop.clear()

        def finish() -> None:
            self._stop.wait(30 if self.hold else 0.035)
            with self._lock:
                self._speaking = False

        self._worker = threading.Thread(target=finish, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()
        with self._lock:
            self._speaking = False

    def close(self) -> None:
        self.stop()


class FakeWake:
    engine = "fixture-wake"
    available = True
    last_detection_ms = 0.1

    def __init__(self, *, fire: bool = False) -> None:
        self.fire = fire

    def load(self) -> None:
        return

    def detect(self, _frame, _sample_rate=SAMPLE_RATE):
        if self.fire:
            self.fire = False
            return "FRIDAY"
        return None

    def close(self) -> None:
        return


class FakeBrainManager:
    unified = None
    memory = None

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.remote_calls = 0
        self.memory_writes = 0

    def respond(self, command, history, on_status=None, *, request_metadata=None, model_selection=None):
        self.calls.append({
            "command": command,
            "history": [message.copy() for message in history],
            "request_metadata": request_metadata,
            "selection": model_selection,
        })
        if request_metadata is not None and request_metadata.effective_privacy != "public":
            if request_metadata.allows_remote("gemini"):
                self.remote_calls += 1
        return BrainReply(text=f"FRIDAY heard {command}", selection=model_selection)

    def close(self) -> None:
        return


def make_core() -> tuple[FridayCore, FakeBrainManager]:
    brain = FakeBrainManager()
    return FridayCore(AssistantProfile(), brain_manager=brain), brain


def make_manager(
    *, core=None, texts=(), config: VoiceConfig | None = None,
    tts: FakeTTS | None = None, wake=None, catalog=CATALOG, audio_input=None,
):
    core = core or make_core()[0]
    stt = FakeSTT(texts)
    tts = tts or FakeTTS()
    manager = VoiceManager(
        core, config or VoiceConfig(continuous_timeout_seconds=4.0, silence_end_seconds=0.3),
        audio_input=audio_input or FakeAudioInput(), stt=stt, tts=tts,
        wake_detector=wake or FakeWake(), device_discovery=lambda: catalog,
    )
    return manager, core, stt, tts


def tone_frame(offset: int = 0, amplitude: float = 0.12) -> np.ndarray:
    positions = np.arange(FRAME_SAMPLES, dtype=np.float32) + offset
    return (amplitude * np.sin(2 * math.pi * 440 * positions / SAMPLE_RATE)).astype(np.float32)


def send_synthetic_utterance(manager: VoiceManager) -> None:
    for index in range(4):
        manager.submit_audio_frame(tone_frame(index * FRAME_SAMPLES))
        time.sleep(0.004)
    for _ in range(7):
        manager.submit_audio_frame(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        time.sleep(0.004)


def wait_for(predicate, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def process_memory_mb() -> float:
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return 0.0
    return round(counters.WorkingSetSize / (1024 * 1024), 2)


def available_memory_mb() -> float:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0.0
    return round(status.ullAvailPhys / (1024 * 1024), 2)


def run_existing(name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / name)],
        cwd=ROOT, capture_output=True, text=True, timeout=90, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"{name} failed: {(completed.stdout + completed.stderr)[-1200:]}")


def main() -> int:
    results: list[tuple[str, str, str]] = []
    metrics: dict[str, float | str] = {
        "process_baseline_mb": process_memory_mb(),
        "available_start_mb": available_memory_mb(),
    }

    def check(label, callback):
        print(f"RUN\t{label}", flush=True)
        try:
            detail = str(callback() or "")
            results.append((label, "PASS", detail))
        except Exception as error:
            results.append((label, "FAIL", f"{type(error).__name__}: {error}"))

    def devices():
        catalog = discover_devices()
        assert isinstance(catalog, DeviceCatalog)
        assert isinstance(catalog.inputs, tuple) and isinstance(catalog.outputs, tuple)
        for device in (*catalog.inputs, *catalog.outputs):
            assert device.name and device.sample_rate > 0
        return f"inputs={len(catalog.inputs)} outputs={len(catalog.outputs)} available={catalog.available}"

    def no_device():
        manager, core, _stt, _tts = make_manager(catalog=DeviceCatalog(available=False, error_code="NO_DEVICE"))
        try:
            assert manager.start_ptt() == "VOICE_UNAVAILABLE"
            assert manager.state == VoiceState.IDLE
        finally:
            manager.close()
            core.close()

    def vad_checks():
        silence = EnergyVAD(sample_rate=SAMPLE_RATE, frame_ms=100)
        for _ in range(20):
            assert silence.feed(np.zeros(FRAME_SAMPLES, dtype=np.float32)).utterance is None
        speech = EnergyVAD(sample_rate=SAMPLE_RATE, frame_ms=100, silence_end_seconds=0.3)
        detected = None
        speech_started = False
        for index in range(4):
            detected = speech.feed(tone_frame(index * FRAME_SAMPLES))
            speech_started = speech_started or detected.speech_started
        assert speech_started
        speech_ended = False
        utterance = None
        for _ in range(5):
            detected = speech.feed(np.zeros(FRAME_SAMPLES, dtype=np.float32))
            if detected.speech_ended:
                speech_ended = True
                utterance = detected.utterance
                break
        assert speech_ended and utterance is not None

    def wake_start():
        core, brain = make_core()
        manager, _core, _stt, _tts = make_manager(core=core, wake=FakeWake(fire=True))
        events = []
        manager.add_event_listener(events.append)
        try:
            assert manager.start_wake_listening() == VoiceState.WAKE_LISTENING.value
            send_synthetic_utterance(manager)
            assert wait_for(lambda: brain.calls and manager.state == VoiceState.LISTENING)
            assert any(event.name == "wake_detected" for event in events)
            assert manager.stop_wake_listening() == VoiceState.IDLE.value
        finally:
            manager.close()
            core.close()

    def ptt():
        core, brain = make_core()
        manager, _core, _stt, _tts = make_manager(core=core, texts=("push to talk works",))
        events = []
        manager.add_event_listener(events.append)
        try:
            assert manager.start_ptt() == VoiceState.LISTENING.value
            for index in range(4):
                manager.submit_audio_frame(tone_frame(index * FRAME_SAMPLES))
                time.sleep(0.005)
            assert wait_for(lambda: any(event.name == "speech_detected" for event in events))
            assert manager.stop_ptt() == VoiceState.PROCESSING.value
            assert wait_for(lambda: len(brain.calls) == 1 and manager.state == VoiceState.IDLE)
        finally:
            manager.close()
            core.close()

    def continuous_turns():
        core, brain = make_core()
        manager, _core, _stt, _tts = make_manager(
            core=core, texts=("first voice question", "second voice follow up"),
        )
        events = []
        manager.add_event_listener(events.append)
        try:
            assert manager.start_conversation() == VoiceState.LISTENING.value
            send_synthetic_utterance(manager)
            assert wait_for(lambda: len(brain.calls) == 1 and manager.state == VoiceState.LISTENING), (
                f"first turn stalled: calls={len(brain.calls)} state={manager.state.value}"
            )
            time.sleep(manager.config.post_tts_cooldown_seconds + 0.05)
            send_synthetic_utterance(manager)
            assert wait_for(lambda: len(brain.calls) == 2 and manager.state == VoiceState.LISTENING), (
                f"second turn stalled: calls={len(brain.calls)} state={manager.state.value} "
                f"deadline={manager._listen_deadline} events={[event.name for event in events]}"
            )
            assert brain.calls[1]["history"][-2]["content"] == "first voice question"
            required = {"listening_started", "speech_detected", "transcript_final", "processing_started",
                        "speaking_started", "speaking_stopped"}
            assert required <= {event.name for event in events}
            return f"turns={len(brain.calls)} events={len(events)}"
        finally:
            manager.close()
            core.close()

    def timeout_and_end_phrase():
        manager, core, _stt, _tts = make_manager(
            texts=("stop listening",), config=VoiceConfig(continuous_timeout_seconds=0.2),
        )
        try:
            assert manager.start_conversation() == VoiceState.LISTENING.value
            manager._process_utterance(tone_frame(), time.monotonic())
            assert manager.state == VoiceState.IDLE
            assert not core._brain_manager.calls
            assert is_end_phrase("Stop listening!")
            assert manager.start_conversation() == VoiceState.LISTENING.value
            manager._listen_deadline = time.monotonic() - 1
            manager._check_timeout()
            assert manager.state == VoiceState.IDLE
        finally:
            manager.close()
            core.close()

    def barge_in():
        manager, core, _stt, tts = make_manager(texts=("stop",), tts=FakeTTS(hold=True))
        events = []
        manager.add_event_listener(events.append)
        try:
            assert manager.start_conversation() == VoiceState.LISTENING.value
            tts.speak("A long, harmless response for interruption testing.")
            manager._session.transition(VoiceState.SPEAKING)
            manager._vad = manager._new_vad(threshold=manager.config.barge_in_rms)
            for index in range(3):
                manager._handle_frame(tone_frame(index * FRAME_SAMPLES))
            assert tts.stop_calls > 0
            assert manager.state == VoiceState.LISTENING
            for index in range(3, 6):
                manager._handle_frame(tone_frame(index * FRAME_SAMPLES))
            for _ in range(5):
                manager._handle_frame(np.zeros(FRAME_SAMPLES, dtype=np.float32))
            assert wait_for(lambda: bool(core._brain_manager.calls)), (
                f"barge-in utterance stalled: state={manager.state.value}"
            )
            assert any(event.name == "barge_in" for event in events)
            assert manager._last_latency.get("barge_in_stop_ms") is not None
            metrics["barge_in_stop_ms"] = manager._last_latency["barge_in_stop_ms"]
        finally:
            manager.close()
            core.close()

    def self_voice_loop():
        core, _brain = make_core()
        manager, _core, _stt, _tts = make_manager(core=core, texts=("This is a sufficiently long answer from Friday.",))
        try:
            assert manager.start_conversation() == VoiceState.LISTENING.value
            manager._last_tts_text = "This is a sufficiently long answer from Friday."
            manager._last_tts_finished = time.monotonic()
            manager._process_utterance(tone_frame(), time.monotonic())
            assert not core._brain_manager.calls
            assert manager.state == VoiceState.LISTENING
            assert is_tts_echo("This is a sufficiently long answer from Friday.", manager._last_tts_text)
        finally:
            manager.close()
            core.close()

    def privacy_context():
        core, brain = make_core()
        manager, _core, _stt, _tts = make_manager(core=core, texts=("private voice request",))
        try:
            assert manager.start_conversation(privacy="private") == VoiceState.LISTENING.value
            manager._process_utterance(tone_frame(), time.monotonic())
            call = brain.calls[0]
            metadata = call["request_metadata"]
            assert isinstance(metadata, BrainRequestMetadata)
            assert metadata.effective_privacy == "private" and not metadata.allows_remote("gemini")
            assert isinstance(call["command"], str)
            assert brain.remote_calls == 0 and brain.memory_writes == 0
            assert manager._session_history is not None
            assert manager._session_history.messages()[0]["content"] == "private voice request"
        finally:
            manager.close()
            core.close()

    def no_audio_persistence():
        audio_input = FakeAudioInput()
        manager, core, _stt, _tts = make_manager(audio_input=audio_input)
        try:
            assert manager.start_ptt() == VoiceState.LISTENING.value
            for index in range(3):
                manager.submit_audio_frame(tone_frame(index * FRAME_SAMPLES))
                time.sleep(0.005)
            assert wait_for(lambda: manager._vad.active)
            manager.stop_ptt()
            assert audio_input.persistence_writes == 0
        finally:
            manager.close()
            core.close()

    def computer_and_approval():
        core, _brain = make_core()
        calls = []
        original = core.tool_registry.execute

        def capture(name, arguments, **kwargs):
            calls.append((name, arguments, kwargs))
            return ToolResult(True, name, "success", "Opened Notepad.")

        core.tool_registry.execute = capture
        try:
            _answer, _tokens, decision = core.respond("open notepad")
            assert decision.route.value == "COMPUTER"
            assert calls and calls[0][0] == "computer.open_app" and calls[0][1] == {"app": "notepad"}
        finally:
            core.tool_registry.execute = original
        _answer, _tokens, decision = core.respond("delete this file")
        assert decision.route.value == "COMPUTER"
        assert core.pending_approval == "delete this file"
        assert core.last_tool_result is not None and core.last_tool_result.requires_followup
        assert core.last_tool_result.status == "approval_required"
        core.close()

    def local_models():
        free_before = available_memory_mb()
        print("RUN\tMODEL_WAKE_LOAD", flush=True)
        wake = SherpaWakeWord()
        assert wake.available, "Sherpa wake model or runtime package is missing"
        wake.load()
        metrics["wake_loaded_rss_mb"] = process_memory_mb()
        metrics["wake_detector_ram_mb"] = round(
            max(0.0, metrics["wake_loaded_rss_mb"] - metrics["process_baseline_mb"]), 2
        )
        metrics["available_after_wake_mb"] = available_memory_mb()
        wake.close()

        print("RUN\tMODEL_PIPER_SYNTHESIS", flush=True)
        piper = PiperLocalTTS()
        assert piper.available, "Piper voice model or runtime package is missing"
        generate_started = time.perf_counter()
        audio = piper.synthesize("FRIDAY VOICE OK")
        metrics["tts_generate_ms"] = round((time.perf_counter() - generate_started) * 1000, 2)
        assert audio.samples.size > 1000 and audio.sample_rate > 0
        metrics["tts_loaded_rss_mb"] = process_memory_mb()
        metrics["tts_loaded_ram_mb"] = round(
            max(0.0, metrics["tts_loaded_rss_mb"] - metrics["process_baseline_mb"]), 2
        )
        metrics["available_after_tts_mb"] = available_memory_mb()
        piper.close()

        print("RUN\tMODEL_SHERPA_STT", flush=True)
        stt = SherpaOnnxSTT()
        assert stt.available, "Sherpa STT model or runtime package is missing"
        stt_baseline_mb = process_memory_mb()
        fixture = ROOT / "tests" / "fixtures" / "voice" / "phase9a_seashells.wav"
        with wave.open(str(fixture), "rb") as source:
            assert source.getnchannels() == 1 and source.getsampwidth() == 2
            fixture_rate = source.getframerate()
            fixture_audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        transcript = stt.transcribe(fixture_audio.astype(np.float32) / 32768.0, fixture_rate)
        metrics["stt_ms"] = transcript.duration_ms
        metrics["stt_loaded_rss_mb"] = process_memory_mb()
        metrics["stt_loaded_ram_mb"] = round(
            max(0.0, metrics["stt_loaded_rss_mb"] - stt_baseline_mb), 2
        )
        metrics["available_after_stt_mb"] = available_memory_mb()
        normalized = normalize_transcript(transcript.text)
        assert all(term in normalized for term in ("seashells", "seashore")), (
            f"unexpected fixture transcript: {transcript.text!r}"
        )
        stt.close()
        wake.close()
        gc.collect()

        print("RUN\tMODEL_PIPER_INTERRUPT", flush=True)
        tts_speech = PiperLocalTTS()
        tts_speech_audio = tts_speech.synthesize("FRIDAY VOICE OK")
        assert tts_speech_audio.samples.size > 1000
        tts_speech.close()

        class BlockingOutput:
            def __init__(self):
                self.started = threading.Event()

            def play(self, _samples, _rate, cancel_event, *, on_started=None):
                if on_started:
                    on_started()
                self.started.set()
                cancel_event.wait(10)

            def stop(self):
                return

        interruptible = PiperLocalTTS(output=BlockingOutput())
        interruptible.speak("FRIDAY VOICE OK")
        assert interruptible.output.started.wait(5), "Piper playback did not start"
        started = time.perf_counter()
        interruptible.stop()
        assert wait_for(lambda: not interruptible.is_speaking, timeout=2)
        metrics["tts_stop_ms"] = round((time.perf_counter() - started) * 1000, 2)
        metrics["tts_first_audio_ms"] = interruptible.last_first_audio_latency_ms or 0.0
        interruptible.close()
        gc.collect()
        metrics["process_after_cleanup_mb"] = process_memory_mb()
        metrics["available_after_cleanup_mb"] = available_memory_mb()
        metrics["available_minimum_mb"] = min(
            free_before, metrics["available_after_wake_mb"], metrics["available_after_tts_mb"],
            metrics["available_after_stt_mb"], metrics["available_after_cleanup_mb"],
        )
        metrics["model_directory"] = str(STT_MODEL_DIR)
        metrics["wake_model_directory"] = str(WAKE_MODEL_DIR)
        metrics["cache_directory"] = str(VOICE_CACHE_DIR)
        return f"transcript={transcript.text!r}; fixture='She sells seashells by the seashore'; generated_samples={audio.samples.size}"

    check("A_DEVICE_ENUMERATION", devices)
    check("B_NO_DEVICE", no_device)
    check("C_D_VAD_SILENCE_AND_SPEECH", vad_checks)
    check("E_F_G_X_LOCAL_STT_TTS_AND_STOP", local_models)
    check("H_WAKE_SESSION_START", wake_start)
    check("O_P_Q_J_CONTINUOUS_PTT_CONTEXT_EVENTS", ptt)
    check("I_J_U_CONTINUOUS_TWO_TURNS_AND_EVENTS", continuous_turns)
    check("K_L_TIMEOUT_AND_END_PHRASE", timeout_and_end_phrase)
    check("M_T_BARGE_IN_AND_TTS_INTERRUPT", barge_in)
    check("N_SELF_VOICE_LOOP", self_voice_loop)
    check("O_PRIVATE_RAW_AUDIO_REMOTE_AND_MEMORY", privacy_context)
    check("P_Q_R_COMPUTER_TOOL_APPROVAL", computer_and_approval)
    check("S_NO_AUDIO_FILE_PERSISTENCE", no_audio_persistence)
    check("V_PHASE_8A3_REGRESSION", lambda: run_existing("phase8a3_smoke.py"))
    check("W_PRIVACY_REGRESSION", lambda: run_existing("phase5c_smoke.py"))

    for label, status, detail in results:
        print(f"{status}\t{label}" + (f"\t{detail}" if detail else ""))
    for key, value in metrics.items():
        print(f"METRIC\t{key}\t{value}")
    failed = [label for label, status, _detail in results if status != "PASS"]
    print(f"SUMMARY\t{'FAIL' if failed else 'PASS'}\t{len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
