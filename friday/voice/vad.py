"""Lightweight RMS VAD with bounded pre-roll and utterance buffers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class VADResult:
    speech: bool
    speech_started: bool = False
    speech_ended: bool = False
    utterance: object | None = None
    rms: float = 0.0


class EnergyVAD:
    """A conservative energy gate for short, bounded utterances."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        frame_ms: int = 100,
        speech_threshold: float = 0.015,
        silence_end_seconds: float = 0.8,
        max_utterance_seconds: float = 20.0,
        preroll_seconds: float = 0.2,
        start_frames: int = 2,
        min_speech_seconds: float = 0.12,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.speech_threshold = speech_threshold
        self.silence_end_seconds = silence_end_seconds
        self.max_utterance_seconds = max_utterance_seconds
        self.start_frames = max(1, start_frames)
        self.min_speech_seconds = min_speech_seconds
        self._preroll = deque(maxlen=max(1, math.ceil(preroll_seconds * 1000 / frame_ms)))
        self._chunks: list[object] = []
        self._speech_frames = 0
        self._quiet_frames = 0
        self._active = False
        self._started_at = 0.0
        self._max_frames = max(1, math.ceil(max_utterance_seconds * 1000 / frame_ms))
        self._silence_frames = max(1, math.ceil(silence_end_seconds * 1000 / frame_ms))

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, frame, *, threshold: float | None = None) -> VADResult:
        import numpy as np

        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return VADResult(False)
        rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
        limit = self.speech_threshold if threshold is None else threshold
        speech = rms >= limit
        now = time.monotonic()
        started = False

        if not self._active:
            self._preroll.append(samples.copy())
            self._speech_frames = self._speech_frames + 1 if speech else 0
            if self._speech_frames < self.start_frames:
                return VADResult(speech, rms=rms)
            self._active = True
            started = True
            self._started_at = now
            self._chunks = [item.copy() for item in self._preroll]
            self._quiet_frames = 0
            self._preroll.clear()
        else:
            if len(self._chunks) < self._max_frames:
                self._chunks.append(samples.copy())
            self._quiet_frames = 0 if speech else self._quiet_frames + 1

        maxed = len(self._chunks) >= self._max_frames or now - self._started_at >= self.max_utterance_seconds
        ended = maxed or (self._active and self._quiet_frames >= self._silence_frames)
        if not ended:
            return VADResult(speech, speech_started=started, rms=rms)
        utterance = self._finish()
        duration = len(utterance) / self.sample_rate * 1000
        if duration < self.min_speech_seconds * 1000:
            utterance = None
        return VADResult(speech, speech_started=started, speech_ended=True, utterance=utterance, rms=rms)

    def flush(self):
        """Return an active utterance when PTT is released."""
        if not self._active:
            return None
        utterance = self._finish()
        if len(utterance) < self.sample_rate * self.min_speech_seconds:
            return None
        return utterance

    def snapshot(self):
        """Return the bounded recent audio without changing detector state."""
        import numpy as np

        chunks = self._chunks if self._active else list(self._preroll)
        if not chunks:
            return None
        return np.concatenate(chunks).astype(np.float32, copy=True)

    def reset(self) -> None:
        self._preroll.clear()
        self._chunks.clear()
        self._speech_frames = 0
        self._quiet_frames = 0
        self._active = False
        self._started_at = 0.0

    def _finish(self):
        import numpy as np

        utterance = np.concatenate(self._chunks) if self._chunks else np.empty(0, dtype=np.float32)
        self.reset()
        return utterance


__all__ = ["EnergyVAD", "VADResult"]
