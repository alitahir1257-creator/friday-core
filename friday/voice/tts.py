"""Local Piper speech synthesis with one bounded, interruptible output at a time."""

from __future__ import annotations

from dataclasses import dataclass
import gc
from pathlib import Path
import threading
import time
from typing import Protocol

import numpy as np

from friday.voice.audio import AudioOutput
from friday.voice.emotion import PIPER_CAPABILITIES
from friday.voice.models import DEFAULT_TTS_MODEL, VoiceConfig
from friday.voice.style import SpeechRenderRequest


@dataclass(frozen=True)
class SynthesizedAudio:
    samples: np.ndarray
    sample_rate: int


class LocalTTS(Protocol):
    engine: str

    @property
    def available(self) -> bool: ...

    @property
    def is_speaking(self) -> bool: ...

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


def installed_voices(voice_dir: Path) -> tuple[str, ...]:
    """Return model-backed voices found locally; never fetch voice metadata online."""
    return tuple(sorted(
        path.stem for path in Path(voice_dir).glob("*.onnx")
        if Path(str(path) + ".json").is_file()
    ))


class PiperLocalTTS:
    engine = "piper"
    capabilities = PIPER_CAPABILITIES

    def __init__(
        self,
        config: VoiceConfig | None = None,
        *,
        output: AudioOutput | None = None,
    ) -> None:
        self.config = config or VoiceConfig()
        self.model_path = Path(self.config.tts_model_path)
        self.output = output or AudioOutput(device=self.config.output_device)
        self._voice = None
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._playing = False
        self.last_error: str | None = None
        self.last_first_audio_latency_ms: float | None = None
        self.last_render: SpeechRenderRequest | None = None
        self.last_length_scale: float = float(self.config.tts_length_scale)

    @property
    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return self.model_path.is_file() and Path(str(self.model_path) + ".json").is_file()

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._playing

    @property
    def voice_name(self) -> str:
        return self.model_path.stem

    def load(self) -> None:
        if self._voice is not None:
            return
        if not self.model_path.is_file() or not Path(str(self.model_path) + ".json").is_file():
            raise RuntimeError("TTS_MODEL_MISSING")
        try:
            from piper import PiperVoice
        except ImportError as error:
            raise RuntimeError("TTS_INSTALL_REQUIRED") from error
        self._voice = PiperVoice.load(str(self.model_path))

    def synthesize(
        self, text: str, *, cancel_event: threading.Event | None = None,
        length_scale: float | None = None, sentence_silence: float = 0.0,
    ) -> SynthesizedAudio:
        clean_text = " ".join(str(text).split())
        if not clean_text:
            return SynthesizedAudio(np.empty(0, dtype=np.int16), 22_050)
        if len(clean_text) > self.config.max_tts_chars:
            clean_text = clean_text[: self.config.max_tts_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
        if cancel_event is not None and cancel_event.is_set():
            return SynthesizedAudio(np.empty(0, dtype=np.int16), 22_050)
        self.load()
        sample_rate = int(getattr(getattr(self._voice, "config", None), "sample_rate", 22_050))
        chunks: list[np.ndarray] = []
        sample_count = 0
        effective_length_scale = float(self.config.tts_length_scale if length_scale is None else length_scale)
        from piper import SynthesisConfig
        chunks_iter = self._voice.synthesize(
            clean_text,
            syn_config=SynthesisConfig(
                length_scale=effective_length_scale, normalize_audio=True, volume=0.85,
            ),
        )
        silence_samples = round(sample_rate * min(0.15, max(0.0, float(sentence_silence))))
        for chunk in chunks_iter:
            if cancel_event is not None and cancel_event.is_set():
                break
            if chunks and silence_samples:
                sample_count += silence_samples
                chunks.append(np.zeros(silence_samples, dtype=np.int16))
            pcm = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).copy()
            sample_count += pcm.size
            if sample_count > self.config.max_tts_seconds * sample_rate:
                raise RuntimeError("TTS_OUTPUT_LIMIT_EXCEEDED")
            chunks.append(pcm)
        audio = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int16)
        return SynthesizedAudio(audio, sample_rate)

    def speak(self, text: str) -> None:
        self._start_speaking(
            text, length_scale=float(self.config.tts_length_scale), sentence_silence=0.0, request=None,
        )

    def speak_render(self, request: SpeechRenderRequest) -> None:
        """Render a bounded presentation style through Piper prosody controls.

        Piper has no learned emotion control.  This deliberately changes only
        safe timing/prosody and records the render contract for validation.
        """
        self._start_speaking(
            request.speech_text,
            length_scale=_piper_length_scale(request.pace, self.config.tts_length_scale),
            sentence_silence=request.sentence_silence,
            request=request,
        )

    def _start_speaking(
        self, text: str, *, length_scale: float, sentence_silence: float,
        request: SpeechRenderRequest | None,
    ) -> None:
        clean_text = " ".join(str(text).split())
        if not clean_text:
            return
        with self._lock:
            if self._playing:
                raise RuntimeError("TTS_BUSY")
            self._cancel = threading.Event()
            self._playing = True
            self.last_error = None
            self.last_first_audio_latency_ms = None
            self.last_render = request
            self.last_length_scale = length_scale
            self._worker = threading.Thread(
                target=self._speak_worker,
                args=(clean_text, self._cancel, length_scale, sentence_silence),
                name="friday-local-tts", daemon=True,
            )
            self._worker.start()

    def _speak_worker(
        self, text: str, cancel_event: threading.Event, length_scale: float,
        sentence_silence: float,
    ) -> None:
        started = time.perf_counter()
        try:
            audio = self.synthesize(
                text, cancel_event=cancel_event, length_scale=length_scale,
                sentence_silence=sentence_silence,
            )
            if audio.samples.size and not cancel_event.is_set():
                def on_started() -> None:
                    self.last_first_audio_latency_ms = round((time.perf_counter() - started) * 1000, 2)

                self.output.play(audio.samples, audio.sample_rate, cancel_event, on_started=on_started)
        except Exception as error:
            self.last_error = type(error).__name__
        finally:
            with self._lock:
                self._playing = False

    def stop(self) -> None:
        with self._lock:
            cancel = self._cancel
            playing = self._playing
        if playing:
            cancel.set()
            self.output.stop()

    def close(self) -> None:
        self.stop()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._voice = None
        gc.collect()


def _piper_length_scale(pace: float, configured_scale: float) -> float:
    """Convert bounded speech pace to Piper's inverse length-scale control."""
    normalized_pace = min(1.12, max(0.88, float(pace)))
    return round(min(1.4, max(0.7, float(configured_scale) / normalized_pace)), 3)


__all__ = ["LocalTTS", "PiperLocalTTS", "SynthesizedAudio", "_piper_length_scale", "installed_voices"]
