from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from friday.profile import CREATOR_NAME, AssistantProfile, load_profile, protected_creator_response, update_identity
from friday.voice.cloning import VoiceCloneRequest
from friday.voice.language import SpeechLanguage, detect_language, mixed_speech_segments, normalize_for_brain, speech_text_for
from friday.voice.models import VoiceConfig
from friday.voice.router import TTSRouter, VoxCPMTTS
from friday.voice.style import SpeechStyle, SpeechStyleEngine


class _Piper:
    engine = "piper"
    available = True
    is_speaking = False
    voice_name = "lessac"
    last_error = None
    last_first_audio_latency_ms = None

    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, text): self.spoken.append(text)
    def stop(self): pass
    def close(self): pass


class Phase9IVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SpeechStyleEngine()

    def test_bounded_style_selection_and_explicit_override(self) -> None:
        self.assertEqual(self.engine.select("Hello."), SpeechStyle.NEUTRAL)
        self.assertEqual(self.engine.select("Build completed successfully."), SpeechStyle.POSITIVE)
        self.assertEqual(self.engine.select("Warning: approval is required."), SpeechStyle.SERIOUS)
        self.assertEqual(self.engine.select("The build failed with an error."), SpeechStyle.SERIOUS)
        self.assertEqual(self.engine.select("All good.", user_text="Friday thora calmly bolo."), SpeechStyle.CALM)
        self.assertEqual(self.engine.select("All good.", user_text="Serious voice use kro."), SpeechStyle.SERIOUS)
        self.assertEqual(
            self.engine.select("All good.", user_text="Excited ho k batao.", override=SpeechStyle.NEUTRAL),
            SpeechStyle.EXCITED,
        )

    def test_style_does_not_change_content_or_policy(self) -> None:
        request = self.engine.request("Approval is required before deleting files.", language=SpeechLanguage.ENGLISH,
                                      voice_profile="lessac", quality_preference="auto")
        self.assertEqual(request.text, "Approval is required before deleting files.")
        self.assertEqual(request.speech_text, request.text)

    def test_roman_urdu_and_mixed_normalization_preserve_technical_terms(self) -> None:
        roman = "Friday Chrome kholo aur mera project check kro."
        self.assertEqual(detect_language(roman), SpeechLanguage.MIXED)
        speech = speech_text_for(roman)
        self.assertIn("Chrome", speech)
        self.assertIn("project", speech)
        self.assertIn("کھولو", speech)
        self.assertIn("اور", speech)
        self.assertIn("کرو", speech)
        self.assertEqual(detect_language("Friday VS Code open kro aur Python tests run kro."), SpeechLanguage.MIXED)

        segments = mixed_speech_segments(roman)
        self.assertEqual(
            [content for language, content in segments if language is SpeechLanguage.ENGLISH],
            ["Friday Chrome", "project check"],
        )

    def test_urdu_script_and_english_are_preserved(self) -> None:
        self.assertEqual(detect_language("فرائیڈے، میرا پروجیکٹ کھولو۔"), SpeechLanguage.URDU)
        self.assertIn("پروجیکٹ", normalize_for_brain("فرائیڈے، میرا پروجیکٹ کھولو۔").display_text)
        self.assertEqual(speech_text_for("Friday, open Visual Studio Code."), "Friday, open Visual Studio Code.")

    def test_mixed_segments_keep_technical_terms_in_english(self) -> None:
        segments = mixed_speech_segments("Friday Chrome kholo aur Python tests run kro.")
        self.assertEqual(
            [content for language, content in segments if language is SpeechLanguage.ENGLISH],
            ["Friday Chrome", "Python tests run"],
        )
        self.assertTrue(any(language is SpeechLanguage.URDU for language, _ in segments))

    def test_style_instructions_are_bounded_and_specific(self) -> None:
        self.assertEqual(self.engine.request("done", language=SpeechLanguage.ENGLISH, voice_profile="lessac", quality_preference="auto").instruction,
                         "pleased, warm, slightly energetic")
        self.assertEqual(self.engine.request("warning", language=SpeechLanguage.ENGLISH, voice_profile="lessac", quality_preference="auto", user_text="serious tone mein batao").instruction,
                         "serious, controlled, slightly slower")

    def test_all_piper_styles_have_distinct_bounded_prosody(self) -> None:
        requests = [
            self.engine.request(
                "The same sentence for every style.", language=SpeechLanguage.ENGLISH,
                voice_profile="lessac", quality_preference="lightweight",
                user_text=f"Use {style.value} tone.",
            )
            for style in SpeechStyle
        ]
        self.assertEqual([request.style for request in requests], list(SpeechStyle))
        prosody = [(request.pace, request.sentence_silence) for request in requests]
        self.assertEqual(len(set(prosody)), len(SpeechStyle))
        self.assertTrue(all(0.88 <= request.pace <= 1.12 for request in requests))
        self.assertTrue(all(0.0 <= request.sentence_silence <= 0.15 for request in requests))

    def test_failure_and_important_problem_choose_different_styles(self) -> None:
        self.assertEqual(self.engine.select("The build failed with an error."), SpeechStyle.SERIOUS)
        self.assertEqual(self.engine.select("There is an important problem."), SpeechStyle.CONCERNED)

    def test_router_is_lazy_and_piper_is_the_safe_fallback(self) -> None:
        class _NoUrdu:
            available = False
            def __init__(self, _config): pass
            def close(self): pass

        piper = _Piper()
        router = TTSRouter(VoiceConfig(), piper=piper, urdu_factory=_NoUrdu)
        self.assertIsNone(router._voxcpm)
        request = self.engine.request("Build complete ho gai hai.", language=SpeechLanguage.ROMAN_URDU,
                                      voice_profile="lessac", quality_preference="auto")
        router.speak_render(request)
        self.assertEqual(router.last_route, "piper")
        self.assertEqual(len(piper.spoken), 1)
        self.assertEqual(piper.spoken[0], request.speech_text)
        self.assertIsNotNone(router._urdu)

    def test_high_quality_failure_falls_back_and_does_not_duplicate_backend(self) -> None:
        class _BrokenVox:
            engine = "voxcpm"
            available = True
            is_speaking = False
            last_error = None
            last_first_audio_latency_ms = None
            def __init__(self, _config): self.calls = 0
            def speak_render(self, _request):
                self.calls += 1
                raise RuntimeError("unavailable")
            def close(self): pass

        piper = _Piper()
        router = TTSRouter(VoiceConfig(), piper=piper, voxcpm_factory=_BrokenVox)
        request = self.engine.request("English response.", language=SpeechLanguage.ENGLISH,
                                      voice_profile="lessac", quality_preference="high_quality")
        router.speak_render(request)
        self.assertEqual(router.last_route, "piper")
        self.assertEqual(router._voxcpm.calls, 1)
        self.assertEqual(len(piper.spoken), 1)

    def test_voxcpm_runtime_failure_falls_back_to_piper(self) -> None:
        class _FailingVox(VoxCPMTTS):
            @property
            def available(self): return True
            def __init__(self, config):
                self.config = config
                self._model = None
                self._output = object()
                self._cancel = threading.Event()
                self._worker = None
                self._speaking = False
                self.last_error = None
                self.last_first_audio_latency_ms = None
                self._fallback = None
            def synthesize(self, _request): raise RuntimeError("runtime failed")

        piper = _Piper()
        router = TTSRouter(VoiceConfig(), piper=piper, voxcpm_factory=_FailingVox)
        request = self.engine.request("English response.", language=SpeechLanguage.ENGLISH,
                                      voice_profile="lessac", quality_preference="high_quality")
        router.speak_render(request)
        router._voxcpm._worker.join(timeout=1.0)
        self.assertFalse(router._voxcpm._worker.is_alive())
        self.assertEqual(router._voxcpm.last_error, "RuntimeError")
        self.assertEqual(router.last_route, "piper")
        self.assertEqual(piper.spoken, [request.speech_text])

    def test_profile_voice_settings_and_creator_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            identity = root / "identity.json"
            profile.write_text("{}", encoding="utf-8")
            update_identity(path=identity, profile_path=profile, voice_engine="piper", voice_quality="lightweight")
            loaded = load_profile(profile, identity_path=identity)
        self.assertEqual(loaded.voice_engine, "piper")
        self.assertEqual(loaded.voice_quality, "lightweight")
        self.assertEqual(CREATOR_NAME, "Ali")
        self.assertEqual(protected_creator_response("Who created you?"), "FRIDAY was created by Ali.")

    def test_private_voice_configuration_never_enables_remote_tts(self) -> None:
        profile = AssistantProfile()
        self.assertFalse(profile.cloud_brain_enabled)
        self.assertEqual(VoiceConfig().voice_engine, "auto")

    def test_voice_clone_requires_explicit_local_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "authorized.wav"
            recording.write_bytes(b"RIFF")
            with self.assertRaisesRegex(PermissionError, "EXPLICIT_APPROVAL"):
                VoiceCloneRequest(recording).authorize()
            self.assertEqual(VoiceCloneRequest(recording, approved=True).authorize(), recording)


if __name__ == "__main__":
    unittest.main()
