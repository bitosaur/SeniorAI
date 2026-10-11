"""Microphone capture with simple RMS-energy voice activity detection.

Records a single utterance: waits for speech onset, then captures until
silence_sec of continuous silence (or max_utterance_sec). Returns a WAV
byte blob suitable for sending to Gemini.
"""
from __future__ import annotations

import io
import logging
import threading
import wave
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("seniorai.mic")

_BLOCK_SEC = 0.05  # 50 ms per VAD tick


@dataclass
class RecordResult:
    audio_wav: bytes  # empty if no speech captured
    spoken: bool
    duration_sec: float
    peak_rms: float


def _pcm_to_wav(pcm_int16: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


def record_until_silence(
    sample_rate: int = 16000,
    silence_sec: float = 1.2,
    max_wait_sec: float = 30.0,
    max_utterance_sec: float = 30.0,
    min_utterance_sec: float = 0.3,
    energy_threshold: float = 0.01,
    device_index: int | None = None,
    cancel_event: threading.Event | None = None,
) -> RecordResult:
    """Record a single utterance. Returns empty audio if no speech detected."""
    import sounddevice as sd  # lazy: heavy PortAudio init

    blocksize = max(1, int(sample_rate * _BLOCK_SEC))
    silence_blocks = max(1, int(silence_sec / _BLOCK_SEC))
    max_wait_blocks = max(1, int(max_wait_sec / _BLOCK_SEC))
    max_utter_blocks = max(1, int(max_utterance_sec / _BLOCK_SEC))
    min_utter_blocks = max(1, int(min_utterance_sec / _BLOCK_SEC))

    frames: list[np.ndarray] = []
    spoken = False
    silent_count = 0
    pre_blocks = 0
    utter_blocks = 0
    peak_rms = 0.0

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    stream_kwargs: dict = dict(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
    )
    if device_index is not None and device_index >= 0:
        stream_kwargs["device"] = device_index

    with sd.InputStream(**stream_kwargs) as stream:
        while True:
            if _cancelled():
                return RecordResult(b"", False, 0.0, peak_rms)
            data, _overflow = stream.read(blocksize)
            block = data[:, 0] if data.ndim == 2 else data
            rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
            if rms > peak_rms:
                peak_rms = rms

            if spoken:
                frames.append(block.copy())
                utter_blocks += 1
                if rms > energy_threshold:
                    silent_count = 0
                else:
                    silent_count += 1
                    if (
                        silent_count >= silence_blocks
                        and utter_blocks >= min_utter_blocks
                    ):
                        break
                if utter_blocks >= max_utter_blocks:
                    log.info("mic: max utterance length hit")
                    break
            else:
                pre_blocks += 1
                if rms > energy_threshold:
                    spoken = True
                    frames.append(block.copy())
                    utter_blocks = 1
                    silent_count = 0
                elif pre_blocks >= max_wait_blocks:
                    return RecordResult(b"", False, 0.0, peak_rms)

    audio_f32 = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    audio_int16 = np.clip(audio_f32 * 32767.0, -32768, 32767).astype(np.int16)
    duration = len(audio_int16) / float(sample_rate)
    wav_bytes = _pcm_to_wav(audio_int16, sample_rate)
    log.info(
        "mic: captured %.2fs utterance, peak_rms=%.4f, threshold=%.4f",
        duration, peak_rms, energy_threshold,
    )
    return RecordResult(wav_bytes, True, duration, peak_rms)


def record_while_held(
    stop_event: threading.Event,
    sample_rate: int = 16000,
    device_index: int | None = None,
    min_sec: float = 0.25,
    min_peak_rms: float = 0.01,
    max_sec: float = 60.0,
) -> RecordResult:
    """Record continuously until ``stop_event`` is set (push-to-talk).

    ``spoken`` is True only if the recording is long enough and loud enough
    to plausibly contain speech — otherwise treat it as an accidental tap.
    """
    import sounddevice as sd

    blocksize = max(1, int(sample_rate * _BLOCK_SEC))
    max_blocks = max(1, int(max_sec / _BLOCK_SEC))

    frames: list[np.ndarray] = []
    peak_rms = 0.0
    blocks = 0

    stream_kwargs: dict = dict(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
    )
    if device_index is not None and device_index >= 0:
        stream_kwargs["device"] = device_index

    with sd.InputStream(**stream_kwargs) as stream:
        while not stop_event.is_set() and blocks < max_blocks:
            data, _ = stream.read(blocksize)
            block = data[:, 0] if data.ndim == 2 else data
            frames.append(block.copy())
            rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
            if rms > peak_rms:
                peak_rms = rms
            blocks += 1

    audio_f32 = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    audio_int16 = np.clip(audio_f32 * 32767.0, -32768, 32767).astype(np.int16)
    duration = len(audio_int16) / float(sample_rate)
    wav_bytes = _pcm_to_wav(audio_int16, sample_rate)
    spoken = duration >= min_sec and peak_rms >= min_peak_rms
    log.info(
        "mic: PTT captured %.2fs, peak_rms=%.4f, spoken=%s",
        duration, peak_rms, spoken,
    )
    return RecordResult(wav_bytes, spoken, duration, peak_rms)


def list_devices() -> list[dict]:
    import sounddevice as sd
    return list(sd.query_devices())
