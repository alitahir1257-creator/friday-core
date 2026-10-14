"""Local-only TTS routing with optional heavy engines kept unloaded."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
from typing import Callable

import numpy as np

from friday.voice.audio import AudioOutput
from friday.voice.language import SpeechLanguage, mixed_speech_segments
from friday.voice.models import VoiceConfig
from friday.voice.style import SpeechRenderRequest
from friday.voice.tts import PiperLocalTTS, SynthesizedAudio, _piper_length_scale


class VoxCPMTTS:
    """Optional official VoxCPM2 adapter; never loaded until explicitly selected."""

    engine = "voxcpm"

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.model_dir = Path(config.voxcpm_model_dir)
        self._model = None
        self._output = AudioOutput(device=config.output_device)
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._speaking = False
        self.last_error: str | None = None
        self.last_first_audio_latency_ms: float | None = None
        self._fallback: Callable[[SpeechRenderRequest], None] | None = None

    @property
    def available(self) -> bool:
        try:
            import voxcpm  # noqa: F401
        except ImportError:
            return False
        return self.model_dir.is_dir()

    def synthesize(self, request: SpeechRenderRequest):
        if not self.available:
            raise RuntimeError("VOXCPM_UNAVAILABLE")
        if request.language in {SpeechLanguage.URDU, SpeechLanguage.ROMAN_URDU, SpeechLanguage.MIXED}:
            raise RuntimeError("VOXCPM_URDU_UNVERIFIED")
        if self._model is None:
            from voxcpm import VoxCPM
            self._model = VoxCPM.from_pretrained(str(self.model_dir), device="cpu", load_denoiser=False)
        design = _voice_design(request.voice_profile, request.instruction)
        audio = np.asarray(self._model.generate(
            text=f"({design}){request.speech_text}", cfg_value=2.0,
            inference_timesteps=10, seed=42,
        ), dtype=np.float32).reshape(-1)
        if not audio.size or not np.isfinite(audio).all():
            raise RuntimeError("VOXCPM_INVALID_AUDIO")
        return audio

    def set_fallback(self, fallback: Callable[[SpeechRenderRequest], None]) -> None:
        self._fallback = fallback

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def speak_render(self, request: SpeechRenderRequest) -> None:
        if self._speaking:
            raise RuntimeError("TTS_BUSY")
        self._cancel = threading.Event()
        self._speaking = True
        self.last_error = None
        self.last_first_audio_latency_ms = None
        self._worker = threading.Thread(target=self._speak_worker, args=(request,), daemon=True)
        self._worker.start()

    def _speak_worker(self, request: SpeechRenderRequest) -> None:
        started = time.perf_counter()
        try:
            audio = np.asarray(self.synthesize(request), dtype=np.float32).reshape(-1)
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            if pcm.size and not self._cancel.is_set():
                self._output.play(
                    pcm, int(self._model.tts_model.sample_rate), self._cancel,
                    on_started=lambda: setattr(
                        self, "last_first_audio_latency_ms", round((time.perf_counter() - started) * 1000, 2),
                    ),
                )
        except Exception as error:
            self.last_error = type(error).__name__
            if not self._cancel.is_set() and self._fallback is not None:
                try:
                    self._fallback(request)
                except Exception:
                    self.last_error = "FallbackError"
        finally:
            self._speaking = False

    def stop(self) -> None:
        self._cancel.set()
        self._output.stop()

    def close(self) -> None:
        self.stop()
        self._model = None


class UrduPiperTTS:
    """Reviewed local Urdu Piper voices, loaded only for an Urdu utterance."""

    engine = "urdu_piper"

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.model_dir = Path(config.urdu_tts_model_dir)
        self._voices: dict[str, PiperLocalTTS] = {}
        self._active: PiperLocalTTS | None = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._speaking = False
        self.last_error: str | None = None
        self.last_first_audio_latency_ms: float | None = None
        self.last_render: SpeechRenderRequest | None = None
        self._fallback: Callable[[SpeechRenderRequest], None] | None = None

    @property
    def available(self) -> bool:
        return any(
            path.is_file() and Path(str(path) + ".json").is_file()
            for path in self._model_paths().values()
        )

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def speak_render(self, request: SpeechRenderRequest) -> None:
        self._start(request, english_tts=None)

    def speak_mixed_render(self, request: SpeechRenderRequest, english_tts: PiperLocalTTS) -> None:
        self._start(request, english_tts=english_tts)

    def set_fallback(self, fallback: Callable[[SpeechRenderRequest], None]) -> None:
        self._fallback = fallback

    def _start(self, request: SpeechRenderRequest, *, english_tts: PiperLocalTTS | None) -> None:
        if self._speaking:
            raise RuntimeError("TTS_BUSY")
        voice = self._voice_for(request.voice_profile)
        self._active = voice
        self._cancel = threading.Event()
        self._speaking = True
        self.last_error = None
        self.last_first_audio_latency_ms = None
        self.last_render = request
        self._worker = threading.Thread(
            target=self._speak_worker, args=(request, voice, english_tts, self._cancel),
            name="friday-urdu-piper", daemon=True,
        )
        self._worker.start()

    def _speak_worker(
        self, request: SpeechRenderRequest, voice: PiperLocalTTS,
        english_tts: PiperLocalTTS | None, cancel_event: threading.Event,
    ) -> None:
        started = time.perf_counter()
        try:
            scale = _piper_length_scale(request.pace, voice.config.tts_length_scale)
            if request.language is SpeechLanguage.MIXED and english_tts is not None:
                audio = self._synthesize_mixed(request, voice, english_tts, scale, cancel_event)
            else:
                audio = voice.synthesize(
                    request.speech_text, cancel_event=cancel_event, length_scale=scale,
                    sentence_silence=request.sentence_silence,
                )
            if not audio.samples.size and not cancel_event.is_set():
                raise RuntimeError("URDU_PIPER_INVALID_AUDIO")
            if audio.samples.size and not cancel_event.is_set():
                voice.output.play(
                    audio.samples, audio.sample_rate, cancel_event,
                    on_started=lambda: setattr(
                        self, "last_first_audio_latency_ms", round((time.perf_counter() - started) * 1000, 2),
                    ),
                )
        except Exception as error:
            self.last_error = type(error).__name__
            if not cancel_event.is_set() and self._fallback is not None:
                try:
                    self._fallback(request)
                except Exception:
                    self.last_error = "FallbackError"
        finally:
            self._speaking = False

    @staticmethod
    def _synthesize_mixed(
        request: SpeechRenderRequest, urdu_tts: PiperLocalTTS, english_tts: PiperLocalTTS,
        length_scale: float, cancel_event: threading.Event,
    ) -> SynthesizedAudio:
        parts: list[SynthesizedAudio] = []
        for language, text in mixed_speech_segments(request.text):
            if cancel_event.is_set():
                break
            engine = english_tts if language is SpeechLanguage.ENGLISH else urdu_tts
            parts.append(engine.synthesize(
                text, cancel_event=cancel_event, length_scale=length_scale,
                sentence_silence=request.sentence_silence,
            ))
        return _merge_audio(parts)

    def _voice_for(self, profile: str) -> PiperLocalTTS:
        key = "male" if profile in {"john", "friday_male", "male"} else "female"
        if key not in self._voices:
            path = self._model_paths()[key]
            self._voices[key] = PiperLocalTTS(replace(self.config, tts_model_path=path))
        return self._voices[key]

    def _model_paths(self) -> dict[str, Path]:
        return {
            "male": self.model_dir / "ur_PK-fasih-medium.onnx",
            "female": self.model_dir / "ur_PK-aegis_female-medium.onnx",
        }

    def stop(self) -> None:
        self._cancel.set()
        if self._active is not None:
            self._active.stop()

    def close(self) -> None:
        self.stop()
        for voice in self._voices.values():
            voice.close()
        self._voices.clear()
        self._active = None


def _merge_audio(parts: list[SynthesizedAudio]) -> SynthesizedAudio:
    """Safely join mixed-language audio at one sample rate with short transitions."""
    usable = [part for part in parts if part.samples.size]
    if not usable:
        return SynthesizedAudio(np.empty(0, dtype=np.int16), 22_050)
    rate = usable[0].sample_rate
    normalized: list[np.ndarray] = []
    silence = np.zeros(max(1, round(rate * 0.06)), dtype=np.int16)
    for index, part in enumerate(usable):
        samples = np.asarray(part.samples, dtype=np.int16)
        if part.sample_rate != rate:
            positions = np.linspace(0, max(0, samples.size - 1), max(1, round(samples.size * rate / part.sample_rate)))
            samples = np.interp(positions, np.arange(samples.size), samples).astype(np.int16)
        peak = int(np.max(np.abs(samples.astype(np.int32)))) if samples.size else 0
        if peak:
            samples = np.rint(samples.astype(np.float32) * (26_214 / peak)).astype(np.int16)
        if index:
            normalized.append(silence)
        normalized.append(samples)
    return SynthesizedAudio(np.concatenate(normalized), rate)


class TTSRouter:
    """Piper remains the guaranteed local fallback; optional engines are lazy."""

    engine = "piper"

    def __init__(self, config: VoiceConfig, *, piper: PiperLocalTTS | None = None,
                 voxcpm_factory: Callable[[VoiceConfig], VoxCPMTTS] = VoxCPMTTS,
                 urdu_factory: Callable[[VoiceConfig], UrduPiperTTS] = UrduPiperTTS) -> None:
        self.config = config
        self.piper = piper or PiperLocalTTS(config)
        self._voxcpm_factory = voxcpm_factory
        self._urdu_factory = urdu_factory
        self._voxcpm: VoxCPMTTS | None = None
        self._urdu: UrduPiperTTS | None = None
        self._active = self.piper
        self.last_route = "piper"
        self.last_request: SpeechRenderRequest | None = None

    @property
    def available(self) -> bool:
        return self.piper.available

    @property
    def is_speaking(self) -> bool:
        return self._active.is_speaking

    @property
    def voice_name(self) -> str:
        return self.piper.voice_name

    @property
    def last_error(self):
        return getattr(self._active, "last_error", None)

    @property
    def last_first_audio_latency_ms(self):
        return getattr(self._active, "last_first_audio_latency_ms", None)

    @property
    def last_generation_duration_ms(self):
        return getattr(self._active, "last_generation_duration_ms", None)

    @property
    def last_audio_callback_span_ms(self):
        return getattr(self._active, "last_audio_callback_span_ms", None)

    @property
    def last_playback_duration_ms(self):
        return getattr(self._active, "last_playback_duration_ms", None)

    @property
    def last_output_drain_ms(self):
        return getattr(self._active, "last_output_drain_ms", None)

    @property
    def last_output_close_ms(self):
        return getattr(self._active, "last_output_close_ms", None)

    def speak(self, text: str) -> None:
        self._active = self.piper
        self.last_route = "piper"
        self.piper.speak(text)

    def speak_render(self, request: SpeechRenderRequest) -> None:
        self.last_request = request
        if self._should_try_urdu(request):
            engine = self._urdu or self._urdu_factory(self.config)
            self._urdu = engine
            self._set_fallback(engine)
            if engine.available:
                try:
                    self._active = engine
                    self.last_route = "urdu_piper"
                    if request.language is SpeechLanguage.MIXED:
                        engine.speak_mixed_render(request, self.piper)
                    else:
                        engine.speak_render(request)
                    return
                except Exception:
                    self._active = self.piper
        if self._should_try_voxcpm(request):
            engine = self._voxcpm or self._voxcpm_factory(self.config)
            self._voxcpm = engine
            self._set_fallback(engine)
            if engine.available:
                try:
                    self._active = engine
                    self.last_route = "voxcpm"
                    engine.speak_render(request)
                    return
                except Exception:
                    self._active = self.piper
        self._fallback_to_piper(request)

    def _set_fallback(self, engine: object) -> None:
        fallback = getattr(engine, "set_fallback", None)
        if callable(fallback):
            fallback(self._fallback_to_piper)

    def _fallback_to_piper(self, request: SpeechRenderRequest) -> None:
        self._active = self.piper
        self.last_route = "piper"
        render = getattr(self.piper, "speak_render", None)
        if callable(render):
            render(request)
        else:
            self.piper.speak(request.speech_text)

    def _should_try_urdu(self, request: SpeechRenderRequest) -> bool:
        return request.language in {
            SpeechLanguage.URDU, SpeechLanguage.ROMAN_URDU, SpeechLanguage.MIXED,
        }

    def _should_try_voxcpm(self, request: SpeechRenderRequest) -> bool:
        return (
            self.config.voice_engine in {"auto", "voxcpm"}
            and self.config.voice_quality in {"auto", "high_quality"}
            and request.language is SpeechLanguage.ENGLISH
        )

    def stop(self) -> None:
        self._active.stop()

    def close(self) -> None:
        self.piper.close()
        if self._voxcpm is not None:
            self._voxcpm.close()
        if self._urdu is not None:
            self._urdu.close()


def _voice_design(profile: str, instruction: str) -> str:
    base = "professional, clear, warm, natural, not theatrical"
    gender = "male" if profile == "john" else "female"
    return f"{base}, {gender} assistant, {instruction}"


__all__ = ["TTSRouter", "UrduPiperTTS", "VoxCPMTTS"]
