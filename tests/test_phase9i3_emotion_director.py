from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from friday.voice.emotion import (
    BackendCapabilities,
    EmotionDirector,
    PIPER_CAPABILITIES,
    ProsodyStyle,
    TrueEmotion,
    VocalEvent,
    build_style_instruction,
)
from friday.voice.language import SpeechLanguage, mixed_speech_segments, speech_text_for
from friday.voice.models import VoiceConfig
from friday.voice.router import UrduPiperTTS
from friday.voice.tts import PiperLocalTTS
from friday.voice.style import SpeechStyle


class Phase9I3EmotionDirectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.director = EmotionDirector()

    def test_true_emotion_and_vocal_event_vocabularies_are_separate(self) -> None:
        self.assertEqual([emotion.name for emotion in TrueEmotion], [
            "NEUTRAL", "HAPPY", "AMUSED", "EXCITED", "ANGRY", "SAD", "FEAR",
            "CONCERNED", "EMPATHETIC", "SERIOUS", "CALM",
        ])
        self.assertEqual([event.name for event in VocalEvent], [
            "LAUGH", "SIGH", "GASP", "BREATH", "WHISPER", "SHOUT",
        ])
        self.assertIsNot(TrueEmotion, ProsodyStyle)
        self.assertIsNot(TrueEmotion, VocalEvent)
        self.assertIs(ProsodyStyle, SpeechStyle)

    def test_emotion_direction_and_prosody_mappings_are_deterministic(self) -> None:
        expected = {
            TrueEmotion.NEUTRAL: ProsodyStyle.NEUTRAL,
            TrueEmotion.HAPPY: ProsodyStyle.POSITIVE,
            TrueEmotion.AMUSED: ProsodyStyle.POSITIVE,
            TrueEmotion.EXCITED: ProsodyStyle.EXCITED,
            TrueEmotion.ANGRY: ProsodyStyle.SERIOUS,
            TrueEmotion.SAD: ProsodyStyle.CALM,
            TrueEmotion.FEAR: ProsodyStyle.CONCERNED,
            TrueEmotion.CONCERNED: ProsodyStyle.CONCERNED,
            TrueEmotion.EMPATHETIC: ProsodyStyle.EMPATHETIC,
            TrueEmotion.SERIOUS: ProsodyStyle.SERIOUS,
            TrueEmotion.CALM: ProsodyStyle.CALM,
        }
        first = {emotion: self.director.direct(emotion) for emotion in TrueEmotion}
        second = {emotion: self.director.direct(emotion) for emotion in TrueEmotion}
        self.assertEqual(first, second)
        self.assertEqual({emotion: self.director.piper_fallback(emotion) for emotion in TrueEmotion}, expected)

    def test_piper_capabilities_and_true_emotion_claim_are_restricted(self) -> None:
        self.assertIs(PiperLocalTTS.capabilities, PIPER_CAPABILITIES)
        self.assertFalse(PiperLocalTTS.capabilities.supports_true_emotion)
        self.assertTrue(PiperLocalTTS.capabilities.supports_prosody)
        self.assertFalse(PiperLocalTTS.capabilities.supports_vocal_events)
        self.assertFalse(PiperLocalTTS.capabilities.supports_prompted_style)

        result = self.director.synthesis_metadata(
            TrueEmotion.ANGRY, backend="piper", capabilities=PiperLocalTTS.capabilities,
            vocal_events=(VocalEvent.SIGH,),
        )
        self.assertIs(result.requested_emotion, TrueEmotion.ANGRY)
        self.assertIsNone(result.applied_emotion)
        self.assertFalse(result.true_emotion_applied)
        self.assertIs(result.prosody_applied, ProsodyStyle.SERIOUS)
        self.assertEqual(result.backend, "PIPER")
        self.assertEqual(result.fallback_reason, "backend_does_not_support_true_emotion")
        self.assertEqual(result.vocal_events, (VocalEvent.SIGH,))

    def test_unsupported_backend_does_not_claim_emotion_or_prosody(self) -> None:
        result = self.director.synthesis_metadata(
            TrueEmotion.HAPPY, backend="unknown", capabilities=BackendCapabilities(),
        )
        self.assertIsNone(result.applied_emotion)
        self.assertFalse(result.true_emotion_applied)
        self.assertIsNone(result.prosody_applied)
        self.assertEqual(result.fallback_reason, "backend_does_not_support_true_emotion")
        unavailable = self.director.synthesis_metadata(
            TrueEmotion.HAPPY, backend="unknown", capabilities=None,
        )
        self.assertEqual(unavailable.fallback_reason, "backend_capabilities_unavailable")
        piper_guard = self.director.synthesis_metadata(
            TrueEmotion.HAPPY, backend="piper",
            capabilities=BackendCapabilities(supports_true_emotion=True, supports_prosody=True),
        )
        self.assertFalse(piper_guard.true_emotion_applied)
        self.assertIsNone(piper_guard.applied_emotion)
        unsupported_specific_emotion = self.director.synthesis_metadata(
            TrueEmotion.HAPPY, backend="future", capabilities=BackendCapabilities(
                supports_true_emotion=True, supports_prosody=True,
            ), supports_emotion=False,
        )
        self.assertFalse(unsupported_specific_emotion.true_emotion_applied)
        supported_emotion = self.director.synthesis_metadata(
            TrueEmotion.HAPPY, backend="future", capabilities=BackendCapabilities(
                supports_true_emotion=True,
            ), supports_emotion=True,
        )
        self.assertTrue(supported_emotion.true_emotion_applied)
        self.assertIs(supported_emotion.applied_emotion, TrueEmotion.HAPPY)

    def test_style_instruction_keeps_direction_language_and_context_separate(self) -> None:
        self.assertEqual(
            build_style_instruction(TrueEmotion.ANGRY, SpeechLanguage.ENGLISH),
            "Speak with restrained anger, firm delivery, and controlled intensity.",
        )
        self.assertEqual(
            build_style_instruction(TrueEmotion.EMPATHETIC, "Urdu"),
            "Speak gently, patiently, and reassuringly. Keep the spoken language as Urdu.",
        )
        self.assertIn("language as Urdu", build_style_instruction(TrueEmotion.CALM, "urdu"))
        fear = build_style_instruction(TrueEmotion.FEAR, "English", "a safety warning")
        self.assertIn("Speak cautiously, with tense, slightly trembling delivery.", fear)
        self.assertIn("Context: a safety warning", fear)
        self.assertNotIn("spoken text", fear.casefold())

    def test_urdu_male_and_female_voice_routes_remain_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = VoiceConfig(urdu_tts_model_dir=Path(directory))
            router = UrduPiperTTS(config)
            with patch("friday.voice.router.PiperLocalTTS", side_effect=lambda value: value):
                male = router._voice_for("john")
                female = router._voice_for("lessac")
        self.assertEqual(Path(male.tts_model_path).name, "ur_PK-fasih-medium.onnx")
        self.assertEqual(Path(female.tts_model_path).name, "ur_PK-aegis_female-medium.onnx")

    def test_roman_urdu_and_mixed_urdu_english_regressions(self) -> None:
        roman = "Friday Chrome kholo aur mera project check kro."
        self.assertIn("کھولو", speech_text_for(roman))
        segments = mixed_speech_segments(roman)
        self.assertIn((SpeechLanguage.ENGLISH, "Friday Chrome"), segments)
        self.assertTrue(any(language is SpeechLanguage.URDU for language, _ in segments))

    def test_direction_generation_makes_no_remote_speech_calls(self) -> None:
        with patch("urllib.request.urlopen") as urlopen, patch.object(
            socket, "create_connection", side_effect=AssertionError("remote call attempted"),
        ) as create_connection:
            for emotion in TrueEmotion:
                self.director.direct(emotion)
                build_style_instruction(emotion, SpeechLanguage.ENGLISH)
                self.director.synthesis_metadata(
                    emotion, backend="piper", capabilities=PIPER_CAPABILITIES,
                )
        urlopen.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
