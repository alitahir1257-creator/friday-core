"""Local voice lifecycle, bounded turn processing, and continuous sessions."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time
from typing import Callable

import numpy as np

from friday.conversation import ConversationHistory
from friday.voice.audio import AudioInput, AudioUnavailable, discover_devices
from friday.voice.models import (
    ConversationSession,
    DeviceCatalog,
    TranscriptResult,
    VoiceConfig,
    VoiceEvent,
    VoiceMode,
    VoiceState,
    VoiceStatus,
)
from friday.voice.session import SessionState, is_end_phrase, is_tts_echo, strip_wake_prefix
from friday.voice.stt import SherpaOnnxSTT
from friday.voice.tts import PiperLocalTTS
from friday.voice.vad import EnergyVAD
from friday.voice.wake import SherpaWakeWord


END_OF_QUEUE = object()
PRIVATE_LEVELS = {"private", "local_only"}


class VoiceManager:
    """A thin voice front end over FridayCore.respond; no separate chat brain."""

    def __init__(
        self,
        core,
        config: VoiceConfig | None = None,
        *,
        audio_input: AudioInput | None = None,
        stt=None,
        tts=None,
        wake_detector=None,
        device_discovery: Callable[[], DeviceCatalog] | None = None,
        event_listener: Callable[[VoiceEvent], None] | None = None,
    ) -> None:
        self.core = core
        self.config = config or VoiceConfig()
        self.audio_input = audio_input or AudioInput(
            device=self.config.input_device, frame_ms=self.config.frame_ms,
        )
        self.stt = stt or SherpaOnnxSTT(self.config)
        self.tts = tts or PiperLocalTTS(self.config)
        self.wake_detector = wake_detector or SherpaWakeWord(self.config)
        self._device_discovery = device_discovery or discover_devices
        self._session = SessionState()
        self._events: list[Callable[[VoiceEvent], None]] = []
        if event_listener is not None:
            self._events.append(event_listener)
        self._frame_queue: queue.Queue[object] = queue.Queue(maxsize=32)
        self._turn_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self._lock = threading.RLock()
        self._audio_thread: threading.Thread | None = None
        self._turn_thread: threading.Thread | None = None
        self._closed = False
        self._audio_started = False
        self._wake_armed = False
        self._wake_privacy = "public"
        self._ptt_active = False
        self._ptt_segments: list[np.ndarray] = []
        self._ptt_sample_count = 0
        self._ptt_max_samples = int(self.config.sample_rate * self.config.max_utterance_seconds)
        self._vad = self._new_vad()
        self._wake_vad = self._new_vad()
        self._wake_recent: np.ndarray | None = None
        self._wake_recent_at = 0.0
        self._strip_wake_once = False
        self._session_history: ConversationHistory | None = None
        self._listen_deadline: float | None = None
        self._cooldown_until = 0.0
        self._interrupt_event = threading.Event()
        self._current_tts_text = ""
        self._current_tts_started = 0.0
        self._last_tts_text = ""
        self._last_tts_finished = 0.0
        self._last_barge_in_stop_ms: float | None = None
        self._mic_device: str | None = None
        self._last_latency: dict[str, float] = {}
        self._last_error_code: str | None = None

    @property
    def state(self) -> VoiceState:
        return self._session.state

    @property
    def session(self) -> ConversationSession | None:
        return self._session.current

    def add_event_listener(self, listener: Callable[[VoiceEvent], None]) -> None:
        with self._lock:
            if listener not in self._events:
                self._events.append(listener)

    def remove_event_listener(self, listener: Callable[[VoiceEvent], None]) -> None:
        with self._lock:
            if listener in self._events:
                self._events.remove(listener)

    def device_catalog(self) -> DeviceCatalog:
        return self._device_discovery()

    def status(self) -> VoiceStatus:
        catalog = self.device_catalog()
        code = self._availability_code(catalog, require_wake=self.state == VoiceState.WAKE_LISTENING)
        session = self.session
        return VoiceStatus(
            mode=session.mode.value if session else ("wake" if self._wake_armed else "idle"),
            state=self.state.value,
            stt_engine=str(getattr(self.stt, "engine", "unavailable")),
            tts_engine=str(getattr(self.tts, "engine", "unavailable")),
            voice_name=str(getattr(self.tts, "voice_name", Path(self.config.tts_model_path).stem)),
            mic_device=self._mic_device or (catalog.default_input.name if catalog.default_input else None),
            session_duration_seconds=session.duration_seconds if session else 0.0,
            turn_count=session.turn_count if session else 0,
            last_latency=dict(self._last_latency),
            available=code is None,
            availability_code=code or "READY",
        )

    def start_wake_listening(self, *, privacy: str = "public") -> str:
        """Explicitly arm local wake listening; it is never enabled on app startup."""
        privacy = self._validate_privacy(privacy)
        with self._lock:
            if self.session is not None:
                return "SESSION_ACTIVE"
            catalog = self.device_catalog()
            code = self._availability_code(catalog, require_wake=True)
            if code:
                return self._unavailable(code, phase="wake")
            try:
                self.wake_detector.load()
                self._start_audio(catalog)
            except Exception as error:
                return self._unavailable(self._error_code(error), phase="wake")
            self._wake_armed = True
            self._wake_privacy = privacy
            self._wake_recent = None
            self._wake_recent_at = 0.0
            self._wake_vad.reset()
            self._session.transition(VoiceState.WAKE_LISTENING)
            self._emit("voice_idle", waiting_for_wake=True)
            return VoiceState.WAKE_LISTENING.value

    def start_conversation(self, *, privacy: str = "public") -> str:
        """Start a continuous local microphone session without a wake phrase."""
        return self._start_continuous(VoiceMode.WAKE, privacy, require_wake=False)

    def start_ptt(self, *, privacy: str = "public") -> str:
        """Begin bounded push-to-talk capture; the release processes its final audio."""
        privacy = self._validate_privacy(privacy)
        with self._lock:
            if self.state == VoiceState.SPEAKING:
                self.tts.stop()
                self._interrupt_event.set()
                self._session.transition(VoiceState.INTERRUPTED)
                self._emit("barge_in", source="push_to_talk")
                if self.session is not None:
                    self.session.mode = VoiceMode.PTT
                self._ptt_active = True
                self._ptt_segments.clear()
                self._ptt_sample_count = 0
                self._vad = self._new_vad(threshold=self.config.barge_in_rms)
                self._session.transition(VoiceState.LISTENING)
                return VoiceState.LISTENING.value
            if self.session is not None:
                return "SESSION_ACTIVE"
            catalog = self.device_catalog()
            code = self._availability_code(catalog, require_wake=False)
            if code:
                return self._unavailable(code, phase="ptt")
            try:
                self._start_audio(catalog)
            except Exception as error:
                return self._unavailable(self._error_code(error), phase="ptt")
            self._begin_session(VoiceMode.PTT, privacy)
            self._ptt_active = True
            self._ptt_segments.clear()
            self._ptt_sample_count = 0
            self._vad = self._new_vad()
            self._emit("listening_started", mode=VoiceMode.PTT.value)
            return VoiceState.LISTENING.value

    def stop_ptt(self) -> str:
        with self._lock:
            session = self.session
            if session is None or session.mode != VoiceMode.PTT or not self._ptt_active:
                return "PTT_NOT_ACTIVE"
            self._ptt_active = False
            remaining = self._vad.flush()
            if remaining is not None:
                self._append_ptt_segment(remaining)
            self._stop_audio()
            if self._ptt_sample_count == 0:
                self._end_session("no_speech")
                return VoiceState.IDLE.value
            audio = np.concatenate(self._ptt_segments)
            self._ptt_segments.clear()
            self._ptt_sample_count = 0
            self._queue_turn(audio)
            return VoiceState.PROCESSING.value

    def end_session(self, reason: str = "explicit") -> str:
        with self._lock:
            self.tts.stop()
            self._interrupt_event.set()
            self._end_session(reason)
            if not self._wake_armed:
                self._stop_audio()
            return self.state.value

    def stop_wake_listening(self) -> str:
        with self._lock:
            self._wake_armed = False
            self.wake_detector.close()
            if self.session is not None:
                self._end_session("wake_listening_stopped")
            self._stop_audio()
            self._session.transition(VoiceState.IDLE)
            self._emit("voice_idle")
            return VoiceState.IDLE.value

    def submit_audio_frame(self, samples, sample_rate: int | None = None) -> None:
        """Bounded frame injection used by tests and future local audio adapters."""
        self._queue_audio(samples, sample_rate or self.config.sample_rate)

    def wait_for_state(self, state: VoiceState | str, timeout: float = 5.0) -> bool:
        expected = state.value if isinstance(state, VoiceState) else str(state)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.state.value == expected:
                return True
            time.sleep(0.01)
        return self.state.value == expected

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._wake_armed = False
            self._ptt_active = False
            self.tts.stop()
            self._stop_audio()
            self._put_sentinel(self._frame_queue)
            self._put_sentinel(self._turn_queue)
            self.wake_detector.close()
            self.stt.close()
            self.tts.close()
            if self.session is not None:
                self._end_session("closed")
        for worker in (self._audio_thread, self._turn_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1.0)

    def _start_continuous(self, mode: VoiceMode, privacy: str, *, require_wake: bool) -> str:
        privacy = self._validate_privacy(privacy)
        with self._lock:
            if self.session is not None:
                return "SESSION_ACTIVE"
            catalog = self.device_catalog()
            code = self._availability_code(catalog, require_wake=require_wake)
            if code:
                return self._unavailable(code, phase="session")
            try:
                if require_wake:
                    self.wake_detector.load()
                self._start_audio(catalog)
            except Exception as error:
                return self._unavailable(self._error_code(error), phase="session")
            self._wake_armed = require_wake
            self._begin_session(mode, privacy)
            self._emit("listening_started", mode=mode.value)
            return VoiceState.LISTENING.value

    def _availability_code(self, catalog: DeviceCatalog, *, require_wake: bool) -> str | None:
        selected_input = next(
            (item for item in catalog.inputs if item.index == self.config.input_device),
            catalog.default_input if self.config.input_device is None else None,
        )
        selected_output = next(
            (item for item in catalog.outputs if item.index == self.config.output_device),
            catalog.default_output if self.config.output_device is None else None,
        )
        if selected_input is None or selected_output is None:
            return "VOICE_UNAVAILABLE"
        if not bool(getattr(self.stt, "available", False)):
            return "VOICE_UNAVAILABLE"
        if not bool(getattr(self.tts, "available", False)):
            return "VOICE_UNAVAILABLE"
        if require_wake and not bool(getattr(self.wake_detector, "available", False)):
            return "VOICE_UNAVAILABLE"
        return None

    def _unavailable(self, code: str, *, phase: str) -> str:
        self._last_error_code = code
        self._emit("voice_error", code=code, phase=phase)
        return "VOICE_UNAVAILABLE"

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if message in {
            "SOUNDDEVICE_INSTALL_REQUIRED", "MICROPHONE_UNAVAILABLE", "SPEAKER_UNAVAILABLE",
            "STT_MODEL_MISSING", "STT_INSTALL_REQUIRED", "TTS_MODEL_MISSING",
            "TTS_INSTALL_REQUIRED", "WAKE_MODEL_MISSING", "SENTENCEPIECE_INSTALL_REQUIRED",
            "WAKE_TOKENIZER_MISSING", "WAKE_PHRASE_INVALID", "WAKE_PHRASE_UNSUPPORTED_BY_MODEL",
        }:
            return message
        return type(error).__name__.upper()

    def _start_audio(self, catalog: DeviceCatalog) -> None:
        if not self._audio_started:
            self._ensure_workers()
            self.audio_input.start(self._queue_audio)
            self._audio_started = True
        self._mic_device = getattr(self.audio_input, "device_name", None)
        if self._mic_device is None and catalog.default_input is not None:
            self._mic_device = catalog.default_input.name

    def _stop_audio(self) -> None:
        if self._audio_started:
            try:
                self.audio_input.stop()
            finally:
                self._audio_started = False

    def _ensure_workers(self) -> None:
        self._closed = False
        if self._audio_thread is None or not self._audio_thread.is_alive():
            self._audio_thread = threading.Thread(
                target=self._audio_loop, name="friday-voice-audio", daemon=True,
            )
            self._audio_thread.start()
        if self._turn_thread is None or not self._turn_thread.is_alive():
            self._turn_thread = threading.Thread(
                target=self._turn_loop, name="friday-voice-turn", daemon=True,
            )
            self._turn_thread.start()

    def _queue_audio(self, samples, sample_rate: int) -> None:
        if self._closed:
            return
        frame = (np.asarray(samples, dtype=np.float32).reshape(-1), int(sample_rate))
        try:
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass

    def _audio_loop(self) -> None:
        while not self._closed:
            try:
                item = self._frame_queue.get(timeout=0.05)
            except queue.Empty:
                self._check_timeout()
                continue
            if item is END_OF_QUEUE:
                return
            samples, sample_rate = item
            try:
                frame = _resample(samples, sample_rate, self.config.sample_rate)
                self._handle_frame(frame)
            except Exception as error:
                self._emit("voice_error", code=self._error_code(error), phase="audio_frame")

    def _handle_frame(self, frame: np.ndarray) -> None:
        current = self.state
        if current == VoiceState.WAKE_LISTENING:
            self._handle_wake_frame(frame)
            return
        if current == VoiceState.SPEAKING:
            result = self._vad.feed(frame, threshold=self.config.barge_in_rms)
            if result.speech_started:
                self._interrupt_event.set()
                stop_started = time.perf_counter()
                self.tts.stop()
                self._last_barge_in_stop_ms = round((time.perf_counter() - stop_started) * 1000, 2)
                self._last_latency["barge_in_stop_ms"] = self._last_barge_in_stop_ms
                self._session.transition(VoiceState.INTERRUPTED)
                self._emit(
                    "barge_in", source="microphone",
                    stop_latency_ms=self._last_barge_in_stop_ms,
                )
                self._emit("speaking_stopped", reason="barge_in")
                self._session.transition(VoiceState.LISTENING)
                if self.session is not None:
                    self.session.touch()
                self._listen_deadline = None
            if result.speech_ended and result.utterance is not None:
                self._queue_turn(result.utterance)
            return
        if current != VoiceState.LISTENING:
            return
        if self._ptt_active:
            result = self._vad.feed(frame)
            if result.speech_started:
                self._emit("speech_detected", mode=VoiceMode.PTT.value)
            if result.utterance is not None:
                self._append_ptt_segment(result.utterance)
            return
        if time.monotonic() < self._cooldown_until:
            result = self._vad.feed(frame, threshold=self.config.barge_in_rms)
            if result.speech_started:
                self._listen_deadline = None
                if self.session is not None:
                    self.session.touch()
                self._emit("speech_detected", mode=self.session.mode.value if self.session else "wake")
            if result.speech_ended and result.utterance is not None:
                self._queue_turn(result.utterance)
            return
        result = self._vad.feed(frame)
        if result.speech_started:
            self._listen_deadline = None
            if self.session is not None:
                self.session.touch()
            self._emit("speech_detected", mode=self.session.mode.value if self.session else "wake")
        if result.speech_ended and result.utterance is not None:
            self._queue_turn(result.utterance)

    def _handle_wake_frame(self, frame: np.ndarray) -> None:
        result = self._wake_vad.feed(frame)
        if result.utterance is not None:
            self._wake_recent = result.utterance
            self._wake_recent_at = time.monotonic()
        try:
            keyword = self.wake_detector.detect(frame, self.config.sample_rate)
        except Exception as error:
            self._emit("voice_error", code=self._error_code(error), phase="wake_detect")
            self._wake_armed = False
            self._session.transition(VoiceState.ERROR)
            self._stop_audio()
            return
        if not keyword:
            return
        activation_audio = self._wake_vad.snapshot()
        if activation_audio is None and time.monotonic() - self._wake_recent_at < 1.0:
            activation_audio = self._wake_recent
        self._wake_vad.reset()
        self.wake_detector.close()
        self._begin_session(VoiceMode.WAKE, getattr(self, "_wake_privacy", "public"))
        self._strip_wake_once = True
        self._emit(
            "wake_detected", keyword=keyword,
            detection_ms=float(getattr(self.wake_detector, "last_detection_ms", 0.0) or 0.0),
        )
        self._emit("listening_started", mode=VoiceMode.WAKE.value)
        if activation_audio is not None:
            frame_size = max(1, round(self.config.sample_rate * self.config.frame_ms / 1000))
            for offset in range(0, len(activation_audio), frame_size):
                self._handle_session_audio(activation_audio[offset:offset + frame_size])

    def _handle_session_audio(self, frame: np.ndarray) -> None:
        result = self._vad.feed(frame)
        if result.speech_started:
            self._listen_deadline = None
            self._emit("speech_detected", mode=VoiceMode.WAKE.value)
        if result.speech_ended and result.utterance is not None:
            self._queue_turn(result.utterance)

    def _queue_turn(self, samples: np.ndarray) -> None:
        session = self.session
        if session is None or samples is None or len(samples) == 0:
            return
        session.touch()
        self._session.transition(VoiceState.PROCESSING)
        self._emit("processing_started", audio_duration_ms=round(len(samples) * 1000 / self.config.sample_rate, 2))
        item = (np.asarray(samples, dtype=np.float32).copy(), time.monotonic())
        try:
            self._turn_queue.put_nowait(item)
        except queue.Full:
            self._emit("voice_error", code="TURN_QUEUE_FULL", phase="process")
            self._session.transition(VoiceState.LISTENING)
            self._listen_deadline = time.monotonic() + self.config.continuous_timeout_seconds

    def _turn_loop(self) -> None:
        while not self._closed:
            try:
                item = self._turn_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is END_OF_QUEUE:
                return
            samples, speech_end_mark = item
            try:
                self._process_utterance(samples, speech_end_mark)
            except Exception as error:
                self._emit("voice_error", code=self._error_code(error), phase="turn")
                if self.session is not None:
                    self._session.transition(VoiceState.LISTENING)
                    self._listen_deadline = time.monotonic() + self.config.continuous_timeout_seconds

    def _process_utterance(self, samples: np.ndarray, speech_end_mark: float) -> None:
        session = self.session
        if session is None:
            return
        stt_started = time.perf_counter()
        try:
            transcript = self.stt.transcribe(samples, self.config.sample_rate)
        finally:
            # Release STT before BrainManager can load local Qwen on an 8 GB host.
            self.stt.close()
        stt_ms = round((time.perf_counter() - stt_started) * 1000, 2)
        text = transcript.text.strip()[: self.config.max_transcript_chars]
        if self._strip_wake_once:
            text = strip_wake_prefix(text, self.config.wake_phrase)
            self._strip_wake_once = False
        self._emit(
            "transcript_final", text=text, confidence=transcript.confidence,
            language=transcript.language, engine=transcript.engine,
            duration_ms=transcript.duration_ms, audio_duration_ms=transcript.audio_duration_ms,
        )
        if not text:
            self._resume_listening()
            return
        if is_end_phrase(text, self.config.assistant_name):
            self._end_session("end_phrase")
            return
        if (
            time.monotonic() - self._last_tts_finished <= 8.0
            and is_tts_echo(text, self._last_tts_text)
        ):
            self._resume_listening()
            return

        session = self.session
        if session is None:
            return
        session.current_transcript = text
        session.turn_count += 1
        session.touch()
        brain_started = time.perf_counter()
        request_metadata = self._request_metadata(session.privacy)
        kwargs = {"on_status": self._on_core_status}
        if request_metadata is not None:
            kwargs["request_metadata"] = request_metadata
        if self._session_history is not None:
            kwargs["history_override"] = self._session_history.messages()
        response = self.core.respond(text, **kwargs)
        answer = response[0] if isinstance(response, tuple) else str(response)
        response_ms = round((time.perf_counter() - brain_started) * 1000, 2)
        selection = getattr(self.core, "last_model_selection", None)
        model_ms = float(getattr(selection, "latency_ms", 0.0) or 0.0)
        barge_stop_ms = float(self._last_barge_in_stop_ms or 0.0)
        self._last_barge_in_stop_ms = None
        self._last_latency = {
            "wake_detection_ms": float(getattr(self.wake_detector, "last_detection_ms", 0.0) or 0.0),
            "speech_end_detection_ms": round(max(0.0, (time.monotonic() - speech_end_mark) * 1000), 2),
            "stt_ms": stt_ms,
            "brain_orchestration_ms": round(max(0.0, response_ms - model_ms), 2),
            "brain_model_ms": round(model_ms, 2),
            "total_local_overhead_ms": round(
                stt_ms + max(0.0, response_ms - model_ms)
                + barge_stop_ms, 2,
            ),
        }
        if barge_stop_ms:
            self._last_latency["barge_in_stop_ms"] = barge_stop_ms
        if self._session_history is not None:
            self._session_history.add_exchange(text, answer)
        self._release_owned_local_brain()
        self._speak_answer(answer)

    @staticmethod
    def _request_metadata(privacy: str):
        if privacy not in PRIVATE_LEVELS:
            return None
        from friday.brain_manager import BrainRequestMetadata
        from friday.model_policy import ModelPolicy

        return BrainRequestMetadata(
            sensitivity="private", privacy=privacy, task_class=ModelPolicy.PRIVATE.value,
        )

    def _speak_answer(self, answer: str) -> None:
        text = " ".join(str(answer).split())
        if not text:
            self._resume_listening()
            return
        if len(text) > self.config.max_tts_chars:
            text = text[: self.config.max_tts_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
        self._current_tts_text = text
        self._current_tts_started = time.monotonic()
        self._interrupt_event.clear()
        self._session.transition(VoiceState.SPEAKING)
        self._emit("speaking_started", engine=getattr(self.tts, "engine", "local"))
        try:
            self.tts.speak(text)
        except Exception as error:
            self._emit("voice_error", code=self._error_code(error), phase="tts_start")
            self._resume_listening()
            return
        self._vad = self._new_vad(threshold=self.config.barge_in_rms)
        while self.tts.is_speaking and not self._closed and not self._interrupt_event.wait(0.02):
            pass
        first_audio_ms = getattr(self.tts, "last_first_audio_latency_ms", None)
        if first_audio_ms is not None:
            self._last_latency["tts_first_audio_ms"] = float(first_audio_ms)
            self._last_latency["total_local_overhead_ms"] = round(
                self._last_latency.get("total_local_overhead_ms", 0.0) + float(first_audio_ms), 2
            )
        if getattr(self.tts, "last_error", None):
            self._emit("voice_error", code=str(self.tts.last_error), phase="tts")
        interrupted = self._interrupt_event.is_set()
        if not interrupted:
            self._last_tts_text = text
            self._last_tts_finished = time.monotonic()
            self._release_voice_engines()
            self._vad = self._new_vad()
            self._emit("speaking_stopped", reason="complete")
            self._resume_listening()
        elif self.session is not None:
            self._last_tts_text = text
            self._last_tts_finished = time.monotonic()
            self._release_voice_engines()
            # Keep the barge-in VAD state so it can finish the user's utterance.
            self._resume_listening(reset_deadline=False, post_tts_cooldown=False)
    def _resume_listening(
        self, *, reset_deadline: bool = True, post_tts_cooldown: bool = True,
    ) -> None:
        session = self.session
        if session is None:
            return
        self._session.transition(VoiceState.LISTENING)
        session.touch()
        self._cooldown_until = (
            time.monotonic() + self.config.post_tts_cooldown_seconds
            if post_tts_cooldown else 0.0
        )
        self._listen_deadline = (
            time.monotonic() + self.config.continuous_timeout_seconds if reset_deadline else None
        )
        self._emit("listening_started", mode=session.mode.value)
        if session.mode == VoiceMode.PTT:
            self._end_session("ptt_turn_complete")

    def _check_timeout(self) -> None:
        if self._listen_deadline is None or time.monotonic() < self._listen_deadline:
            return
        if self.state == VoiceState.LISTENING and self.session is not None:
            self._end_session("silence_timeout")

    def _begin_session(self, mode: VoiceMode, privacy: str) -> None:
        normalized_privacy = self._validate_privacy(privacy)
        self._session.begin(mode, self.config.continuous_timeout_seconds, normalized_privacy)
        self._session_history = ConversationHistory(max_turns=6)
        if normalized_privacy == "public":
            snapshot = getattr(self.core, "history_snapshot", None)
            if callable(snapshot):
                messages = snapshot()
                for index in range(0, len(messages) - 1, 2):
                    user, assistant = messages[index:index + 2]
                    if user.get("role") == "user" and assistant.get("role") == "assistant":
                        self._session_history.add_exchange(
                            user.get("content", ""), assistant.get("content", "")
                        )
        self._vad = self._new_vad()
        self._interrupt_event.clear()
        self._listen_deadline = time.monotonic() + self.config.continuous_timeout_seconds

    @staticmethod
    def _validate_privacy(privacy: str) -> str:
        normalized = str(privacy).casefold()
        if normalized not in {"public", "private", "local_only"}:
            raise ValueError("Voice privacy must be public, private, or local_only.")
        return normalized

    def _end_session(self, reason: str) -> None:
        session = self.session
        if session is None:
            self._session.transition(VoiceState.IDLE)
            self._emit("voice_idle")
            return
        self._session.transition(VoiceState.ENDING)
        self._emit(
            "session_ended", reason=reason, turn_count=session.turn_count,
            duration_seconds=round(session.duration_seconds, 2),
        )
        prior_mode = session.mode
        self._session.end()
        self._session_history = None
        self._listen_deadline = None
        self._ptt_active = False
        self._ptt_segments.clear()
        self._ptt_sample_count = 0
        self._strip_wake_once = False
        self._current_tts_text = ""
        self.wake_detector.close()
        if prior_mode == VoiceMode.PTT or not self._wake_armed:
            self._stop_audio()
            self._emit("voice_idle")
            return
        try:
            self.wake_detector.load()
            self._wake_vad = self._new_vad()
            self._session.transition(VoiceState.WAKE_LISTENING)
            self._emit("voice_idle", waiting_for_wake=True)
        except Exception as error:
            self._wake_armed = False
            self._stop_audio()
            self._session.transition(VoiceState.ERROR)
            self._emit("voice_error", code=self._error_code(error), phase="wake_rearm")

    def _new_vad(self, *, threshold: float | None = None) -> EnergyVAD:
        return EnergyVAD(
            sample_rate=self.config.sample_rate,
            frame_ms=self.config.frame_ms,
            speech_threshold=threshold or self.config.speech_start_rms,
            silence_end_seconds=self.config.silence_end_seconds,
            max_utterance_seconds=self.config.max_utterance_seconds,
            preroll_seconds=self.config.preroll_seconds,
        )

    def _append_ptt_segment(self, samples: np.ndarray) -> None:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        remaining = self._ptt_max_samples - self._ptt_sample_count
        if remaining <= 0:
            return
        bounded = audio[:remaining].copy()
        if bounded.size:
            self._ptt_segments.append(bounded)
            self._ptt_sample_count += bounded.size

    def _release_owned_local_brain(self) -> None:
        """Release only FRIDAY's retained local process before loading Piper."""
        brain_manager = getattr(self.core, "_brain_manager", None)
        local = getattr(brain_manager, "local", None)
        lifecycle = getattr(local, "lifecycle", None)
        close = getattr(lifecycle, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _release_voice_engines(self) -> None:
        try:
            self.stt.close()
        except Exception:
            pass
        try:
            self.tts.close()
        except Exception:
            pass

    def _on_core_status(self, _status: str) -> None:
        # BrainManager status strings are intentionally not copied into voice events.
        return

    def _emit(self, name: str, **data: object) -> None:
        session = self.session
        event = VoiceEvent(
            name=name, timestamp=datetime.now(timezone.utc),
            session_id=session.session_id if session is not None else None,
            data=data,
        )
        with self._lock:
            listeners = tuple(self._events)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                continue

    @staticmethod
    def _put_sentinel(target: queue.Queue[object]) -> None:
        try:
            target.put_nowait(END_OF_QUEUE)
        except queue.Full:
            try:
                target.get_nowait()
                target.put_nowait(END_OF_QUEUE)
            except queue.Empty:
                pass


def _resample(samples: np.ndarray, sample_rate: int, target_rate: int) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate <= 0:
        raise ValueError("Audio sample rate must be positive.")
    if sample_rate == target_rate or not audio.size:
        return audio
    size = max(1, round(audio.size * target_rate / sample_rate))
    return np.interp(
        np.arange(size, dtype=np.float64) * sample_rate / target_rate,
        np.arange(audio.size, dtype=np.float64), audio,
    ).astype(np.float32)


__all__ = ["VoiceManager"]
