from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

from friday.runtime_config import child_process_environment, load_gemini_api_key_from_config
from friday.mcp.client import MCPStdioClient
from friday.mcp.models import MCPServerConfig
from friday.voice.models import VoiceConfig, VoiceMode, VoiceState
from friday.voice.stt import (
    GEMINI_FLASH_LITE_MODEL,
    GEMINI_TRANSCRIBE_MODEL,
    GeminiSTT,
    GeminiSTTModelStore,
    GeminiTransientError,
    STTUnavailableError,
)
from friday.voice.manager import VoiceManager
from friday.voice.vad import EnergyVAD
from scripts import gemini_stt_selection_probe


def test_key_loader_reads_only_the_expected_variable_without_output(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / ".env.local"
    key_file.write_text("OTHER=value\nGEMINI_API_KEY=unit-test-value\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "inherited-test-value")

    assert load_gemini_api_key_from_config(key_file)
    assert os.environ["GEMINI_API_KEY"] == "unit-test-value"
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_key_loader_missing_file_is_nonfatal_and_does_not_create_a_key(tmp_path, monkeypatch):
    missing = tmp_path / "missing.env"
    monkeypatch.setenv("GEMINI_API_KEY", "inherited-test-value")

    assert not load_gemini_api_key_from_config(missing)
    assert "GEMINI_API_KEY" not in os.environ
    assert not missing.exists()


def test_child_process_environment_does_not_inherit_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-value")
    monkeypatch.setenv("PATH", "D:\\FridayTest")

    child_env = child_process_environment()

    assert "GEMINI_API_KEY" not in child_env
    assert child_env["PATH"] == "D:\\FridayTest"


def test_mcp_environment_cannot_forward_gemini_key_directly_or_by_alias(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-value")
    monkeypatch.setenv("FRIDAY_TEST_VALUE", "safe-test-value")
    config = MCPServerConfig(
        name="test", command="python",
        environment_allowlist=frozenset({"GEMINI_API_KEY", "FORWARDED_KEY", "SAFE_VALUE"}),
        environment_refs={
            "GEMINI_API_KEY": "GEMINI_API_KEY",
            "FORWARDED_KEY": "GEMINI_API_KEY",
            "SAFE_VALUE": "FRIDAY_TEST_VALUE",
        },
    )

    child_env = MCPStdioClient(config)._child_environment()

    assert "GEMINI_API_KEY" not in child_env
    assert "FORWARDED_KEY" not in child_env
    assert child_env["SAFE_VALUE"] == "safe-test-value"


def test_selection_probe_requires_the_d_config_key_even_if_process_env_has_one(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-value")
    monkeypatch.setattr(gemini_stt_selection_probe, "load_gemini_api_key_from_config", lambda: False)
    monkeypatch.setattr(
        gemini_stt_selection_probe, "GeminiSTT",
        lambda *_args, **_kwargs: pytest.fail("probe must stop before creating the network adapter"),
    )

    assert gemini_stt_selection_probe.main() == 2
    assert capsys.readouterr().out == "STT_UNAVAILABLE: the D: Gemini key configuration is missing or empty.\n"


def test_missing_key_reports_stt_unavailable_without_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = []
    stt = GeminiSTT(
        VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json"),
        request_json=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert not stt.available
    with pytest.raises(STTUnavailableError, match="STT_UNAVAILABLE"):
        stt.transcribe(np.ones(1_600, dtype=np.float32))
    assert calls == []


def test_flash_lite_transcript_parsing_preserves_code_switching_and_uses_one_audio_request(tmp_path):
    observed = []

    def request_json(url, payload, *, api_key, timeout_seconds):
        observed.append((url, payload, api_key, timeout_seconds))
        return {"candidates": [{"content": {"parts": [{"text": "C drive mein kitni space bachi hai?"}]}}]}, 2400.0

    config = VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json")
    stt = GeminiSTT(config, request_json=request_json, api_key_provider=lambda: "unit-test-value")
    result = stt.transcribe(np.ones(16_000, dtype=np.float32))

    assert result.text == "C drive mein kitni space bachi hai?"
    assert result.engine == "gemini"
    assert len(observed) == 1
    url, payload, _key, timeout = observed[0]
    assert GEMINI_FLASH_LITE_MODEL in url
    assert timeout == config.gemini_timeout_seconds
    assert payload["generationConfig"]["temperature"] == 0
    assert payload["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "audio/wav"
    assert "custom_vocabulary" not in json.dumps(payload)
    assert result.latency_ms["gemini_upload_ms"] == 0
    assert result.latency_ms["gemini_request_ms"] == 2400


def test_dedicated_transcribe_uses_verbatim_auto_language_and_small_vocabulary(tmp_path):
    observed = []

    def request_json(url, payload, *, api_key, timeout_seconds):
        observed.append((url, payload))
        return {"output_text": "open calculator"}, 2300.0

    store = GeminiSTTModelStore(tmp_path / "voice-stt.json")
    assert store.record_transcribe_probe(
        "open calculator", 2300.0, free_tier_verified=True,
    ) == GEMINI_TRANSCRIBE_MODEL
    stt = GeminiSTT(
        VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json"),
        model_store=store,
        request_json=request_json,
        api_key_provider=lambda: "unit-test-value",
    )

    result = stt.transcribe(np.ones(16_000, dtype=np.float32))

    assert result.text == "open calculator"
    url, payload = observed[0]
    assert url.endswith("/v1beta/interactions")
    assert payload["model"] == GEMINI_TRANSCRIBE_MODEL
    transcription = payload["generation_config"]["transcription_config"]
    assert transcription["mode"] == {"type": "verbatim"}
    assert "language_codes" not in transcription
    assert transcription["custom_vocabulary"] == [
        "Friday", "Ali", "Calculator", "C drive", "VS Code", "Visual Studio Code",
        "README", "Notepad", "Chrome", "Google", "GitHub", "Codex", "PowerShell",
        "FRIDAY", "friday-core",
    ]
    assert "function_call" not in json.dumps(payload).casefold()


def test_transcribe_probe_selects_flash_lite_if_dedicated_result_is_not_eligible(tmp_path):
    store = GeminiSTTModelStore(tmp_path / "voice-stt.json")

    selected = store.record_transcribe_probe(
        "open calculator", 8_000.0, free_tier_verified=True,
    )

    assert selected == GEMINI_FLASH_LITE_MODEL
    saved = json.loads((tmp_path / "voice-stt.json").read_text(encoding="utf-8"))
    assert saved["selected_model"] == GEMINI_FLASH_LITE_MODEL
    assert saved["verified_free_tier_models"] == [GEMINI_FLASH_LITE_MODEL]
    assert "api_key" not in json.dumps(saved).casefold()


def test_transient_primary_failure_uses_at_most_one_verified_free_fallback(tmp_path):
    store = GeminiSTTModelStore(tmp_path / "voice-stt.json")
    store.record_transcribe_probe("open calculator", 2300.0, free_tier_verified=True)
    calls = []

    def request_json(url, payload, *, api_key, timeout_seconds):
        calls.append((url, payload))
        if url.endswith("/v1beta/interactions"):
            raise GeminiTransientError("GEMINI_STT_TRANSIENT_NETWORK")
        return {"candidates": [{"content": {"parts": [{"text": "calculator kholo"}]}}]}, 2500.0

    stt = GeminiSTT(
        VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json"),
        model_store=store,
        request_json=request_json,
        api_key_provider=lambda: "unit-test-value",
    )
    result = stt.transcribe(np.ones(1_600, dtype=np.float32))

    assert result.text == "calculator kholo"
    assert len(calls) == 2
    assert calls[0][1]["model"] == GEMINI_TRANSCRIBE_MODEL
    assert GEMINI_FLASH_LITE_MODEL in calls[1][0]
    assert json.loads((tmp_path / "voice-stt.json").read_text(encoding="utf-8"))["selected_model"] == GEMINI_FLASH_LITE_MODEL


def test_model_store_rejects_unlisted_models_and_auto(tmp_path):
    path = tmp_path / "voice-stt.json"
    path.write_text(json.dumps({
        "selected_model": "auto",
        "verified_free_tier_models": ["auto", "gemini-3.5-pro", GEMINI_FLASH_LITE_MODEL],
    }), encoding="utf-8")

    settings = GeminiSTTModelStore(path).load()

    assert settings.selected_model == GEMINI_FLASH_LITE_MODEL
    assert settings.verified_free_tier_models == (GEMINI_FLASH_LITE_MODEL,)


class _UnavailableSTT:
    engine = "gemini"
    is_cloud = True
    available = True

    def __init__(self):
        self.calls = 0
        self.closed = 0

    def transcribe(self, *_args):
        self.calls += 1
        raise STTUnavailableError("STT_UNAVAILABLE")

    def close(self):
        self.closed += 1


class _SilentPiper:
    engine = "piper"
    voice_name = "Lessac"
    available = True
    is_speaking = False
    last_first_audio_latency_ms = 0.0
    last_error = None

    def __init__(self):
        self.spoken = []

    def speak_render(self, request):
        self.spoken.append(request.speech_text)

    def stop(self):
        pass

    def close(self):
        pass


def test_missing_key_is_reported_as_stt_unavailable_by_voice_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    manager = VoiceManager(
        SimpleNamespace(profile=SimpleNamespace(voice_profile="lessac")),
        VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json"),
        stt=GeminiSTT(
            VoiceConfig(gemini_stt_config_path=tmp_path / "voice-stt.json"),
        ),
        tts=_SilentPiper(),
        wake_detector=SimpleNamespace(close=lambda: None),
        audio_input=SimpleNamespace(stop=lambda: None, active=False),
        device_discovery=lambda: SimpleNamespace(inputs=(), outputs=(), default_input=None, default_output=None, available=False),
    )

    assert manager.start_ptt() == "STT_UNAVAILABLE"
    assert manager.status().availability_code == "STT_UNAVAILABLE"
    assert manager.state == VoiceState.ERROR
    manager.close()


def test_stt_failure_recovers_to_ready_and_speaks_truthful_local_message():
    stt, tts = _UnavailableSTT(), _SilentPiper()
    events = []
    manager = VoiceManager(
        SimpleNamespace(profile=SimpleNamespace(voice_profile="lessac")),
        VoiceConfig(), stt=stt, tts=tts,
        wake_detector=SimpleNamespace(close=lambda: None),
        audio_input=SimpleNamespace(stop=lambda: None, active=False),
        device_discovery=lambda: SimpleNamespace(inputs=(), outputs=(), default_input=None, default_output=None, available=False),
        event_listener=events.append,
    )
    manager._begin_session(VoiceMode.PTT, "public")

    manager._process_utterance(np.ones(1_600, dtype=np.float32), time.monotonic(), 0.0)

    assert stt.calls == 1
    assert stt.closed >= 1
    assert tts.spoken and "unavailable" in tts.spoken[0].casefold()
    assert any(event.name == "voice_error" and event.data.get("code") == "STT_UNAVAILABLE" for event in events)
    assert manager.state == VoiceState.IDLE
    manager.close()


def test_private_voice_never_calls_gemini():
    stt, tts = _UnavailableSTT(), _SilentPiper()
    manager = VoiceManager(
        SimpleNamespace(profile=SimpleNamespace(voice_profile="lessac")),
        VoiceConfig(), stt=stt, tts=tts,
        wake_detector=SimpleNamespace(close=lambda: None),
        audio_input=SimpleNamespace(stop=lambda: None, active=False),
        device_discovery=lambda: SimpleNamespace(inputs=(), outputs=(), default_input=None, default_output=None, available=False),
    )
    manager._begin_session(VoiceMode.PTT, "private")

    manager._process_utterance(np.ones(1_600, dtype=np.float32), time.monotonic(), 0.0)

    assert stt.calls == 0
    assert tts.spoken and "private" in tts.spoken[0].casefold()
    assert manager.state == VoiceState.IDLE
    manager.close()


def test_vad_emits_one_finished_utterance_with_silence_end_overhead():
    vad = EnergyVAD(
        sample_rate=1_000, frame_ms=100, speech_threshold=0.1,
        silence_end_seconds=0.3, max_utterance_seconds=2.0,
        preroll_seconds=0.1, start_frames=1, min_speech_seconds=0.1,
    )
    speech = np.full(100, 0.2, dtype=np.float32)
    silence = np.zeros(100, dtype=np.float32)

    assert not vad.feed(speech).speech_ended
    assert not vad.feed(silence).speech_ended
    assert not vad.feed(silence).speech_ended
    ended = vad.feed(silence)

    assert ended.speech_ended
    assert ended.utterance is not None
    assert ended.end_detection_ms == 300
    assert not vad.feed(silence).speech_ended
