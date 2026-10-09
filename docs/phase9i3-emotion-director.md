# Phase 9I.3: Emotion Director Foundation

FRIDAY now has backend-neutral semantic emotion values, deterministic delivery
directions, a Piper prosody approximation map, explicit backend capability
metadata, and a minimal future emotional-TTS protocol. Emotion intent, prosody,
and vocal events remain separate concepts.

`EmotionSynthesisMetadata` records requested intent separately from the
capability-resolved emotion and prosody request. It does not prove that audio
generation completed. For Piper, `applied_emotion` is empty and
`true_emotion_applied` is always false; a mapped `SpeechStyle` only records the
prosody approximation.

## Current support

- Semantic emotional intent and deterministic delivery direction.
- Piper prosody approximation through the existing `SpeechStyle` values.
- A backend-neutral style-instruction builder and future backend contract.
- Piper capability metadata: true emotion `false`, prosody `true`, vocal events
  `false`, prompted style `false`.

The Phase 9I.3 foundation does not select an emotion in the live voice flow.
Conversation context and personality-based emotion selection remain in Phase 9B.
Piper does not consume the generated style instructions, and prosody
approximation is not emotional timbre.

The later integration boundary is:

`conversation context -> personality engine -> TrueEmotion -> EmotionDirector -> VoiceRouter -> backend`

## Unsupported today

FRIDAY has no verified local TTS backend with true emotional timbre. No new TTS
model, Myraa runtime, cloud TTS service, or dependency was added. Kokoro and
Supertonic 3 remain unintegrated benchmark assets.
