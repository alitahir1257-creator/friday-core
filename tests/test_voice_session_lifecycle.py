from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from friday.voice import audio as voice_audio
from friday.voice.audio import AudioInput, AudioOutput, AudioUnavailable
from friday.voice.manager import VoiceManager
from friday.voice.models import AudioDevice, DeviceCatalog, VoiceConfig, VoiceMode, VoiceState
from friday.voice.tts import PiperLocalTTS, SynthesizedAudio


class _FakeSTT:
    engine = "test"
    available = True

    def close(self):
        pass


class _FakeTTS:
    engine = "piper"
    available = True
    is_speaking = True
    last_error = None
    last_first_audio_latency_ms = None

    def stop(self):
        self.is_speaking = False

    def close(self):
        pass


class _FakeAudioInput:
    device = None
    device_name = "Realtek microphone"

    def __init__(self):
        self.active = False
        self.starts = 0
        self.stops = 0

    def start(self, _callback):
        self.starts += 1
        self.active = True

    def stop(self):
        self.stops += 1
        self.active = False


def _catalog():
    mic = AudioDevice(1, "Realtek microphone", 1, 16_000, "WASAPI")
    speaker = AudioDevice(2, "Realtek speakers", 2, 48_000, "WASAPI")
    return DeviceCatalog((mic,), (speaker,), mic, speaker, True)


def test_playback_stream_stays_abortable_until_closed(monkeypatch):
    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    streams = []

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.active = True
            self.output = output
            self.registered_during_stop = False
            self.aborted = 0
            self.closed = False
            streams.append(self)

        def start(self):
            for _ in range(10):
                try:
                    self.callback(np.empty((4, 1), dtype=np.int16), 4, None, None)
                except CallbackStop:
                    break

        def stop(self):
            self.registered_during_stop = self.output._stream is self
            self.output.stop()

        def abort(self):
            self.aborted += 1

        def close(self):
            self.active = False
            self.closed = True

    sd = SimpleNamespace(
        CallbackStop=CallbackStop, CallbackAbort=CallbackAbort,
        OutputStream=lambda **kwargs: Stream(**kwargs),
    )
    monkeypatch.setattr(voice_audio, "_sounddevice", lambda: sd)
    monkeypatch.setattr(voice_audio, "discover_devices", lambda **_kwargs: _catalog())
    output = AudioOutput()
    output.play(np.array([10, 20, 30, 40], dtype=np.int16), 16_000, threading.Event())

    assert output._stream is None
    assert streams[0].registered_during_stop
    assert streams[0].aborted == 1
    assert streams[0].closed
    assert output.last_audio_callback_span_ms is not None


def test_microphone_start_failure_closes_partial_stream(monkeypatch):
    class Stream:
        closed = False

        def start(self):
            raise RuntimeError("MICROPHONE_START_FAILED")

        def close(self):
            self.closed = True

    stream = Stream()
    sd = SimpleNamespace(InputStream=lambda **_kwargs: stream)
    monkeypatch.setattr(voice_audio, "_sounddevice", lambda: sd)
    monkeypatch.setattr(voice_audio, "discover_devices", lambda **_kwargs: _catalog())
    microphone = AudioInput()

    with pytest.raises(RuntimeError, match="MICROPHONE_START_FAILED"):
        microphone.start(lambda *_args: None)

    assert stream.closed
    assert microphone._stream is None


def test_stalled_output_drain_aborts_and_fails(monkeypatch):
    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.active = True
            self.aborted = threading.Event()
            self.closed = False

        def start(self):
            for _ in range(10):
                try:
                    self.callback(np.empty((4, 1), dtype=np.int16), 4, None, None)
                except CallbackStop:
                    break

        def stop(self):
            self.aborted.wait(1)

        def abort(self):
            self.aborted.set()

        def close(self):
            self.closed = True
            self.active = False

    stream = None

    def create_stream(**kwargs):
        nonlocal stream
        stream = Stream(**kwargs)
        return stream

    sd = SimpleNamespace(
        CallbackStop=CallbackStop, CallbackAbort=CallbackAbort, OutputStream=create_stream,
    )
    monkeypatch.setattr(voice_audio, "_sounddevice", lambda: sd)
    monkeypatch.setattr(voice_audio, "discover_devices", lambda **_kwargs: _catalog())
    monkeypatch.setattr(voice_audio, "_output_completion_timeout_seconds", lambda _duration: 0.02)
    output = AudioOutput()

    with pytest.raises(AudioUnavailable, match="AUDIO_OUTPUT_TIMEOUT"):
        output.play(np.array([10, 20, 30, 40], dtype=np.int16), 16_000, threading.Event())

    assert stream is not None and stream.aborted.is_set() and stream.closed
    assert output._stream is None


def test_piper_worker_clears_busy_and_worker_on_output_error():
    class Output:
        last_audio_callback_span_ms = 12.0
        last_playback_duration_ms = 18.0
        last_output_drain_ms = 2.0
        last_output_close_ms = 1.0

        def play(self, *_args, **_kwargs):
            self.last_audio_callback_span_ms = 12.0
            self.last_playback_duration_ms = 18.0
            self.last_output_drain_ms = 2.0
            self.last_output_close_ms = 1.0
            raise RuntimeError("AUDIO_OUTPUT_FAILED")

        def stop(self):
            pass

    output = Output()
    tts = PiperLocalTTS(VoiceConfig(), output=output)
    tts.synthesize = lambda *_args, **_kwargs: SynthesizedAudio(np.ones(16, dtype=np.int16), 16_000)
    tts._start_speaking("hello", length_scale=1.0, sentence_silence=0.0, request=None)
    worker = tts._worker
    assert worker is not None
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert tts.is_speaking is False
    assert tts._worker is None
    assert tts.last_error == "RuntimeError"
    assert tts.last_generation_duration_ms is not None
    assert tts.last_output_close_ms == 1.0


def test_ptt_during_speaking_reopens_inactive_microphone():
    audio = _FakeAudioInput()
    tts = _FakeTTS()
    manager = VoiceManager(
        SimpleNamespace(), VoiceConfig(), audio_input=audio, stt=_FakeSTT(), tts=tts,
        wake_detector=SimpleNamespace(close=lambda: None), device_discovery=_catalog,
    )
    try:
        manager._begin_session(VoiceMode.PTT, "public")
        manager._session.transition(VoiceState.SPEAKING)

        assert manager.start_ptt() == VoiceState.LISTENING.value
        assert audio.starts == 1
        assert manager.microphone_active
        assert manager.state == VoiceState.LISTENING
    finally:
        manager.close()


def test_ptt_reacquisition_failure_releases_stale_session():
    class FailingAudioInput(_FakeAudioInput):
        def start(self, _callback):
            raise RuntimeError("MICROPHONE_UNAVAILABLE")

    manager = VoiceManager(
        SimpleNamespace(), VoiceConfig(), audio_input=FailingAudioInput(), stt=_FakeSTT(),
        tts=_FakeTTS(), wake_detector=SimpleNamespace(close=lambda: None), device_discovery=_catalog,
    )
    try:
        manager._begin_session(VoiceMode.PTT, "public")
        manager._session.transition(VoiceState.SPEAKING)

        assert manager.start_ptt() == "VOICE_UNAVAILABLE"
        assert manager.session is None
        assert manager.state == VoiceState.ERROR
    finally:
        manager.close()


def test_tts_exception_returns_voice_session_to_idle():
    class FailingTTS(_FakeTTS):
        is_speaking = False

        def speak_render(self, _request):
            raise RuntimeError("AUDIO_OUTPUT_FAILED")

    events = []
    manager = VoiceManager(
        SimpleNamespace(), VoiceConfig(), audio_input=_FakeAudioInput(), stt=_FakeSTT(),
        tts=FailingTTS(), wake_detector=SimpleNamespace(close=lambda: None),
        device_discovery=_catalog, event_listener=events.append,
    )
    try:
        manager._begin_session(VoiceMode.PTT, "public")
        manager._speak_answer("A short response")

        assert manager.state == VoiceState.IDLE
        assert manager.session is None
        assert any(event.name == "speaking_stopped" and event.data["reason"] == "failed" for event in events)
        assert any(event.name == "voice_idle" for event in events)
    finally:
        manager.close()
