"""Generate the scoped, local Phase 9I audio acceptance set."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import sys
import threading
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.profile import CREATOR_NAME, protected_creator_response
from friday.voice.cloning import VoiceCloneRequest
from friday.voice.language import SpeechLanguage, detect_language
from friday.voice.models import (
    URDU_TTS_MODEL_DIR, VOICE_CACHE_DIR,
    VOXCPM_MODEL_DIR, VoiceConfig,
)
from friday.voice.router import TTSRouter, UrduPiperTTS, _merge_audio
from friday.voice.stt import WhisperCppSTT
from friday.voice.style import SpeechStyle, SpeechStyleEngine
from friday.voice.tts import PiperLocalTTS, SynthesizedAudio, _piper_length_scale


CASES = (
    ("urdu_male_script", SpeechLanguage.URDU, "john", "فرائیڈے، میرا پروجیکٹ کھولو۔"),
    ("urdu_female_script", SpeechLanguage.URDU, "lessac", "بلڈ مکمل ہو گئی ہے یا ابھی ایررز ہیں؟"),
    ("roman_urdu_friday", SpeechLanguage.MIXED, "john", "Friday Chrome kholo aur mera project check kro."),
    ("roman_urdu_build", SpeechLanguage.MIXED, "john", "Build complete ho gai hai ya abhi errors hain?"),
    ("mixed_vscode_python", SpeechLanguage.MIXED, "john", "Friday VS Code open kro aur Python tests run kro."),
    ("mixed_github_summary", SpeechLanguage.MIXED, "john", "GitHub repo check kro aur mujhy summary batao."),
)
STYLE_SENTENCE = "This task needs attention. I will explain what happened and what comes next."
STYLE_ENGINE = SpeechStyleEngine()


def _save_audio(name: str, audio: SynthesizedAudio) -> tuple[Path, float]:
    samples = np.asarray(audio.samples, dtype=np.int16).reshape(-1)
    if audio.sample_rate <= 0 or not samples.size:
        raise RuntimeError(f"INVALID_AUDIO:{name}")
    peak = int(np.max(np.abs(samples.astype(np.int32))))
    rms = math.sqrt(float(np.mean(np.square(samples.astype(np.float64)))))
    if peak > 30_000 or rms <= 0:
        raise RuntimeError(f"AUDIO_LEVEL_INVALID:{name}:peak={peak}:rms={rms:.1f}")

    target = OUTPUT / f"{name}.wav"
    with wave.open(str(target), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(audio.sample_rate)
        stream.writeframes(samples.astype("<i2", copy=False).tobytes())
    with wave.open(str(target), "rb") as stream:
        if stream.getframerate() != audio.sample_rate or stream.getnframes() == 0:
            raise RuntimeError(f"WAV_HEADER_INVALID:{name}")
    duration = samples.size / audio.sample_rate
    print(f"AUDIO\t{name}\trate={audio.sample_rate}\tduration={duration:.3f}s\tpeak={peak}\trms={rms:.1f}\tbytes={target.stat().st_size}")
    return target, duration


def main() -> None:
    global OUTPUT
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    OUTPUT = VOICE_CACHE_DIR / f"phase9i-acceptance-{stamp}-{time.time_ns() % 100000:05d}"
    OUTPUT.mkdir(parents=True, exist_ok=False)

    config = VoiceConfig(
        voice_engine="piper", voice_quality="lightweight", emotion_mode="auto",
        urdu_tts_model_dir=URDU_TTS_MODEL_DIR,
    )
    english = PiperLocalTTS(config)
    urdu_backend = UrduPiperTTS(config)
    male = urdu_backend._voice_for("john")
    female = urdu_backend._voice_for("lessac")
    router = TTSRouter(config, piper=english)
    if router._urdu is not None or router._voxcpm is not None or english._voice is not None:
        raise RuntimeError("TTS_NOT_LAZY")
    if male.model_path.name != "ur_PK-fasih-medium.onnx" or female.model_path.name != "ur_PK-aegis_female-medium.onnx":
        raise RuntimeError("URDU_VOICE_PROFILE_MAPPING_INVALID")
    print("PASS\tlazy load and stable male/female Urdu profile mapping")

    generated: dict[str, tuple[Path, float]] = {}
    for name, expected_language, profile, text in CASES:
        language = detect_language(text)
        if language is not expected_language:
            raise RuntimeError(f"LANGUAGE_ROUTE_INVALID:{name}:{language.value}")
        request = STYLE_ENGINE.request(
            text, language=language, voice_profile=profile,
            quality_preference="lightweight", override=SpeechStyle.NEUTRAL,
        )
        if not router._should_try_urdu(request):
            raise RuntimeError(f"URDU_ROUTE_DISABLED:{name}")
        voice = male if profile == "john" else female
        scale = _piper_length_scale(request.pace, voice.config.tts_length_scale)
        if language is SpeechLanguage.MIXED:
            audio = UrduPiperTTS._synthesize_mixed(
                request, voice, english, scale, threading.Event(),
            )
        else:
            audio = voice.synthesize(
                request.speech_text, length_scale=scale,
                sentence_silence=request.sentence_silence,
            )
        generated[name] = _save_audio(name, audio)
    print("PASS\tUrdu script, Roman Urdu, mixed Urdu-English, male and female local WAVs")

    english_request = STYLE_ENGINE.request(
        "Friday, please open Chrome and check the project.",
        language=SpeechLanguage.ENGLISH, voice_profile="lessac",
        quality_preference="lightweight", override=SpeechStyle.NEUTRAL,
    )
    english_audio = english.synthesize(english_request.speech_text)
    generated["english_regression"] = _save_audio("english_regression", english_audio)
    print("PASS\tEnglish Piper regression")

    style_audio: dict[SpeechStyle, SynthesizedAudio] = {}
    style_parameters: set[tuple[float, float]] = set()
    style_durations: dict[SpeechStyle, float] = {}
    for style in SpeechStyle:
        request = STYLE_ENGINE.request(
            STYLE_SENTENCE, language=SpeechLanguage.ENGLISH, voice_profile="lessac",
            quality_preference="lightweight", override=style,
        )
        scale = _piper_length_scale(request.pace, english.config.tts_length_scale)
        style_parameters.add((scale, request.sentence_silence))
        audio = english.synthesize(
            request.speech_text, length_scale=scale,
            sentence_silence=request.sentence_silence,
        )
        style_audio[style] = audio
        _, style_durations[style] = _save_audio(f"style_{style.value}", audio)
        print(f"STYLE\t{style.value}\tlength_scale={scale:.3f}\tsentence_silence={request.sentence_silence:.3f}")
    if len(style_parameters) != len(SpeechStyle):
        raise RuntimeError("PIPER_STYLE_PARAMETERS_COLLIDE")
    if any(style_durations[style] == style_durations[SpeechStyle.NEUTRAL] for style in SpeechStyle if style is not SpeechStyle.NEUTRAL):
        raise RuntimeError("PIPER_STYLE_AUDIO_DURATION_UNCHANGED")
    if any(np.array_equal(style_audio[style].samples, style_audio[SpeechStyle.NEUTRAL].samples)
           for style in SpeechStyle if style is not SpeechStyle.NEUTRAL):
        raise RuntimeError("PIPER_STYLE_WAVEFORM_UNCHANGED")
    print("PASS\tall nine Piper prosody baselines generate distinct bounded audio timing")

    mixed = _merge_audio([
        SynthesizedAudio(np.full(1_600, 1_000, dtype=np.int16), 16_000),
        SynthesizedAudio(np.full(2_205, 500, dtype=np.int16), 22_050),
    ])
    expected_pause = round(mixed.sample_rate * 0.06)
    if mixed.sample_rate != 16_000 or not np.any(np.convolve(
        (mixed.samples == 0).astype(np.int32), np.ones(expected_pause, dtype=np.int32), mode="valid",
    ) == expected_pause):
        raise RuntimeError("MIXED_AUDIO_RATE_OR_PAUSE_INVALID")
    if int(np.max(np.abs(mixed.samples.astype(np.int32)))) > 26_214:
        raise RuntimeError("MIXED_AUDIO_CLIPPING_OR_LEVEL_INVALID")
    print("PASS\tmixed segment sample-rate conversion, 60 ms pause, normalized peak, no clipping")

    if CREATOR_NAME != "Ali" or "Ali" not in protected_creator_response("Friday tumhen kis ne banaya?"):
        raise RuntimeError("CREATOR_IDENTITY_CHANGED")
    if len({male.model_path.resolve(), female.model_path.resolve()}) != 2:
        raise RuntimeError("VOICE_IDENTITY_COLLISION")
    clone_request = VoiceCloneRequest(generated["urdu_male_script"][0], approved=False)
    try:
        clone_request.authorize()
    except PermissionError:
        pass
    else:
        raise RuntimeError("VOICE_CLONING_APPROVAL_BYPASSED")
    print("PASS\tAli creator identity, stable male/female models, cloning approval gate")

    whisper = WhisperCppSTT(config)
    if whisper.available:
        prior_temp_audio = set(VOICE_CACHE_DIR.glob("friday-stt-*.wav"))
        result = whisper.transcribe(english_audio.samples, english_audio.sample_rate)
        if result.engine != "whisper.cpp" or set(VOICE_CACHE_DIR.glob("friday-stt-*.wav")) != prior_temp_audio:
            raise RuntimeError("WHISPER_INTERFACE_OR_TEMP_AUDIO_CLEANUP_FAILED")
        print(f"PASS\tWhisper.cpp tiny local interface check\ttranscription_ms={result.duration_ms:.1f}\tmic=not used")
    else:
        print("UNVERIFIED\tWhisper.cpp runtime/model unavailable; no mic or hardware diagnosis attempted")

    if VOXCPM_MODEL_DIR.exists():
        print("VOXCPM\tmodel directory exists; no runtime/model load attempted under the 500 MiB per-file gate")
    else:
        print("VOXCPM\tadapter present; runtime/model absent; no model load or download attempted")
    print("PRIVACY\tno remote TTS/STT call made; synthesized fixtures only; Whisper temporary WAV cleanup checked when available")
    print(f"OUTPUT\t{OUTPUT}\ttotal_bytes={sum(path.stat().st_size for path in OUTPUT.glob('*.wav'))}")

    english.close()
    urdu_backend.close()
    router.close()


if __name__ == "__main__":
    main()
