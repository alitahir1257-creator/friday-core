"""Local speech-to-text adapters. The default engine is Sherpa-ONNX CPU."""

from __future__ import annotations

import gc
from pathlib import Path
import time
from typing import Protocol

import numpy as np

from friday.voice.models import STT_MODEL_DIR, TranscriptResult, VoiceConfig


class LocalSTT(Protocol):
    engine: str

    @property
    def available(self) -> bool: ...

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16_000) -> TranscriptResult: ...

    def close(self) -> None: ...


class SherpaOnnxSTT:
    """Lazy streaming Zipformer recognizer; reports no invented confidence/language."""

    engine = "sherpa-onnx"

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or VoiceConfig()
        self.model_dir = Path(self.config.stt_model_dir)
        self._recognizer = None

    @property
    def available(self) -> bool:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return all((self.model_dir / name).is_file() for name in self._model_files())

    @staticmethod
    def _model_files() -> tuple[str, ...]:
        return (
            "tokens.txt",
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.int8.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
        )

    def load(self) -> None:
        if self._recognizer is not None:
            return
        missing = [name for name in self._model_files() if not (self.model_dir / name).is_file()]
        if missing:
            raise RuntimeError("STT_MODEL_MISSING")
        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError("STT_INSTALL_REQUIRED") from error
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self.model_dir / "tokens.txt"),
            encoder=str(self.model_dir / "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=str(self.model_dir / "decoder-epoch-99-avg-1.int8.onnx"),
            joiner=str(self.model_dir / "joiner-epoch-99-avg-1.int8.onnx"),
            num_threads=1,
            sample_rate=self.config.sample_rate,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,
        )

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16_000) -> TranscriptResult:
        audio = _resample(samples, sample_rate, self.config.sample_rate)
        if audio.size == 0:
            return TranscriptResult("", None, None, 0.0, 0.0, self.engine)
        started = time.perf_counter()
        self.load()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.config.sample_rate, audio)
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        result = self._recognizer.get_result(stream)
        text = str(getattr(result, "text", result) or "").strip()
        return TranscriptResult(
            text=text[: self.config.max_transcript_chars],
            confidence=None,
            language=None,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            audio_duration_ms=round(audio.size / self.config.sample_rate * 1000, 2),
            engine=self.engine,
            is_final=True,
        )

    def close(self) -> None:
        self._recognizer = None
        gc.collect()


def _resample(samples: np.ndarray, sample_rate: int, target_rate: int) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate <= 0:
        raise ValueError("Audio sample rate must be positive.")
    if sample_rate == target_rate or audio.size == 0:
        return audio
    output_size = max(1, round(audio.size * target_rate / sample_rate))
    source_x = np.arange(audio.size, dtype=np.float64)
    target_x = np.arange(output_size, dtype=np.float64) * sample_rate / target_rate
    return np.interp(target_x, source_x, audio).astype(np.float32)


__all__ = ["LocalSTT", "SherpaOnnxSTT"]
