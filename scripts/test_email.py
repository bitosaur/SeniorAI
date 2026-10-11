"""Smoke test: send an image via Gmail SMTP.

Usage:
    python scripts/test_email.py                       # send latest capture
    python scripts/test_email.py path/to/image.jpg     # send a specific file
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.emailer import send_scan


def _latest_capture(capture_dir: Path) -> Path:
    jpegs = sorted(capture_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    if not jpegs:
        raise SystemExit(
            f"No .jpg files in {capture_dir}. "
            "Run scripts/test_camera.py first, or pass an image path."
        )
    return jpegs[-1]


def main() -> int:
    cfg = load_config()

    if len(sys.argv) > 1:
        image = Path(sys.argv[1]).resolve()
        if not image.exists():
            raise SystemExit(f"Not found: {image}")
    else:
        image = _latest_capture(cfg.capture_dir)

    print(f"Sending {image.name}")
    print(f"  from: {cfg.gmail_user}")
    print(f"  to:   {cfg.email_recipient}")

    send_scan(
        gmail_user=cfg.gmail_user,
        gmail_app_password=cfg.gmail_app_password,
        recipient=cfg.email_recipient,
        image_path=image,
        subject=f"[SeniorAI test] {image.name}",
    )
    print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
