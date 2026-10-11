"""Smoke test: extract text from an image via Gemini and read it aloud.

Usage:
    python scripts/test_read.py                        # use latest capture
    python scripts/test_read.py path/to/image.jpg      # use a specific file
    python scripts/test_read.py --no-tts               # extract only, skip TTS
    python scripts/test_read.py path.jpg --no-tts      # combine
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.reader import extract_text
from src.tts import speak


def _latest_capture(capture_dir: Path) -> Path:
    jpegs = sorted(capture_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    if not jpegs:
        raise SystemExit(
            f"No .jpg files in {capture_dir}. "
            "Run scripts/test_camera.py first, or pass an image path."
        )
    return jpegs[-1]


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--no-tts"]
    skip_tts = "--no-tts" in sys.argv[1:]

    cfg = load_config()

    if args:
        image = Path(args[0]).resolve()
        if not image.exists():
            raise SystemExit(f"Not found: {image}")
    else:
        image = _latest_capture(cfg.capture_dir)

    print(f"Reading {image.name}")
    print(f"  model: {cfg.gemini_model}")

    doc = extract_text(image, api_key=cfg.gemini_api_key, model=cfg.gemini_model)

    print(f"\n--- language: {doc.language} ---")
    print(doc.text if doc.text else "(no text detected)")
    print("--- end ---\n")

    if skip_tts:
        print("TTS skipped (--no-tts).")
        return 0

    if not doc.text.strip():
        print("Nothing to speak.")
        return 0

    print("Speaking...")
    speak(
        doc.text,
        language=doc.language,
        api_key=cfg.google_tts_api_key,
        default_language=cfg.tts_default_lang,
        slow=cfg.tts_slow,
    )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
