from __future__ import annotations

import logging
import platform
from datetime import datetime
from pathlib import Path

import cv2

log = logging.getLogger("seniorai.camera")


class CameraError(RuntimeError):
    pass


def _open(index: int, width: int, height: int) -> cv2.VideoCapture:
    # DSHOW on Windows avoids a slow MSMF backend init.
    backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        raise CameraError(f"Could not open camera index {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def capture(
    out_dir: Path,
    index: int = 0,
    width: int = 1920,
    height: int = 1080,
    warmup_frames: int = 10,
    jpeg_quality: int = 92,
) -> Path:
    """Capture a single frame and save as JPEG. Returns saved path."""
    cap = _open(index, width, height)
    try:
        # Webcams need a few frames for auto-exposure/white-balance to settle.
        for _ in range(max(1, warmup_frames)):
            ok, frame = cap.read()
            if not ok:
                raise CameraError("Camera read failed during warmup")

        ok, frame = cap.read()
        if not ok or frame is None:
            raise CameraError("Camera returned no frame")

        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"scan_{ts}.jpg"
        ok = cv2.imwrite(
            str(path),
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )
        if not ok:
            raise CameraError(f"Failed to write {path}")
        log.info("Captured %s (%dx%d)", path, frame.shape[1], frame.shape[0])
        return path
    finally:
        cap.release()
