# Phase 9I — Expressive Voice + Urdu / Roman Urdu

## Preserved local stack

Whisper.cpp remains primary STT and Sherpa remains wake/KWS. Piper remains the lightweight local TTS. The router keeps the current barge-in, PTT, continuous conversation, self-loop protection, profiles, speed, and PRIVATE-mode boundaries; this phase adds no cloud speech route or TTS framework.

English routes to the existing Piper voice. Urdu script routes to Urdu Piper. Roman Urdu is normalized only for speech while the display text stays unchanged. Mixed Urdu-English text is split around protected technical terms and rendered by Urdu or English Piper as appropriate, then resampled to one rate, peak-normalized, and joined with a short pause.

## Verified Urdu voices

The pre-existing local models were exercised on Windows CPU with Piper TTS 1.8.0. Both returned valid non-empty mono PCM WAV audio at 22,050 Hz:

| Profile | Model | Exact model size | License |
| --- | --- | ---: | --- |
| Male | `ur_PK-fasih-medium.onnx` | 63,532,015 bytes | MIT |
| Female | `ur_PK-aegis_female-medium.onnx` | 63,515,589 bytes | MIT |

Each matching `.onnx.json` config is present and loaded by the runtime. The voice models identify Urdu (`ur`) and a 22,050 Hz sample rate. [Fasih model card](https://huggingface.co/rhasspy/piper-voices/blob/main/ur/ur_PK/fasih/medium/MODEL_CARD), [Piper voice metadata](https://huggingface.co/rhasspy/piper-voices/blob/main/voices.json), [Aegis female model card](https://huggingface.co/mahwizzzz/piper-voice-ur-aegis-female).

Actual local WAV acceptance covered both required Urdu-script utterances, both required Roman Urdu utterances, both mixed Urdu-English utterances, and male/female Urdu profiles. The acceptance set is in `D:\FRIDAY\data\cache\voice\phase9i-acceptance-*`.

## Piper expressive baseline

Classification: `PROSODY_STYLE_BASELINE`, not emotional timbre. All nine styles reach Piper through bounded inverse length-scale and sentence-silence controls. Actual WAVs were generated from the same English sentence; each style had distinct parameters, non-neutral duration, and a non-identical waveform. This is measurable pacing/pause variation, not a claim that Piper synthesizes genuine emotion.

The existing local response/user text context selects presentation; explicit calm, positive, excited, confident, serious, concerned, empathetic, and urgent overrides take precedence even over a neutral profile setting. It does not affect safety, approvals, truthfulness, or task decisions. Uncertain content remains neutral.

## VoxCPM2 status

The FRIDAY adapter exists, but VoxCPM is **not implemented**: no runtime/model is installed and no VoxCPM synthesis or Voice Design request was run. The official `llama.cpp-omni` Q8 GGUF route requires `VoxCPM2-BaseLM-Q8_0.gguf` (1,727,309,920 bytes) and `VoxCPM2-Acoustic-F16.gguf` (1,825,096,352 bytes), 3,552,406,272 bytes total; each individual file exceeds the 500 MiB approval gate. No VoxCPM file was downloaded. Classification: `BLOCKED_BY_MODEL_SIZE_APPROVAL`. The documented route's Voice Design behavior has not been validated through FRIDAY runtime synthesis. No RAM, peak-RAM, cold/warm latency, or RTF measurements are claimed. [Official VoxCPM repository](https://github.com/OpenBMB/VoxCPM), [GGUF files and route](https://huggingface.co/DennisHuang648/VoxCPM2-GGUF), [llama.cpp-omni VoxCPM2 guide](https://github.com/tc-mb/llama.cpp-omni/blob/master/tools/omni/voxcpm2/README.md).

## Privacy and cloning

The acceptance used local Piper and made zero remote speech calls. The one tiny Whisper.cpp interface check used an in-memory synthesized fixture, not a microphone; its temporary WAV cleanup was verified. No raw microphone audio was recorded or automatically persisted. Creator identity remains Ali, with male/female model selection stable across turns.

Cloning remains optional and is not a Phase 9I completion gate. Its local request requires explicit approval; no automatic microphone reuse or cloud cloning path is enabled.
