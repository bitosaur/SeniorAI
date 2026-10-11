"""Generate the pre-recorded audio assets shipped with the app.

Voice phrases are synthesized once via Google Cloud Text-to-Speech using
your ``GOOGLE_TTS_API_KEY``; tone dings are generated programmatically with
numpy (no API cost). Outputs go to ``assets/audio/``.

Run once after setting up ``.env``. Re-run any time the PHRASES dict below
changes. The generated files commit to git (see ``.gitignore`` exception).

    python scripts/generate_assets.py
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
from google.api_core.client_options import ClientOptions
from google.cloud import texttospeech as tts_api

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "audio"

# name -> (text, language_code, voice_name)
# All Bengali phrases use the Indian Bengali WaveNet female voice.
PHRASES: dict[str, tuple[str, str, str]] = {
    "namashkar":       ("নমস্কার",                              "bn-IN", "bn-IN-Wavenet-A"),
    "no_wifi":         ("ওয়াই-ফাই সংযোগ নেই",                  "bn-IN", "bn-IN-Wavenet-A"),
    "no_internet":     ("ইন্টারনেট সংযোগ নেই",                  "bn-IN", "bn-IN-Wavenet-A"),
    "new_message":     ("গুলুর কাছ থেকে নতুন বার্তা",           "bn-IN", "bn-IN-Wavenet-A"),
    "ask_message":     ("গুলুকে কি বলবো?",                      "bn-IN", "bn-IN-Wavenet-A"),
    "document_prompt": ("পড়ে শোনাবো?, না, গুলুকে পাঠাবো?",        "bn-IN", "bn-IN-Wavenet-A"),
    "document_sent":   ("গুলুকে পাঠিয়ে দিয়েছি",                   "bn-IN", "bn-IN-Wavenet-A"),
    "say_again":       ("আবার বলুন",                             "bn-IN", "bn-IN-Wavenet-A"),
}

# name -> (frequency_hz, duration_ms)
TONES: dict[str, tuple[float, int]] = {
    "ding_start":   (880.0, 180),   # A5, soft rising cue
    "ding_end":     (587.33, 180),  # D5, softer closing cue
}


def _synth(client: "tts_api.TextToSpeechClient",
           text: str, language: str, voice_name: str,
           out_path: Path) -> None:
    response = client.synthesize_speech(
        input=tts_api.SynthesisInput(text=text),
        voice=tts_api.VoiceSelectionParams(language_code=language, name=voice_name),
        audio_config=tts_api.AudioConfig(audio_encoding=tts_api.AudioEncoding.MP3),
    )
    out_path.write_bytes(response.audio_content)
    print(f"  wrote {out_path.name} ({len(response.audio_content):,} bytes)")


def _tone(out_path: Path, frequency: float, duration_ms: int,
          sample_rate: int = 22050, amplitude: float = 0.35,
          fade_ms: int = 25) -> None:
    n = int(sample_rate * duration_ms / 1000)
    fade = int(sample_rate * fade_ms / 1000)
    t = np.arange(n) / sample_rate
    wave_data = amplitude * np.sin(2.0 * np.pi * frequency * t)
    envelope = np.ones(n)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    wave_data *= envelope
    pcm = (wave_data * 32767).astype(np.int16)
    _write_wav(out_path, pcm, sample_rate)


def _write_wav(out_path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(out_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())
    print(f"  wrote {out_path.name} ({out_path.stat().st_size:,} bytes)")


def _shutter(out_path: Path, sample_rate: int = 22050) -> None:
    """Two quick high clicks mimicking a camera shutter mechanism."""
    click_dur_ms = 40
    gap_ms = 60
    total_ms = click_dur_ms * 2 + gap_ms
    n = int(sample_rate * total_ms / 1000)
    t = np.arange(n) / sample_rate
    audio = np.zeros(n)

    def add_click(start_ms: int, freq: float, amp: float) -> None:
        start = int(sample_rate * start_ms / 1000)
        length = int(sample_rate * click_dur_ms / 1000)
        end = min(n, start + length)
        local_t = np.arange(end - start) / sample_rate
        env = np.exp(-local_t * 120.0)  # sharp percussive decay
        audio[start:end] += amp * np.sin(2.0 * np.pi * freq * local_t) * env

    add_click(0, 2200.0, 0.45)
    add_click(click_dur_ms + gap_ms, 1700.0, 0.40)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    _write_wav(out_path, pcm, sample_rate)


def _thinking(out_path: Path, sample_rate: int = 22050) -> None:
    """Low-volume mechanical clock tick-tock — two clicks per second, loops
    seamlessly. Tick slightly higher pitch than tock so it sounds like an
    actual clock and not a repeating beep."""
    total_ms = 1000
    n = int(sample_rate * total_ms / 1000)
    audio = np.zeros(n)

    def add_click(start_ms: int, freq: float, amp: float,
                  length_ms: int = 18, decay_rate: float = 320.0) -> None:
        start = int(sample_rate * start_ms / 1000)
        length = int(sample_rate * length_ms / 1000)
        end = min(n, start + length)
        local_t = np.arange(end - start) / sample_rate
        env = np.exp(-local_t * decay_rate)
        audio[start:end] += amp * np.sin(2.0 * np.pi * freq * local_t) * env

    add_click(0, 5200.0, 0.10)   # tick
    add_click(500, 4200.0, 0.10) # tock

    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    _write_wav(out_path, pcm, sample_rate)


CUSTOM_TONES = {
    "shutter": _shutter,
    "thinking": _thinking,
}


def main() -> int:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()

    client = tts_api.TextToSpeechClient(
        client_options=ClientOptions(api_key=cfg.google_tts_api_key)
    )

    print(f"Writing assets to {ASSETS_DIR}")
    print("\nVoice phrases:")
    for name, (text, lang, voice) in PHRASES.items():
        _synth(client, text, lang, voice, ASSETS_DIR / f"{name}.mp3")

    print("\nTones:")
    for name, (freq, dur) in TONES.items():
        _tone(ASSETS_DIR / f"{name}.wav", freq, dur)
    for name, builder in CUSTOM_TONES.items():
        builder(ASSETS_DIR / f"{name}.wav")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
