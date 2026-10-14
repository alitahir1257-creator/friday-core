"""Data contracts and bounded configuration for local voice sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from pathlib import Path
import uuid


VOICE_MODELS_DIR = Path(r"D:\FRIDAY\models\voice")
VOICE_CACHE_DIR = Path(r"D:\FRIDAY\data\cache\voice")
STT_MODEL_DIR = VOICE_MODELS_DIR / "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
WAKE_MODEL_DIR = VOICE_MODELS_DIR / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
DEFAULT_TTS_MODEL = VOICE_MODELS_DIR / "en_US-lessac-medium.onnx"
WHISPER_CPP_EXECUTABLE = Path(r"D:\FRIDAY\runtime\voice\whisper.cpp\Release\whisper-cli.exe")
WHISPER_CPP_MODEL = Path(r"D:\FRIDAY\models\voice\whisper\ggml-tiny-q5_1.bin")
GEMINI_STT_CONFIG_PATH = Path(r"D:\FRIDAY\config\voice-stt.json")
VOXCPM_MODEL_DIR = VOICE_MODELS_DIR / "voxcpm"
URDU_TTS_MODEL_DIR = VOICE_MODELS_DIR / "urdu"


class VoiceState(str, Enum):
    IDLE = "IDLE"
    WAKE_LISTENING = "WAKE_LISTENING"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ENDING = "ENDING"
    ERROR = "ERROR"


class VoiceMode(str, Enum):
    WAKE = "wake"
    PTT = "ptt"


SPEAKING_SPEED_SCALES = {"slow": 1.15, "normal": 1.0, "fast": 0.85}

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
    tts_length_scale: float = 1.0
    input_device: int | None = None
    output_device: int | None = None
    max_transcript_chars: int = 2_000
    max_tts_chars: int = 2_000
    max_tts_seconds: float = 90.0
    # Local providers remain explicit debug-only choices; Gemini is the normal path.
    conversational_stt: str = "gemini"
    whisper_cpp_executable: Path = WHISPER_CPP_EXECUTABLE
    whisper_cpp_model: Path = WHISPER_CPP_MODEL
    gemini_stt_config_path: Path = GEMINI_STT_CONFIG_PATH
    stt_language_hint: str = "auto"
    stt_threads: int = 8
    stt_timeout_seconds: float = 60.0
    gemini_timeout_seconds: float = 15.0
    voice_engine: str = "auto"
    voice_quality: str = "auto"
    emotion_mode: str = "auto"
    voxcpm_model_dir: Path = VOXCPM_MODEL_DIR
    urdu_tts_model_dir: Path = URDU_TTS_MODEL_DIR

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.frame_ms <= 0:
            raise ValueError("Voice sample rate and frame duration must be positive.")
        if self.max_utterance_seconds <= 0 or self.continuous_timeout_seconds <= 0:
            raise ValueError("Voice duration limits must be positive.")
        if not 0 < self.wake_threshold <= 1:
            raise ValueError("Wake threshold must be between 0 and 1.")
        if self.max_transcript_chars < 1 or self.max_tts_chars < 1:
            raise ValueError("Voice text limits must be positive.")
        if self.tts_length_scale not in SPEAKING_SPEED_SCALES.values():
            raise ValueError("Speaking speed must be slow, normal, or fast.")
        if self.conversational_stt not in {"gemini", "whisper_cpp", "sherpa_onnx"}:
            raise ValueError("Conversational STT must be gemini, whisper_cpp, or sherpa_onnx.")
        if self.voice_engine not in {"auto", "piper", "kokoro", "voxcpm", "urdu_backend", "urdu_piper"}:
            raise ValueError("Voice engine must be auto, piper, kokoro, voxcpm, urdu_backend, or urdu_piper.")
        if self.voice_quality not in {"auto", "fast", "premium", "expressive", "lightweight", "high_quality"}:
            raise ValueError("Voice quality must be auto, fast, premium, expressive, lightweight, or high_quality.")
        if self.emotion_mode not in {"auto", "neutral"}:
            raise ValueError("Emotion mode must be auto or neutral.")
        if (
            isinstance(self.stt_threads, bool) or not isinstance(self.stt_threads, int)
            or self.stt_threads < 1 or not math.isfinite(self.stt_timeout_seconds)
            or self.stt_timeout_seconds <= 0 or not math.isfinite(self.gemini_timeout_seconds)
            or self.gemini_timeout_seconds <= 0
        ):
            raise ValueError("STT threads and timeout must be positive.")


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
    latency_ms: dict[str, float] = field(default_factory=dict)
    model: str | None = None


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
