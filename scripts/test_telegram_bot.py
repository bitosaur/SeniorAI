"""Exercises the parents <-> Gulu Telegram bot end-to-end.

Usage:

    # 1. Poll for inbound messages. Press Ctrl+C to stop.
    python scripts/test_telegram_bot.py poll

    # 2. Send a short synthetic voice message (1s sine tone as WAV) to Gulu.
    python scripts/test_telegram_bot.py send-voice

    # 3. Send a plain-text message to Gulu.
    python scripts/test_telegram_bot.py send-text "hello from the bot"

    # 4. Print current persisted state (update_offset + unread slot).
    python scripts/test_telegram_bot.py state

Expects TELEGRAM_BOT_TOKEN and TELEGRAM_GULU_CHAT_ID set in .env. Prints
diagnostic output; doesn't touch the main app's runtime.
"""
from __future__ import annotations

import io
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402
from src.telegram_bot import IncomingMessage, TelegramBot  # noqa: E402


def _synthetic_wav(duration_sec: float = 1.0, freq: float = 440.0,
                   sample_rate: int = 16000) -> bytes:
    n = int(sample_rate * duration_sec)
    t = np.arange(n) / sample_rate
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _make_bot() -> TelegramBot:
    cfg = load_config()
    if not cfg.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not cfg.telegram_gulu_chat_id:
        print("TELEGRAM_GULU_CHAT_ID not set in .env")
        sys.exit(1)
    return TelegramBot(
        cfg.telegram_bot_token,
        cfg.telegram_gulu_chat_id,
        cfg.log_dir,
    )


def _cmd_poll() -> int:
    bot = _make_bot()

    def on_message(msg: IncomingMessage) -> None:
        print(f"  [{time.strftime('%H:%M:%S')}] {msg.kind}: "
              f"id={msg.message_id} text={msg.text!r} audio={msg.audio_path!r}")

    bot.set_callback(on_message)
    bot.start()
    print(f"Polling Gulu ({bot._chat_id}). Ctrl+C to stop.")  # noqa: SLF001
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        bot.stop()
    return 0


def _cmd_send_voice() -> int:
    bot = _make_bot()
    bot.start()
    try:
        wav = _synthetic_wav(1.0, 440.0)
        ok = bot.send_voice(wav, title="SeniorAI test")
        print("sent" if ok else "failed")
        return 0 if ok else 1
    finally:
        bot.stop()


def _cmd_send_text(text: str) -> int:
    bot = _make_bot()
    bot.start()
    try:
        ok = bot.send_text(text)
        print("sent" if ok else "failed")
        return 0 if ok else 1
    finally:
        bot.stop()


def _cmd_state() -> int:
    cfg = load_config()
    state_path = cfg.log_dir / "telegram_state.json"
    if not state_path.exists():
        print(f"no state file at {state_path}")
        return 0
    print(json.dumps(json.loads(state_path.read_text()), indent=2))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "poll":
        return _cmd_poll()
    if cmd == "send-voice":
        return _cmd_send_voice()
    if cmd == "send-text":
        if len(argv) < 3:
            print("usage: send-text <message>")
            return 1
        return _cmd_send_text(" ".join(argv[2:]))
    if cmd == "state":
        return _cmd_state()
    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
