"""Plays pre-recorded audio assets (voice phrases + tone dings).

Assets live in ``assets/audio/`` and are produced by
``scripts/generate_assets.py``. If an asset is missing, we log and no-op
rather than crash — startup will still proceed.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .audio_player import play_audio_file
from .audio_player import stop as stop_audio  # re-export for convenience

log = logging.getLogger("seniorai.announcer")

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "audio"

# Known assets. Values are preferred extensions to probe in order.
_ASSETS: dict[str, tuple[str, ...]] = {
    # Voice phrases (Bengali, generated via Cloud TTS)
    "namashkar": (".mp3",),
    "no_wifi": (".mp3",),
    "no_internet": (".mp3",),
    "new_message": (".mp3",),
    "ask_message": (".mp3",),
    "document_prompt": (".mp3",),
    "document_sent": (".mp3",),
    "say_again": (".mp3",),
    # Tones (generated via numpy sine-wave)
    "ding_start": (".wav",),
    "ding_end": (".wav",),
    "shutter": (".wav",),
    "thinking": (".wav",),
}


def play(name: str, block: bool = True, loop: bool = False) -> None:
    """Play a named asset. Logs a warning if the file is missing.

    loop=True keeps playing until ``stop_audio()`` is called; only valid
    with block=False.
    """
    extensions = _ASSETS.get(name, (".mp3", ".wav"))
    for ext in extensions:
        path = ASSETS_DIR / f"{name}{ext}"
        if path.exists():
            log.debug("playing asset: %s (loop=%s)", path.name, loop)
            play_audio_file(path, block=block, loop=loop)
            return
    log.warning("asset not found: %s (run scripts/generate_assets.py)", name)


def missing_assets() -> list[str]:
    """Return names of assets not present on disk."""
    missing = []
    for name, exts in _ASSETS.items():
        if not any((ASSETS_DIR / f"{name}{ext}").exists() for ext in exts):
            missing.append(name)
    return missing
