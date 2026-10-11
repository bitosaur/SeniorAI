"""Diagnostic for the Button-2 vision flow, in isolation from the main app.

Usage:
  python scripts/test_vision.py                 # capture now + classify
  python scripts/test_vision.py --image PATH    # classify an existing file
  python scripts/test_vision.py --speak         # also TTS the description

Does NOT touch the 30s medicine window, document-prompt flow, or chat
session seeding — this is just to check classification quality.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import camera, vision  # noqa: E402
from src.config import load_config  # noqa: E402
from src.tts import speak  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="Skip camera capture and use this JPEG")
    parser.add_argument("--speak", action="store_true", help="TTS-speak the description")
    args = parser.parse_args()

    cfg = load_config()

    if args.image:
        if not args.image.exists():
            print(f"ERROR: {args.image} not found", file=sys.stderr)
            return 2
        image_path = args.image
        print(f"Using existing image: {image_path}")
    else:
        print("Capturing photo…")
        image_path = camera.capture(
            cfg.capture_dir,
            index=cfg.camera_index,
            width=cfg.camera_width,
            height=cfg.camera_height,
            warmup_frames=cfg.camera_warmup_frames,
            jpeg_quality=cfg.camera_jpeg_quality,
        )
        print(f"  saved → {image_path}")

    print("Classifying with Gemini…")
    result = vision.classify_image(image_path, cfg.gemini_api_key, cfg.gemini_model)
    print(f"\nCategory:    {result.category.value}")
    print(f"Language:    {result.language}")
    print(f"Description: {result.description}\n")

    if args.speak and result.description:
        print("Speaking description…")
        speak(
            result.description,
            result.language,
            cfg.google_tts_api_key,
            cfg.tts_default_lang,
            cfg.tts_slow,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
