"""Smoke test: opens the webcam, grabs a frame, saves it.

Usage:
    python scripts/test_camera.py            # uses config from .env
    python scripts/test_camera.py 1          # override camera index
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.camera import capture
from src.config import load_config


def main() -> int:
    cfg = load_config()
    index = int(sys.argv[1]) if len(sys.argv) > 1 else cfg.camera_index
    path = capture(
        cfg.capture_dir,
        index=index,
        width=cfg.camera_width,
        height=cfg.camera_height,
        warmup_frames=cfg.camera_warmup_frames,
        jpeg_quality=cfg.camera_jpeg_quality,
    )
    print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
