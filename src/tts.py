from __future__ import annotations

import logging

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPIError
from google.cloud import texttospeech as tts_api

from .audio_player import play_audio_bytes

log = logging.getLogger("seniorai.tts")

# ISO 639-1 -> (BCP-47 language code, voice name), split by gender.
# Indian-accent voices for EN/HI since parents are in India.
# Voice catalog: https://cloud.google.com/text-to-speech/docs/voices
# Convention: Google IN voices use -A/-D for female, -B/-C for male.
_FEMALE_VOICES: dict[str, tuple[str, str]] = {
    "en": ("en-IN", "en-IN-Neural2-A"),
    "bn": ("bn-IN", "bn-IN-Wavenet-A"),
    "hi": ("hi-IN", "hi-IN-Neural2-A"),
    "ta": ("ta-IN", "ta-IN-Wavenet-A"),
    "te": ("te-IN", "te-IN-Standard-A"),
    "mr": ("mr-IN", "mr-IN-Wavenet-A"),
    "gu": ("gu-IN", "gu-IN-Wavenet-A"),
    "kn": ("kn-IN", "kn-IN-Wavenet-A"),
    "ml": ("ml-IN", "ml-IN-Wavenet-A"),
    "pa": ("pa-IN", "pa-IN-Wavenet-A"),
    "ur": ("ur-IN", "ur-IN-Wavenet-A"),
}

_MALE_VOICES: dict[str, tuple[str, str]] = {
    "en": ("en-IN", "en-IN-Neural2-B"),
    "bn": ("bn-IN", "bn-IN-Wavenet-B"),
    "hi": ("hi-IN", "hi-IN-Neural2-B"),
    "ta": ("ta-IN", "ta-IN-Wavenet-B"),
    "te": ("te-IN", "te-IN-Standard-B"),
    "mr": ("mr-IN", "mr-IN-Wavenet-B"),
    "gu": ("gu-IN", "gu-IN-Wavenet-B"),
    "kn": ("kn-IN", "kn-IN-Wavenet-B"),
    "ml": ("ml-IN", "ml-IN-Wavenet-B"),
    "pa": ("pa-IN", "pa-IN-Wavenet-B"),
    "ur": ("ur-IN", "ur-IN-Wavenet-B"),
}

# Kept for backwards compatibility; defaults to female.
_VOICE_MAP = _FEMALE_VOICES

# Cloud TTS caps a single request at 5000 bytes of input. Chunk below that
# with a safety margin and stitch audio together.
_MAX_CHARS_PER_CHUNK = 4000


def _resolve_voice(
    detected: str, default: str, gender: str = "female"
) -> tuple[str, str]:
    table = _MALE_VOICES if gender.lower() == "male" else _FEMALE_VOICES
    d = (detected or "").lower()
    if d in table:
        return table[d]
    if default in table:
        return table[default]
    return table["en"]


def _chunk_text(text: str, max_chars: int = _MAX_CHARS_PER_CHUNK) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in (p for p in text.split("\n") if p.strip()):
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        # Paragraph alone exceeds max; hard-split it.
        while len(para) > max_chars:
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def _synthesize(
    client: "tts_api.TextToSpeechClient",
    text: str,
    language_code: str,
    voice_name: str,
    speaking_rate: float,
) -> bytes:
    synth_input = tts_api.SynthesisInput(text=text)
    voice = tts_api.VoiceSelectionParams(language_code=language_code, name=voice_name)
    audio_config = tts_api.AudioConfig(
        audio_encoding=tts_api.AudioEncoding.MP3,
        speaking_rate=speaking_rate,
    )
    response = client.synthesize_speech(
        input=synth_input, voice=voice, audio_config=audio_config
    )
    return response.audio_content


def speak(
    text: str,
    language: str,
    api_key: str,
    default_language: str = "en",
    slow: bool = False,
    voice_gender: str = "female",
) -> None:
    if not text.strip():
        log.info("TTS skipped: empty text")
        return

    lang_code, voice_name = _resolve_voice(language, default_language, voice_gender)
    speaking_rate = 0.85 if slow else 1.0
    chunks = _chunk_text(text)
    log.info(
        "Speaking %d chars in %s (%s), %d chunk(s)",
        len(text), lang_code, voice_name, len(chunks),
    )

    client = tts_api.TextToSpeechClient(client_options=ClientOptions(api_key=api_key))

    try:
        audio = b"".join(
            _synthesize(client, chunk, lang_code, voice_name, speaking_rate)
            for chunk in chunks
        )
    except GoogleAPIError as e:
        log.error("Cloud TTS failed: %s", e)
        raise

    play_audio_bytes(audio, suffix=".mp3")
