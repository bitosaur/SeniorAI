"""Unified audio playback. Used by announcer and tts modules.

Single-channel model: at most one sound plays at a time. Starting a new
playback pre-empts any current one. ``stop()`` can be called from any
thread to interrupt (used by F1 to cut Gemini's reply short).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("seniorai.audio")

_mixer_lock = threading.Lock()
_mixer_ready = False


def _ensure_mixer(force: bool = False) -> None:
    """Make sure pygame.mixer is open, re-initializing if the device was lost.

    The original design init'd the mixer once at startup and assumed it
    stayed alive for the process lifetime. That assumption breaks on the
    production box: when PipeWire/WirePlumber restarts (or the
    pipewire-pulse client drops), SDL's audio device is torn down
    underneath us. pygame keeps thinking it's initialized, so every later
    ``music.play()`` raises ``pygame.error: Audio device hasn't been
    opened`` and the appliance goes silent until a manual restart.

    We now key off the *live* ``pygame.mixer.get_init()`` state instead of
    a cached flag, and ``force=True`` tears the mixer down and rebuilds it
    from scratch — used by the retry path in ``play_audio_file`` after a
    play actually failed.
    """
    global _mixer_ready
    import pygame
    if force:
        try:
            pygame.mixer.quit()
        except pygame.error:
            pass
        _mixer_ready = False
    # get_init() returns None once SDL's device is gone; a cached flag can't
    # see that, so trust the live call.
    if pygame.mixer.get_init() is not None:
        _mixer_ready = True
        return
    pygame.mixer.init()
    _mixer_ready = True


def play_audio_file(path: Path, block: bool = True, loop: bool = False) -> None:
    """Play a .mp3 or .wav file. Pre-empts any current playback.

    block=True waits until playback ends (including if another thread
    calls ``stop()``). loop=True plays indefinitely — caller must
    call ``stop()`` to end it; ignored when block=True.

    Self-healing: if the audio device was lost while we were idle, the
    first ``play()`` raises ``pygame.error``. We catch it once, force a
    full mixer re-init, and retry. If the retry also fails we log and
    return rather than letting the exception crash the calling action
    thread (which is what used to page the admin's Telegram and leave the
    device mute).
    """
    if not path.exists():
        log.error("Audio file missing: %s", path)
        return

    import pygame
    loops = -1 if (loop and not block) else 0
    with _mixer_lock:
        for attempt in (1, 2):
            try:
                _ensure_mixer(force=(attempt == 2))
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                try:
                    pygame.mixer.music.unload()
                except pygame.error:
                    pass
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.play(loops=loops)
                break
            except pygame.error as exc:
                if attempt == 1:
                    log.warning(
                        "audio device lost (%s); re-initializing mixer and retrying",
                        exc,
                    )
                    continue
                log.error("audio playback failed after mixer re-init: %s", exc)
                return

    if block:
        while True:
            import pygame as _pg  # noqa: F811 — re-alias for clarity
            if not _pg.mixer.music.get_busy():
                break
            time.sleep(0.05)


def play_audio_bytes(data: bytes, suffix: str = ".mp3") -> None:
    """Play raw audio bytes by writing to a temp file then playing."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        play_audio_file(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def stop() -> None:
    """Stop any current playback. Safe to call from any thread, any time."""
    if not _mixer_ready:
        return
    import pygame
    try:
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
    except Exception:
        log.exception("audio stop failed")
