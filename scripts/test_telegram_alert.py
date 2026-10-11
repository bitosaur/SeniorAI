"""Exercises the Telegram admin-alert paths.

    # Direct POST, bypassing dedupe (sanity check).
    python scripts/test_telegram_alert.py direct

    # Full alert() API with dedupe.
    python scripts/test_telegram_alert.py alert

    # Phase 7 — trigger an ERROR log record and verify the handler
    # forwards it. Runs log.error() and log.exception() once each.
    python scripts/test_telegram_alert.py log

Expects TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID set in .env.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import telegram_alert  # noqa: E402
from src.config import load_config  # noqa: E402
from src.telegram_alert import _AdminAlerter  # noqa: E402


def _direct(cfg) -> int:
    alerter = _AdminAlerter(
        cfg.telegram_bot_token,
        cfg.telegram_admin_chat_id,
        cfg.log_dir / "alert_state.json",
    )
    ok = alerter._post("SeniorAI test alert — you should see this once.")
    print("sent" if ok else "failed")
    return 0 if ok else 1


def _alert(cfg) -> int:
    telegram_alert.configure(
        cfg.telegram_bot_token, cfg.telegram_admin_chat_id, cfg.log_dir
    )
    telegram_alert.alert(
        "manual_test",
        "SeniorAI: manual alert() smoke test — deduped to once per day.",
    )
    print("sent (or deduped if already fired today)")
    return 0


def _log(cfg) -> int:
    telegram_alert.configure(
        cfg.telegram_bot_token, cfg.telegram_admin_chat_id, cfg.log_dir
    )
    root = logging.getLogger("seniorai")
    root.setLevel(logging.DEBUG)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    telegram_alert.attach_log_handler(root)

    test_log = logging.getLogger("seniorai.test_phase7")
    test_log.error("Phase-7 test: synthetic error (dedupe key per-line).")
    try:
        raise RuntimeError("Phase-7 synthetic exception")
    except RuntimeError:
        test_log.exception("Phase-7 test: synthetic exception with traceback.")

    # Dispatch is on daemon threads — give them a moment to complete before
    # the script exits.
    time.sleep(3)
    print("dispatched two log records to the handler (check Telegram).")
    return 0


def main(argv: list[str]) -> int:
    cfg = load_config()
    if not cfg.telegram_bot_token or not cfg.telegram_admin_chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_CHAT_ID not set in .env")
        return 1
    cmd = argv[1] if len(argv) > 1 else "direct"
    if cmd == "direct":
        return _direct(cfg)
    if cmd == "alert":
        return _alert(cfg)
    if cmd == "log":
        return _log(cfg)
    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
