"""Local speech-to-text adapters. The default engine is Sherpa-ONNX CPU."""

from __future__ import annotations

import gc
import base64
from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid
import wave

import numpy as np

from friday.runtime_config import child_process_environment
from friday.voice.models import (
    GEMINI_STT_CONFIG_PATH, STT_MODEL_DIR, VOICE_CACHE_DIR, TranscriptResult, VoiceConfig,
)

WHISPER_SAMPLE_RATE = 16_000
GEMINI_TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
GEMINI_FLASH_LITE_MODEL = "gemini-3.5-flash-lite"
GEMINI_STT_MODELS = (GEMINI_TRANSCRIBE_MODEL, GEMINI_FLASH_LITE_MODEL)
GEMINI_ALLOWED_MODELS = GEMINI_STT_MODELS
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MAX_INLINE_BYTES = 14 * 1024 * 1024
GEMINI_TRANSCRIBE_MAX_PRACTICAL_MS = 4_000.0
GEMINI_TRANSCRIBE_VOCABULARY = [
    "Friday", "Ali", "Calculator", "C drive", "VS Code", "Visual Studio Code",
    "README", "Notepad", "Chrome", "Google", "GitHub", "Codex", "PowerShell",
    "FRIDAY", "friday-core",
]
_TRANSCRIPTION_PROMPT = (
    "Transcribe the speech exactly as spoken. Return only the verbatim transcript. "
    "Do not translate, summarize, explain, rewrite, or answer the speech. "
    "Preserve the spoken language and any English-Urdu code-switching."
)
_TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


class GeminiSTTError(RuntimeError):
    """Sanitized Gemini STT failure code with no request, key, or server body."""


class GeminiTransientError(GeminiSTTError):
    """A network, timeout, or rate/server condition safe for one model fallback."""


class STTUnavailableError(GeminiSTTError):
    def __init__(self, code: str = "STT_UNAVAILABLE") -> None:
        super().__init__("STT_UNAVAILABLE")
        self.reason_code = code if re.fullmatch(r"[A-Z0-9_]{1,64}", code) else "STT_UNAVAILABLE"


@dataclass(frozen=True)
class GeminiSTTSettings:
    selected_model: str
    verified_free_tier_models: tuple[str, ...]


class GeminiSTTModelStore:
    """Persist only allowlisted, non-secret model selection metadata."""

    def __init__(self, path: str | Path = GEMINI_STT_CONFIG_PATH) -> None:
        self.path = Path(path)

    def load(self) -> GeminiSTTSettings:
        verified = {GEMINI_FLASH_LITE_MODEL}
        selected = GEMINI_FLASH_LITE_MODEL
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            raw_verified = value.get("verified_free_tier_models", [])
            if isinstance(raw_verified, list):
                verified.update(item for item in raw_verified if item in GEMINI_STT_MODELS)
            candidate = value.get("selected_model")
            if isinstance(candidate, str) and candidate in verified:
                selected = candidate
        return GeminiSTTSettings(selected, tuple(model for model in GEMINI_STT_MODELS if model in verified))

    def record_transcribe_probe(
        self,
        transcript: str,
        latency_ms: float,
        *,
        free_tier_verified: bool,
        expected_transcript: str = "open calculator",
    ) -> str:
        try:
            latency = float(latency_ms)
        except (TypeError, ValueError):
            latency = math.inf
        passed = (
            bool(free_tier_verified) and math.isfinite(latency)
            and 0.0 <= latency <= GEMINI_TRANSCRIBE_MAX_PRACTICAL_MS
            and _transcript_comparison_key(transcript) == _transcript_comparison_key(expected_transcript)
        )
        verified = [GEMINI_FLASH_LITE_MODEL]
        selected = GEMINI_FLASH_LITE_MODEL
        if passed:
            verified.insert(0, GEMINI_TRANSCRIBE_MODEL)
            selected = GEMINI_TRANSCRIBE_MODEL
        self._save(GeminiSTTSettings(selected, tuple(verified)))
        return selected

    def record_success(self, model: str) -> None:
        settings = self.load()
        if model not in settings.verified_free_tier_models:
            return
        self._save(GeminiSTTSettings(model, settings.verified_free_tier_models))

    def fallback_for(self, primary: str) -> str | None:
        return next((model for model in self.load().verified_free_tier_models if model != primary), None)

    def _save(self, settings: GeminiSTTSettings) -> None:
        if settings.selected_model not in GEMINI_STT_MODELS:
            return
        verified = [model for model in settings.verified_free_tier_models if model in GEMINI_STT_MODELS]
        if settings.selected_model not in verified:
            return
        payload = {
            "selected_model": settings.selected_model,
            "verified_free_tier_models": [model for model in GEMINI_STT_MODELS if model in verified],
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class GeminiSTT:
    """One-shot Gemini transcription behind FRIDAY's existing STT interface."""

    engine = "gemini"
    is_cloud = True

    def __init__(
        self,
        config: VoiceConfig | None = None,
        *,
        model_store: GeminiSTTModelStore | None = None,
        request_json=None,
        api_key_provider=None,
    ) -> None:
        self.config = config or VoiceConfig()
        self.model_store = model_store or GeminiSTTModelStore(self.config.gemini_stt_config_path)
        self._request_json = request_json or _post_gemini_json
        self._api_key_provider = api_key_provider or (lambda: os.environ.get("GEMINI_API_KEY", "").strip())

    @property
    def available(self) -> bool:
        return bool(self._api_key_provider())

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16_000) -> TranscriptResult:
        started = time.perf_counter()
        api_key = self._api_key_provider()
        if not api_key:
            raise STTUnavailableError("GEMINI_KEY_MISSING")
        audio = _resample(samples, sample_rate, WHISPER_SAMPLE_RATE)
        if audio.size == 0:
            return TranscriptResult("", None, None, 0.0, 0.0, self.engine)
        wav_bytes = _encode_pcm_wav(audio, WHISPER_SAMPLE_RATE)
        if len(wav_bytes) > GEMINI_MAX_INLINE_BYTES:
            raise STTUnavailableError("GEMINI_AUDIO_TOO_LARGE")
        primary = self.model_store.load().selected_model
        try:
            result = self._transcribe_model(primary, wav_bytes, api_key)
        except GeminiTransientError:
            fallback = self.model_store.fallback_for(primary)
            if fallback is None:
                raise STTUnavailableError("GEMINI_PRIMARY_TRANSIENT_NO_VERIFIED_FALLBACK") from None
            try:
                result = self._transcribe_model(fallback, wav_bytes, api_key)
            except GeminiSTTError:
                raise STTUnavailableError("GEMINI_PRIMARY_AND_FALLBACK_UNAVAILABLE") from None
            self.model_store.record_success(fallback)
        except GeminiSTTError as error:
            raise STTUnavailableError(error.reason_code if isinstance(error, STTUnavailableError) else str(error)) from None
        else:
            self.model_store.record_success(primary)
        latency = dict(result.latency_ms)
        latency["gemini_total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return TranscriptResult(
            result.text, result.confidence, result.language, latency["gemini_total_ms"],
            result.audio_duration_ms, result.engine, result.is_final, latency, result.model,
        )

    def probe_dedicated_candidate(
        self,
        samples: np.ndarray,
        sample_rate: int = 16_000,
        *,
        free_tier_verified: bool,
    ) -> tuple[TranscriptResult, str]:
        """Probe the dedicated model once; never falls back during model selection."""
        api_key = self._api_key_provider()
        if not api_key:
            raise STTUnavailableError("GEMINI_KEY_MISSING")
        audio = _resample(samples, sample_rate, WHISPER_SAMPLE_RATE)
        wav_bytes = _encode_pcm_wav(audio, WHISPER_SAMPLE_RATE)
        try:
            result = self._transcribe_model(GEMINI_TRANSCRIBE_MODEL, wav_bytes, api_key)
        except GeminiSTTError:
            self.model_store.record_transcribe_probe("", math.inf, free_tier_verified=False)
            raise
        selected = self.model_store.record_transcribe_probe(
            result.text, result.duration_ms, free_tier_verified=free_tier_verified,
        )
        return result, selected

    def _transcribe_model(self, model: str, wav_bytes: bytes, api_key: str) -> TranscriptResult:
        encoded_audio = base64.b64encode(wav_bytes).decode("ascii")
        if model == GEMINI_TRANSCRIBE_MODEL:
            payload = {
                "model": GEMINI_TRANSCRIBE_MODEL,
                "input": [
                    {"type": "text", "text": _TRANSCRIPTION_PROMPT},
                    {"type": "audio", "data": encoded_audio, "mime_type": "audio/wav"},
                ],
                "generation_config": {
                    "transcription_config": {
                        "mode": {"type": "verbatim"},
                        "custom_vocabulary": GEMINI_TRANSCRIBE_VOCABULARY,
                    },
                },
            }
            url = GEMINI_INTERACTIONS_URL
        elif model == GEMINI_FLASH_LITE_MODEL:
            payload = {
                "contents": [{"role": "user", "parts": [
                    {"text": _TRANSCRIPTION_PROMPT},
                    {"inlineData": {"mimeType": "audio/wav", "data": encoded_audio}},
                ]}],
                "generationConfig": {"temperature": 0, "candidateCount": 1},
            }
            url = GEMINI_GENERATE_URL.format(model=GEMINI_FLASH_LITE_MODEL)
        else:
            raise GeminiSTTError("GEMINI_MODEL_NOT_ALLOWED")
        try:
            response, request_ms = self._request_json(
                url, payload, api_key=api_key, timeout_seconds=self.config.gemini_timeout_seconds,
            )
        except GeminiSTTError:
            raise
        except Exception as error:
            raise GeminiSTTError("GEMINI_STT_REQUEST_FAILED") from None
        text = _transcript_from_response(model, response)
        if not text:
            raise GeminiSTTError("GEMINI_TRANSCRIPT_EMPTY")
        duration = round(audio_duration_ms_from_wav(wav_bytes), 2)
        return TranscriptResult(
            text, None, None, float(request_ms), duration, self.engine, True,
            {
                "gemini_upload_ms": 0.0,
                "gemini_request_ms": float(request_ms),
                "transcription_ms": float(request_ms),
            },
            model,
        )

    def close(self) -> None:
        """The adapter is stateless; request audio and keys are not retained."""


def _post_gemini_json(url: str, payload: dict, *, api_key: str, timeout_seconds: float):
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as error:
        if error.code in _TRANSIENT_HTTP_CODES:
            raise GeminiTransientError(f"GEMINI_STT_TRANSIENT_HTTP_{error.code}") from None
        raise GeminiSTTError(f"GEMINI_STT_HTTP_{error.code}") from None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        raise GeminiTransientError("GEMINI_STT_TRANSIENT_NETWORK") from None
    if len(body) > 2 * 1024 * 1024:
        raise GeminiSTTError("GEMINI_STT_RESPONSE_TOO_LARGE")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise GeminiSTTError("GEMINI_STT_INVALID_RESPONSE") from None
    if not isinstance(parsed, dict):
        raise GeminiSTTError("GEMINI_STT_INVALID_RESPONSE")
    return parsed, round((time.perf_counter() - started) * 1000, 2)


def _transcript_from_response(model: str, response: object) -> str:
    pieces: list[str] = []
    if not isinstance(response, dict):
        return ""
    if model == GEMINI_TRANSCRIBE_MODEL:
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            pieces.append(output_text)
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict) and isinstance(item.get("content"), list):
                    pieces.extend(
                        block["text"] for block in item["content"]
                        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                    )
    else:
        candidates = response.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                content = candidate.get("content") if isinstance(candidate, dict) else None
                parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list):
                    pieces.extend(
                        part["text"] for part in parts
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
    return _normalize_gemini_transcript(" ".join(pieces))


def _normalize_gemini_transcript(value: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value)).replace("\x00", " ")
    return " ".join(text.split())[:2_000]


def _transcript_comparison_key(value: str) -> str:
    return re.sub(r"[^\w]+", " ", _normalize_gemini_transcript(value).casefold()).strip()


def _encode_pcm_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    audio = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1), nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return buffer.getvalue()


def audio_duration_ms_from_wav(wav_bytes: bytes) -> float:
    with wave.open(BytesIO(wav_bytes), "rb") as source:
        return source.getnframes() * 1000 / source.getframerate()


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
        tail_padding = np.zeros(round(self.config.sample_rate * 0.66), dtype=np.float32)
        stream.accept_waveform(self.config.sample_rate, tail_padding)
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


class WhisperCppSTT:
    """Legacy/debug-only whisper.cpp adapter; never a Gemini fallback."""

    engine = "whisper.cpp"

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or VoiceConfig()
        self.executable = Path(self.config.whisper_cpp_executable)
        self.model_path = Path(self.config.whisper_cpp_model)
        self.language = _validate_language_hint(self.config.stt_language_hint)

    @property
    def available(self) -> bool:
        return self.executable.is_file() and self.model_path.is_file()

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16_000) -> TranscriptResult:
        audio = _resample(samples, sample_rate, WHISPER_SAMPLE_RATE)
        if audio.size == 0:
            return TranscriptResult("", None, None, 0.0, 0.0, self.engine)
        if not self.executable.is_file():
            raise RuntimeError("WHISPER_CPP_EXECUTABLE_MISSING")
        if not self.model_path.is_file():
            raise RuntimeError("WHISPER_CPP_MODEL_MISSING")

        started = time.perf_counter()
        wav_path: Path | None = None
        try:
            VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="friday-stt-", suffix=".wav", dir=VOICE_CACHE_DIR, delete=False,
            ) as temporary:
                wav_path = Path(temporary.name)
            _write_pcm_wav(wav_path, audio, WHISPER_SAMPLE_RATE)
            command = [
                str(self.executable), "-m", str(self.model_path), "-f", str(wav_path),
                "-ng", "-t", str(self.config.stt_threads), "-l", self.language, "-nt",
            ]
            run_options = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": self.config.stt_timeout_seconds,
                "check": False,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            run_options["env"] = child_process_environment()
            try:
                completed = subprocess.run(command, **run_options)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("WHISPER_CPP_TIMEOUT") from error
            except OSError as error:
                raise RuntimeError(f"WHISPER_CPP_EXECUTION_FAILED: {error}") from error
            if completed.returncode:
                detail = " ".join(str(completed.stderr or "").split())[-300:]
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"WHISPER_CPP_EXIT_{completed.returncode}{suffix}")
            text = _parse_whisper_stdout(str(completed.stdout or ""))[: self.config.max_transcript_chars]
            return TranscriptResult(
                text=text,
                confidence=None,
                language=None,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                audio_duration_ms=round(audio.size * 1000 / WHISPER_SAMPLE_RATE, 2),
                engine=self.engine,
                is_final=True,
            )
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError as error:
                    raise RuntimeError("WHISPER_CPP_TEMP_AUDIO_CLEANUP_FAILED") from error

    def close(self) -> None:
        """The CLI process is scoped to transcribe(); no resident model needs closing."""


def create_local_stt(config: VoiceConfig | None = None) -> LocalSTT:
    """Create configured STT; local recognizers require an explicit debug selection."""
    selected = config or VoiceConfig()
    if selected.conversational_stt == "gemini":
        return GeminiSTT(selected)
    if selected.conversational_stt == "whisper_cpp":
        return WhisperCppSTT(selected)
    if selected.conversational_stt == "sherpa_onnx":
        return SherpaOnnxSTT(selected)
    raise ValueError("CONVERSATIONAL_STT_PROVIDER_UNSUPPORTED")


def _validate_language_hint(language: str) -> str:
    value = str(language).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value):
        raise ValueError("STT_LANGUAGE_HINT_INVALID")
    return value


def _write_pcm_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    audio = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1), nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def _parse_whisper_stdout(output: str) -> str:
    speech: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line or line.startswith("[") or lowered.startswith(
            ("main:", "whisper_", "ggml_", "system_info:", "load time =", "total time ="),
        ):
            continue
        speech.append(line)
    return " ".join(speech)


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


__all__ = [
    "GEMINI_ALLOWED_MODELS", "GEMINI_FLASH_LITE_MODEL", "GEMINI_STT_MODELS",
    "GEMINI_TRANSCRIBE_MODEL", "GEMINI_TRANSCRIBE_MAX_PRACTICAL_MS",
    "GEMINI_TRANSCRIBE_VOCABULARY", "GeminiSTT", "GeminiSTTError",
    "GeminiSTTModelStore", "GeminiSTTSettings", "GeminiTransientError", "LocalSTT",
    "STTUnavailableError", "SherpaOnnxSTT", "WhisperCppSTT", "create_local_stt",
]
