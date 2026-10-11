"""Gemini audio chat session.

One turn = one call to ``GeminiChat.send_audio(wav_bytes)``. The session
preserves conversation context across turns so the model can follow up.
Caller (main.py) drives turns via push-to-talk.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from google import genai
from google.genai import types

log = logging.getLogger("seniorai.chat")


_SYSTEM_INSTRUCTION = (
    "You are a warm, patient voice assistant for an elderly couple in India. "
    "They speak ONLY two languages: English or standard Bengali (Bangla, bn-IN). "
    "Detect which of these two the user spoke and reply in the SAME language. "
    "CRITICAL: Never reply in Assamese, Hindi, Odia, or any other language. "
    "If the audio sounds ambiguous or you cannot tell, default to standard "
    "Bengali (Bangla) — not Assamese. Use modern colloquial Bangla as spoken "
    "in West Bengal, India. "
    "Keep replies short and natural — at most two short sentences. "
    "Be kind, clear, and conversational. "
    'Return ONLY a JSON object, no code fences, exactly: '
    '{"language":"en" or "bn","text":"<your spoken reply>"}'
)


@dataclass
class ChatReply:
    text: str
    language: str


def _parse_reply(raw: str, default_lang: str) -> ChatReply:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        text = str(data.get("text", "")).strip()
        lang = str(data.get("language", default_lang)).strip().lower()[:2] or default_lang
        return ChatReply(text=text, language=lang)
    except json.JSONDecodeError:
        log.warning("chat: non-JSON reply, using raw text")
        return ChatReply(text=cleaned, language=default_lang)


class GeminiChat:
    """Persistent multimodal chat session."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._chat = self._client.chats.create(
            model=model,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
            ),
        )

    def send_audio(self, wav_bytes: bytes, default_lang: str = "en") -> ChatReply:
        response = self._chat.send_message(
            [types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")]
        )
        raw = (response.text or "").strip()
        if not raw:
            raise RuntimeError("Empty response from Gemini")
        return _parse_reply(raw, default_lang)

    def send_image(
        self,
        image_bytes: bytes,
        prompt: str,
        default_lang: str = "en",
        mime_type: str = "image/jpeg",
    ) -> ChatReply:
        """Inject an image + text prompt into the chat. Used for vision-seeded
        sessions and for showing a follow-up photo mid-chat. The image stays
        in session history so subsequent audio turns can refer to it."""
        response = self._chat.send_message([
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ])
        raw = (response.text or "").strip()
        if not raw:
            raise RuntimeError("Empty response from Gemini")
        return _parse_reply(raw, default_lang)

    def send_audio_with_image(
        self,
        wav_bytes: bytes,
        image_bytes: bytes,
        default_lang: str = "en",
        mime_type: str = "image/jpeg",
    ) -> ChatReply:
        """One-shot: audio question plus a fresh image (mid-chat photo)."""
        response = self._chat.send_message([
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
        ])
        raw = (response.text or "").strip()
        if not raw:
            raise RuntimeError("Empty response from Gemini")
        return _parse_reply(raw, default_lang)
