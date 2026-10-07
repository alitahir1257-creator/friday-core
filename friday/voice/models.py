"""Data contracts and bounded configuration for local voice sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import uuid


VOICE_MODELS_DIR = Path(r"D:\FRIDAY\models\voice")
VOICE_CACHE_DIR = Path(r"D:\FRIDAY\data\cache\voice")
STT_MODEL_DIR = VOICE_MODELS_DIR / "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
WAKE_MODEL_DIR = VOICE_MODELS_DIR / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
DEFAULT_TTS_MODEL = VOICE_MODELS_DIR / "en_US-lessac-medium.onnx"


class VoiceState(str, Enum):
    IDLE = "IDLE"
    WAKE_LISTENING = "WAKE_LISTENING"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ENDING = "ENDING"
    ERROR = "ERROR"


class VoiceMode(str, Enum):
    WAKE = "wake"
    PTT = "ptt"


@dataclass(frozen=True)
class VoiceConfig:
    """Small, local-only defaults; models are never downloaded automatically."""

    sample_rate: int = 16_000
    frame_ms: int = 100
    speech_start_rms: float = 0.015
    barge_in_rms: float = 0.045
    silence_end_seconds: float = 0.8
    max_utterance_seconds: float = 20.0
    preroll_seconds: float = 0.2
    continuous_timeout_seconds: float = 30.0
    post_tts_cooldown_seconds: float = 0.25
    assistant_name: str = "Friday"
    wake_phrase: str = "Friday"
    wake_threshold: float = 0.25
    wake_keywords_file: Path | None = None
    stt_model_dir: Path = STT_MODEL_DIR
    wake_model_dir: Path = WAKE_MODEL_DIR
    tts_model_path: Path = DEFAULT_TTS_MODEL
    input_device: int | None = None
    output_device: int | None = None
    max_transcript_chars: int = 2_000
    max_tts_chars: int = 2_000
    max_tts_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.frame_ms <= 0:
            raise ValueError("Voice sample rate and frame duration must be positive.")
        if self.max_utterance_seconds <= 0 or self.continuous_timeout_seconds <= 0:
            raise ValueError("Voice duration limits must be positive.")
        if not 0 < self.wake_threshold <= 1:
            raise ValueError("Wake threshold must be between 0 and 1.")
        if self.max_transcript_chars < 1 or self.max_tts_chars < 1:
            raise ValueError("Voice text limits must be positive.")


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_channels: int
    sample_rate: int


@dataclass(frozen=True)
class DeviceCatalog:
    inputs: tuple[AudioDevice, ...] = ()
    outputs: tuple[AudioDevice, ...] = ()
    default_input: AudioDevice | None = None
    default_output: AudioDevice | None = None
    available: bool = False
    error_code: str | None = None


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    confidence: float | None
    language: str | None
    duration_ms: float
    audio_duration_ms: float
    engine: str
    is_final: bool = True


@dataclass(frozen=True)
class VoiceEvent:
    name: str
    timestamp: datetime
    session_id: str | None
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceStatus:
    mode: str
    state: str
    stt_engine: str
    tts_engine: str
    voice_name: str
    mic_device: str | None
    session_duration_seconds: float
    turn_count: int
    last_latency: dict[str, float]
    available: bool
    availability_code: str


@dataclass
class ConversationSession:
    mode: VoiceMode
    timeout_seconds: float
    privacy: str = "public"
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = 0
    state: VoiceState = VoiceState.IDLE
    current_transcript: str = ""
    speaking: bool = False

    def touch(self) -> None:
        self.last_activity_at = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.started_at).total_seconds())


__all__ = [
    "AudioDevice", "ConversationSession", "DEFAULT_TTS_MODEL", "DeviceCatalog",
    "STT_MODEL_DIR", "TranscriptResult", "VOICE_CACHE_DIR", "VOICE_MODELS_DIR",
    "VoiceConfig", "VoiceEvent", "VoiceMode", "VoiceState", "VoiceStatus",
    "WAKE_MODEL_DIR",
]
