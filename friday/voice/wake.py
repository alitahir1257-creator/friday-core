"""Local Sherpa-ONNX keyword spotting with a replaceable configured phrase."""

from __future__ import annotations

import gc
import hashlib
from pathlib import Path
import time

import numpy as np

from friday.voice.models import WAKE_MODEL_DIR, VoiceConfig


class SherpaWakeWord:
    engine = "sherpa-onnx-kws"

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or VoiceConfig()
        self.model_dir = Path(self.config.wake_model_dir)
        self._spotter = None
        self._stream = None
        self.last_detection_ms: float | None = None
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        model_files = (
            "tokens.txt", "bpe.model",
            "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        )
        return all((self.model_dir / name).is_file() for name in model_files)

    def _keywords_path(self) -> Path:
        configured_path = self.config.wake_keywords_file
        if configured_path is not None and Path(configured_path).is_file():
            return Path(configured_path)
        phrase = " ".join(self.config.wake_phrase.split()).upper()
        if not phrase or len(phrase) > 80:
            raise RuntimeError("WAKE_PHRASE_INVALID")
        digest = hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:12]
        target = self.model_dir / f"wake-keywords-{digest}.txt"
        if target.is_file():
            return target
        try:
            import sentencepiece as spm
        except ImportError as error:
            raise RuntimeError("SENTENCEPIECE_INSTALL_REQUIRED") from error
        tokens_path = self.model_dir / "tokens.txt"
        bpe_path = self.model_dir / "bpe.model"
        if not tokens_path.is_file() or not bpe_path.is_file():
            raise RuntimeError("WAKE_TOKENIZER_MISSING")
        token_table = {
            line.split()[0]
            for line in tokens_path.read_text(encoding="utf-8").splitlines()
            if len(line.split()) == 2
        }
        pieces = spm.SentencePieceProcessor(model_file=str(bpe_path)).encode(phrase, out_type=str)
        if not pieces or any(piece not in token_table for piece in pieces):
            raise RuntimeError("WAKE_PHRASE_UNSUPPORTED_BY_MODEL")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(" ".join(pieces) + "\n", encoding="utf-8")
        return target

    def load(self) -> None:
        if self._spotter is not None:
            return
        if not self.available:
            raise RuntimeError("WAKE_MODEL_MISSING")
        try:
            import sherpa_onnx
        except ImportError as error:
            raise RuntimeError("STT_INSTALL_REQUIRED") from error
        keyword_path = self._keywords_path()
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(self.model_dir / "tokens.txt"),
            encoder=str(self.model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(self.model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(self.model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            num_threads=1,
            sample_rate=self.config.sample_rate,
            feature_dim=80,
            keywords_file=str(keyword_path),
            keywords_score=1.0,
            keywords_threshold=self.config.wake_threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

    def detect(self, frame: np.ndarray, sample_rate: int = 16_000) -> str | None:
        if sample_rate != self.config.sample_rate:
            frame = _resample(frame, sample_rate, self.config.sample_rate)
        self.load()
        started = time.perf_counter()
        self._stream.accept_waveform(self.config.sample_rate, np.asarray(frame, dtype=np.float32))
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        self.last_detection_ms = round((time.perf_counter() - started) * 1000, 2)
        keyword = str(getattr(result, "keyword", "") or "").strip()
        if not keyword:
            return None
        self._spotter.reset_stream(self._stream)
        return keyword

    def close(self) -> None:
        self._stream = None
        self._spotter = None
        gc.collect()


def _resample(samples: np.ndarray, sample_rate: int, target_rate: int) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate == target_rate or not audio.size:
        return audio
    size = max(1, round(audio.size * target_rate / sample_rate))
    return np.interp(
        np.arange(size, dtype=np.float64) * sample_rate / target_rate,
        np.arange(audio.size, dtype=np.float64), audio,
    ).astype(np.float32)


__all__ = ["SherpaWakeWord"]
