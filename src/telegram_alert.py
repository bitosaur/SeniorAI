"""Send-only admin alerts to the user's personal Telegram chat.

Plain HTTP Bot API — no ``python-telegram-bot`` dependency yet; that comes
in phase 6 when we add the parents' send/receive flow.

Alerts are deduped: at most one message per ``key`` per calendar day. State
persists to ``logs/alert_state.json`` so restarts don't re-send.

Setup:
  1. Message @BotFather on Telegram → /newbot → save the token.
  2. Send any message to the new bot from your own account.
  3. Visit ``https://api.telegram.org/bot<TOKEN>/getUpdates`` → copy your
     numeric ``chat.id`` from the JSON.
  4. Put both in ``.env`` as TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID.

If either env var is empty, ``alert()`` is a no-op (logged at INFO) — so
the app still runs cleanly before you've set up the bot.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
from datetime import date
from pathlib import Path
from urllib import parse, request

log = logging.getLogger("seniorai.alert")


def _build_ssl_context() -> ssl.SSLContext:
    """Prefer the OS trust store so corporate-proxy root CAs work on dev
    machines; falls back to certifi's bundle if truststore isn't installed."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _build_ssl_context()


class _AdminAlerter:
    def __init__(self, bot_token: str, chat_id: str, state_path: Path) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.state_path = state_path
        self._lock = threading.Lock()

    def send(self, key: str, text: str) -> None:
        if not self.bot_token or not self.chat_id:
            log.info("telegram alert suppressed (bot not configured): key=%s", key)
            return
        with self._lock:
            today = date.today().isoformat()
            state = self._load()
            if state.get(key) == today:
                log.info("telegram alert deduped for today: key=%s", key)
                return
            if self._post(text):
                state[key] = today
                self._save(state)
                log.info("telegram alert sent: key=%s", key)

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.state_path.read_text()) or {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict[str, str]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))

    def _post(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        body = parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = request.Request(url, data=body, method="POST")
        try:
            with request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    log.error("telegram alert POST returned HTTP %s", resp.status)
                return ok
        except Exception:
            log.exception("telegram alert POST failed")
            return False


_alerter: _AdminAlerter | None = None


def configure(bot_token: str, chat_id: str, state_dir: Path) -> None:
    global _alerter
    _alerter = _AdminAlerter(bot_token, chat_id, state_dir / "alert_state.json")


def alert(key: str, text: str) -> None:
    """Send an admin alert, deduped to once per key per calendar day."""
    if _alerter is not None:
        _alerter.send(key, text)
    else:
        log.debug("telegram alert called before configure(): %s", key)


# ---------------------------------------------------------------------------
# Phase 7 — ERROR-level log forwarder
# ---------------------------------------------------------------------------
# Skip these loggers to prevent feedback loops: if the Telegram POST itself
# fails and logs an ERROR, we must not try to POST that error to Telegram.
_LOOP_GUARD_LOGGERS = {
    "seniorai.alert",
    "seniorai.telegram_bot",
}

# Telegram caps a single sendMessage payload at 4096 chars. Leave headroom
# for our own header.
_MAX_BODY_CHARS = 3500


class TelegramLogHandler(logging.Handler):
    """A ``logging.Handler`` that forwards ERROR-level records to the admin.

    Reuses the module-level ``_alerter`` for per-day dedup keyed by the
    record's origin (logger name + source line), so a recurring error at
    one site fires at most once per calendar day but a new error location
    still surfaces fresh. Dispatch is on a daemon thread so ``log.error``
    never blocks the caller on network I/O.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        if _alerter is None:
            return
        if record.name in _LOOP_GUARD_LOGGERS:
            return
        # Skip records that bubbled up from a loop-guarded logger's children.
        if any(record.name.startswith(g + ".") for g in _LOOP_GUARD_LOGGERS):
            return
        try:
            key = f"log:{record.name}:{record.module}:{record.lineno}"
            text = self._format_body(record)
            # Dispatch asynchronously — _alerter.send() acquires a lock and
            # does a synchronous HTTPS POST with a 10s timeout; we never
            # want log.error() to block that long.
            threading.Thread(
                target=_alerter.send,
                args=(key, text),
                daemon=True,
                name="TgLogDispatch",
            ).start()
        except Exception:  # noqa: BLE001 — never let logging raise
            self.handleError(record)

    @staticmethod
    def _format_body(record: logging.LogRecord) -> str:
        header = (
            f"SeniorAI [{record.levelname}] {record.name}\n"
            f"  at {record.module}:{record.lineno}"
        )
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        parts = [header, message]
        if record.exc_info:
            import traceback
            tb = "".join(traceback.format_exception(*record.exc_info))
            parts.append(tb)
        body = "\n".join(parts)
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "\n...[truncated]"
        return body


_log_handler: TelegramLogHandler | None = None


def attach_log_handler(logger: logging.Logger) -> None:
    """Attach the ERROR-level Telegram forwarder to ``logger``.

    Idempotent: safe to call more than once. Requires ``configure()`` to
    have been called first, otherwise the handler silently no-ops until
    it is configured (i.e. it's inert on a blank-token dev setup).
    """
    global _log_handler
    if _log_handler is not None:
        return
    _log_handler = TelegramLogHandler()
    logger.addHandler(_log_handler)
