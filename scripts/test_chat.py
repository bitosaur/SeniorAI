"""Single-turn PTT simulator. Records for N seconds, then runs the
full Gemini chat + TTS pipeline once.

    python scripts/test_chat.py             # default 5 sec
    python scripts/test_chat.py --sec 8
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import announcer
from src.chat import GeminiChat
from src.config import load_config
from src.logger import setup_logger
from src.mic import record_while_held
from src.tts import speak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=float, default=5.0, help="Recording duration (seconds)")
    args = ap.parse_args()

    cfg = load_config()
    setup_logger(cfg.log_dir, "INFO")
    log = logging.getLogger("test_chat")

    log.info("Speak for %.1fs after the ding...", args.sec)
    announcer.play("ding_start", block=True)

    stop = threading.Event()
    threading.Timer(args.sec, stop.set).start()
    result = record_while_held(
        stop_event=stop,
        sample_rate=cfg.mic_sample_rate,
        device_index=cfg.mic_device_index if cfg.mic_device_index >= 0 else None,
        min_sec=0.25,
        min_peak_rms=cfg.vad_energy_threshold * 0.5,
    )
    if not result.spoken:
        log.warning("No speech detected (peak_rms=%.4f).", result.peak_rms)
        announcer.play("say_again")
        return 1

    log.info("Sending to Gemini...")
    session = GeminiChat(cfg.gemini_api_key, cfg.gemini_model)
    reply = session.send_audio(result.audio_wav, cfg.tts_default_lang)
    log.info("Reply (%s): %s", reply.language, reply.text)
    speak(reply.text, reply.language, cfg.google_tts_api_key, cfg.tts_default_lang, cfg.tts_slow)
    announcer.play("ding_end", block=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
