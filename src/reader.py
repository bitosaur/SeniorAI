from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import types

log = logging.getLogger("seniorai.reader")


@dataclass
class ExtractedDocument:
    text: str
    language: str  # ISO 639-1, e.g. "en", "bn"


_PROMPT = """You are reading a document aloud for an elderly person.
Extract ALL visible text from this image exactly as it appears, preserving
reading order. Then identify the primary language.

Return ONLY valid JSON, no code fences, no commentary, with this exact shape:
{"language": "<ISO 639-1 code>", "text": "<full document text>"}

Rules:
- "language" must be a 2-letter lowercase ISO 639-1 code (e.g. "en", "bn", "hi").
- If multiple languages, pick the one with the most text.
- "text" is plain text with natural paragraph breaks as \\n.
- If no text is visible, return {"language":"en","text":""}.
"""


def _parse_response(raw: str) -> ExtractedDocument:
    # Gemini sometimes wraps JSON in ```json fences despite the prompt.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON response: {raw!r}") from e

    lang = str(data.get("language", "en")).strip().lower()[:2] or "en"
    text = str(data.get("text", "")).strip()
    return ExtractedDocument(text=text, language=lang)


def extract_text(
    image_path: Path,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> ExtractedDocument:
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    client = genai.Client(api_key=api_key)
    image_bytes = image_path.read_bytes()

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            _PROMPT,
        ],
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError("Empty response from Gemini")

    doc = _parse_response(raw)
    log.info("Extracted %d chars, language=%s", len(doc.text), doc.language)
    return doc
