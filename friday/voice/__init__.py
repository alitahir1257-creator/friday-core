"""Optional, local-first voice input and output for FRIDAY Core."""

from friday.voice.manager import VoiceManager
from friday.voice.emotion import (
    BackendCapabilities,
    DeliverySpecification,
    EmotionDirector,
    EmotionSynthesisMetadata,
    EmotionalTTSBackend,
    PIPER_CAPABILITIES,
    ProsodyStyle,
    TrueEmotion,
    VocalEvent,
    build_style_instruction,
)
from friday.voice.models import (
    AudioDevice,
    ConversationSession,
    DeviceCatalog,
    TranscriptResult,
    VoiceConfig,
    VoiceEvent,
    VoiceMode,
    VoiceState,
    VoiceStatus,
)

__all__ = [
    "AudioDevice", "ConversationSession", "DeviceCatalog", "TranscriptResult",
    "BackendCapabilities", "DeliverySpecification", "EmotionDirector", "EmotionSynthesisMetadata",
    "EmotionalTTSBackend", "PIPER_CAPABILITIES", "ProsodyStyle", "TrueEmotion", "VocalEvent",
    "build_style_instruction",
    "VoiceConfig", "VoiceEvent", "VoiceManager", "VoiceMode", "VoiceState", "VoiceStatus",
]
