"""Gemini vision: classify a captured photo and parse a spoken intent.

Two one-shot calls used by the Button-2 flow in ``main.py``:

* ``classify_image(path)`` → ``VisionResult`` with category
  (medicine / document / other) + a short spoken description in Bengali
  (or English for clearly-English items). Drives which branch runs next.
* ``parse_document_intent(wav_bytes)`` → "read" / "send" / "unknown".
  Used after ``document_prompt`` asks the user what to do with the scan.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from google import genai
from google.genai import types

log = logging.getLogger("seniorai.vision")


class VisionCategory(str, Enum):
    MEDICINE = "medicine"
    DOCUMENT = "document"
    OTHER = "other"


@dataclass
class VisionResult:
    category: VisionCategory
    description: str   # short sentence to speak aloud
    language: str      # ISO 639-1: "bn" or "en"
    image_path: Path


class DocumentIntent(str, Enum):
    READ = "read"
    SEND = "send"
    UNKNOWN = "unknown"


_CLASSIFY_PROMPT = """You are helping an elderly Bengali-speaking couple in India use a physical scanner device. They just pointed a camera at something and captured this photo. Classify it into EXACTLY one category:

- "medicine": pill blister, strip, bottle, tube, or medicine packaging with a brand name visible
- "document": a page of text — letter, bill, form, prescription letter, newspaper, book page, notice, envelope
- "other": anything else (a face, place, object, food, plant, appliance, etc.)

Rules for the spoken "description" field:
- Keep it to 1-2 short sentences suitable for an elderly person hearing it once.
- Default language: standard colloquial Bengali (Bangla, bn-IN), as spoken in West Bengal. NEVER use Assamese, Odia, or Hindi.
- If the item is clearly English-only and a direct Bengali rendering would sound unnatural (e.g., a pure English document), you MAY use English — set language accordingly.
- medicine: say the medicine name in its original script (usually Latin letters), then — only if clearly printed on the pack — the dosage or schedule, in Bengali.
- document: one short sentence naming the kind of document (e.g. "এটি একটি ইলেকট্রিক বিল।").
- other: one or two short sentences describing what is seen.

Return ONLY a JSON object, no code fences, no commentary, with this EXACT shape:
{"category":"medicine"|"document"|"other","description":"<spoken text>","language":"bn"|"en"}
"""


_INTENT_PROMPT = """The user is an elderly person in India who just heard the prompt:
"পড়ে শোনাবো, না গুলুকে পাঠাবো?" (Shall I read it aloud, or send it to Gulu?)

Listen to their short spoken reply (Bengali or English) and classify their intent into exactly one of:
- "read" — they want the document read aloud (Bangla: পড়ো / পড়ে শোনাও / শোনাও / English: read / read it / aloud)
- "send" — they want it sent/emailed/forwarded (Bangla: পাঠাও / পাঠিয়ে দাও / গুলুকে পাঠাও / ইমেইল / English: send / email / forward)
- "unknown" — anything else, unclear, silent, or ambiguous

Return ONLY a JSON object, no code fences, with this exact shape:
{"intent":"read"|"send"|"unknown"}
"""


def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def _parse_classification(raw: str, image_path: Path) -> VisionResult:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON classification: {raw!r}") from e

    cat_raw = str(data.get("category", "")).strip().lower()
    try:
        category = VisionCategory(cat_raw)
    except ValueError:
        log.warning("vision: unknown category %r, defaulting to OTHER", cat_raw)
        category = VisionCategory.OTHER

    description = str(data.get("description", "")).strip()
    language = str(data.get("language", "bn")).strip().lower()[:2] or "bn"
    return VisionResult(
        category=category,
        description=description,
        language=language,
        image_path=image_path,
    )


def classify_image(
    image_path: Path,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> VisionResult:
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    client = genai.Client(api_key=api_key)
    image_bytes = image_path.read_bytes()

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            _CLASSIFY_PROMPT,
        ],
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError("Empty response from Gemini classifier")

    result = _parse_classification(raw, image_path)
    log.info(
        "vision: classified %s as %s (lang=%s, %d chars)",
        image_path.name, result.category.value, result.language, len(result.description),
    )
    return result


def parse_document_intent(
    wav_bytes: bytes,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> DocumentIntent:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
            _INTENT_PROMPT,
        ],
    )
    raw = (response.text or "").strip()
    if not raw:
        log.warning("vision: empty intent response")
        return DocumentIntent.UNKNOWN

    try:
        data = json.loads(_strip_fences(raw))
        intent_raw = str(data.get("intent", "unknown")).strip().lower()
        return DocumentIntent(intent_raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("vision: could not parse intent response: %r", raw)
        return DocumentIntent.UNKNOWN
