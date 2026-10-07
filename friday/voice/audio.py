"""Optional microphone and speaker access with bounded, memory-only capture."""

from __future__ import annotations

import threading
from typing import Callable

from friday.voice.models import AudioDevice, DeviceCatalog


class AudioUnavailable(RuntimeError):
    """Raised when local audio devices or the optional sound backend are absent."""


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as error:
        raise AudioUnavailable("SOUNDDEVICE_INSTALL_REQUIRED") from error
    return sd


def discover_devices(*, sounddevice_module=None) -> DeviceCatalog:
    """Enumerate input/output endpoints without making FRIDAY startup depend on them."""
    try:
        sd = sounddevice_module or _sounddevice()
        raw_devices = sd.query_devices()
        defaults = sd.default.device
        inputs: list[AudioDevice] = []
        outputs: list[AudioDevice] = []
        by_index: dict[int, AudioDevice] = {}
        for index, item in enumerate(raw_devices):
            input_channels = int(item.get("max_input_channels", 0) or 0)
            output_channels = int(item.get("max_output_channels", 0) or 0)
            rate = int(float(item.get("default_samplerate", 0) or 0))
            name = str(item.get("name", f"Audio device {index}"))
            if input_channels > 0:
                device = AudioDevice(index, name, input_channels, rate)
                inputs.append(device)
                by_index[index] = device
            if output_channels > 0:
                device = AudioDevice(index, name, output_channels, rate)
                outputs.append(device)
                by_index[index] = device
        default_input_index = int(defaults[0]) if defaults and defaults[0] is not None else -1
        default_output_index = int(defaults[1]) if defaults and defaults[1] is not None else -1
        default_input = next((item for item in inputs if item.index == default_input_index), None)
        default_output = next((item for item in outputs if item.index == default_output_index), None)
        available = bool(inputs and outputs)
        return DeviceCatalog(
            inputs=tuple(inputs), outputs=tuple(outputs), default_input=default_input,
            default_output=default_output, available=available,
            error_code=None if available else "AUDIO_DEVICE_UNAVAILABLE",
        )
    except Exception as error:
        return DeviceCatalog(available=False, error_code=type(error).__name__)


class AudioInput:
    """PortAudio callback adapter. Frames are copied once and handed to the caller."""

    def __init__(self, *, device: int | None = None, frame_ms: int = 100) -> None:
        self.device = device
        self.frame_ms = frame_ms
        self.device_name: str | None = None
        self.sample_rate = 16_000
        self._stream = None
        self._lock = threading.RLock()

    def start(self, callback: Callable[[object, int], None]) -> None:
        with self._lock:
            if self._stream is not None:
                return
            sd = _sounddevice()
            catalog = discover_devices(sounddevice_module=sd)
            selected = next(
                (item for item in catalog.inputs if item.index == self.device),
                catalog.default_input if self.device is None else None,
            )
            if selected is None:
                raise AudioUnavailable("MICROPHONE_UNAVAILABLE")
            self.device_name = selected.name
            self.sample_rate = selected.sample_rate or 16_000
            blocksize = max(1, int(self.sample_rate * self.frame_ms / 1000))

            def on_audio(indata, _frames, _time_info, status) -> None:
                if status:
                    return
                try:
                    callback(indata[:, 0].copy(), self.sample_rate)
                except Exception:
                    # PortAudio callbacks must not allow consumer failures to tear down input.
                    return

            stream = sd.InputStream(
                device=selected.index, samplerate=self.sample_rate, channels=1,
                blocksize=blocksize, dtype="float32", callback=on_audio,
            )
            stream.start()
            self._stream = stream

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()


class AudioOutput:
    """Interruptible mono playback for local TTS output."""

    def __init__(self, *, device: int | None = None) -> None:
        self.device = device
        self.device_name: str | None = None
        self._stream = None
        self._lock = threading.RLock()

    def play(
        self, samples, sample_rate: int, cancel_event: threading.Event,
        *, on_started: Callable[[], None] | None = None,
    ) -> None:
        sd = _sounddevice()
        catalog = discover_devices(sounddevice_module=sd)
        selected = next(
            (item for item in catalog.outputs if item.index == self.device),
            catalog.default_output if self.device is None else None,
        )
        if selected is None:
            raise AudioUnavailable("SPEAKER_UNAVAILABLE")
        self.device_name = selected.name
        if selected.sample_rate > 0 and selected.sample_rate != sample_rate:
            import numpy as np

            output_size = max(1, round(len(samples) * selected.sample_rate / sample_rate))
            positions = np.arange(output_size, dtype=np.float64) * sample_rate / selected.sample_rate
            samples = np.clip(
                np.interp(positions, np.arange(len(samples), dtype=np.float64), samples),
                -32768, 32767,
            ).astype(np.int16)
            sample_rate = selected.sample_rate
        position = 0

        def on_audio(outdata, frames, _time_info, _status) -> None:
            nonlocal position
            outdata.fill(0)
            if cancel_event.is_set():
                raise sd.CallbackStop
            end = min(position + frames, len(samples))
            count = end - position
            if count:
                outdata[:count, 0] = samples[position:end]
                position = end
            if position >= len(samples):
                raise sd.CallbackStop

        stream = sd.OutputStream(
            device=selected.index, samplerate=sample_rate, channels=1,
            dtype="int16", blocksize=0, callback=on_audio,
        )
        with self._lock:
            self._stream = stream
        try:
            stream.start()
            if on_started is not None:
                on_started()
            while not cancel_event.wait(0.02) and position < len(samples):
                pass
        finally:
            with self._lock:
                self._stream = None
            try:
                stream.stop()
            except Exception:
                pass
            stream.close()

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass


__all__ = ["AudioInput", "AudioOutput", "AudioUnavailable", "discover_devices"]
