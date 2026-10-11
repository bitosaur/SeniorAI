"""Two-way Telegram bot for the parents <-> Gulu chat channel.

Separate from ``telegram_alert.py`` (which is a send-only admin alerter
keyed to ``TELEGRAM_ADMIN_CHAT_ID``). This module talks to exactly one
whitelisted chat — ``TELEGRAM_GULU_CHAT_ID`` — and does two things:

* **Inbound (long-poll)**: A background thread calls ``getUpdates`` with
  a long timeout. Voice notes are downloaded to
  ``logs/telegram_inbox/`` and text messages are kept in-memory; either
  kind fires ``on_new_message`` so ``main.py`` can announce "new message
  from Gulu" and flip the ``has_unread_telegram`` flag.
* **Outbound**: ``send_voice(wav_bytes)`` sends the parents' recorded
  reply to Gulu via the ``sendAudio`` endpoint (works for WAV; the
  recipient sees a playable audio attachment). ``send_text`` is there
  for diagnostics.

Persistence — ``logs/telegram_state.json`` holds:

* ``update_offset``: last-acked Telegram ``update_id`` + 1, so restarts
  don't replay old updates.
* ``unread``: the single "message waiting to be heard" slot. Only one
  unread at a time — new inbound voice overwrites the previous unread
  (matching the product spec: F3 plays back the most recent message).

The long-poll thread re-uses a single ``httpx.Client`` and survives
transient network errors (sleeps and retries). HTTPS uses the OS trust
store via ``truststore`` so the user's corporate-proxy dev laptop works.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("seniorai.telegram_bot")


# ---------------------------------------------------------------------------
# SSL (OS trust store for corporate-proxy dev boxes; falls back to certifi)
# ---------------------------------------------------------------------------
def _build_ssl_context() -> ssl.SSLContext:
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _build_ssl_context()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@dataclass
class IncomingMessage:
    kind: str            # "voice" or "text"
    text: str            # text body (empty for untranscribed voice)
    audio_path: str      # absolute path to downloaded voice file ("" for text)
    message_id: int
    received_at: float   # epoch seconds


# Long-poll timeout (seconds) passed to Telegram. The HTTP client adds
# ~5s of margin so a stuck socket eventually surfaces as a read error.
_LONG_POLL_SEC = 25
_HTTP_TIMEOUT = _LONG_POLL_SEC + 5


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class TelegramBot:
    """Two-way bot keyed to a single whitelisted chat.

    Instantiate with the bot token, the whitelisted chat id (the family
    member's chat), a directory for persistence + downloaded voice
    files, and a callback for new inbound messages.

    ``start()`` launches the long-poll thread. Safe to call even when
    the bot token / chat id are empty — in that case the bot is inert
    (all methods no-op and long-poll never starts). ``stop()`` joins
    the thread cleanly.
    """

    def __init__(
        self,
        bot_token: str,
        gulu_chat_id: str,
        state_dir: Path,
        on_new_message: Callable[[IncomingMessage], None] | None = None,
    ) -> None:
        self._token = (bot_token or "").strip()
        self._chat_id = (gulu_chat_id or "").strip()
        self._state_dir = state_dir
        self._inbox_dir = state_dir / "telegram_inbox"
        self._state_path = state_dir / "telegram_state.json"
        self._callback = on_new_message

        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        self._update_offset: int = 0
        self._unread: IncomingMessage | None = None

        # One httpx client reused for the entire process life.
        self._client: httpx.Client | None = None

        self._load_state()

    # -- public API ------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def set_callback(
        self, on_new_message: Callable[[IncomingMessage], None] | None
    ) -> None:
        """Late-bind the new-message callback (App is built after the bot)."""
        self._callback = on_new_message

    def start(self) -> None:
        if not self.configured:
            log.info("telegram_bot: not configured (token/chat_id missing); long-poll disabled")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            verify=_SSL_CTX,
            timeout=httpx.Timeout(_HTTP_TIMEOUT, connect=10.0),
            base_url=f"https://api.telegram.org/bot{self._token}",
        )
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="TelegramBot", daemon=True
        )
        self._thread.start()
        log.info("telegram_bot: long-poll started (chat_id=%s, offset=%d)",
                 self._chat_id, self._update_offset)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=_HTTP_TIMEOUT + 2)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        log.info("telegram_bot: stopped")

    # -- unread state ----------------------------------------------------
    def has_unread(self) -> bool:
        with self._lock:
            return self._unread is not None

    def peek_unread(self) -> IncomingMessage | None:
        with self._lock:
            return self._unread

    def mark_read(self) -> None:
        with self._lock:
            if self._unread is None:
                return
            log.info("telegram_bot: marking message_id=%d as read",
                     self._unread.message_id)
            self._unread = None
            self._save_state()

    # -- outbound --------------------------------------------------------
    def send_voice(self, wav_bytes: bytes, title: str = "Voice") -> bool:
        """Send a recorded WAV to Gulu via ``sendAudio``.

        Returns True on HTTP 200. Uses ``sendAudio`` (not ``sendVoice``)
        because ``sendVoice`` requires OGG/OPUS; WAV gets rejected. The
        recipient sees a playable audio attachment in chat.
        """
        if not self.configured or self._client is None:
            log.info("telegram_bot.send_voice: skipped (not configured)")
            return False
        try:
            resp = self._client.post(
                "/sendAudio",
                data={"chat_id": self._chat_id, "title": title},
                files={"audio": ("voice.wav", wav_bytes, "audio/wav")},
                timeout=60.0,
            )
        except httpx.HTTPError:
            log.exception("telegram_bot.send_voice: POST failed")
            return False
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        if not ok:
            log.error("telegram_bot.send_voice: HTTP %s body=%s",
                      resp.status_code, resp.text[:300])
        return ok

    def send_text(
        self,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> bool:
        if not self.configured or self._client is None:
            return False
        data: dict = {"chat_id": self._chat_id, "text": text}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
            # Don't fail the send if the original message got deleted.
            data["allow_sending_without_reply"] = "true"
        try:
            resp = self._client.post(
                "/sendMessage", data=data, timeout=15.0,
            )
        except httpx.HTTPError:
            log.exception("telegram_bot.send_text: POST failed")
            return False
        return resp.status_code == 200 and resp.json().get("ok") is True

    def send_read_receipt(
        self,
        reply_to_message_id: int,
        text: str = "✓ শুনেছেন / Heard",
    ) -> bool:
        """Send a short threaded reply confirming the parents played the
        message. Fires after F3 unless Gulu/sender-chat is unreachable."""
        return self.send_text(text, reply_to_message_id=reply_to_message_id)

    # -- long-poll thread ------------------------------------------------
    def _poll_loop(self) -> None:
        backoff = 2.0
        while not self._stop_evt.is_set():
            try:
                updates = self._get_updates()
            except httpx.HTTPError as e:
                log.warning("telegram_bot: getUpdates transport error: %s", e)
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except Exception:
                log.exception("telegram_bot: getUpdates unexpected error")
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 2.0
            for update in updates:
                try:
                    self._handle_update(update)
                except Exception:
                    log.exception("telegram_bot: handler failed for update")

    def _get_updates(self) -> list[dict]:
        assert self._client is not None
        resp = self._client.get(
            "/getUpdates",
            params={
                "offset": self._update_offset,
                "timeout": _LONG_POLL_SEC,
                "allowed_updates": json.dumps(["message"]),
            },
        )
        if resp.status_code != 200:
            log.warning("telegram_bot: getUpdates HTTP %s", resp.status_code)
            return []
        payload = resp.json()
        if not payload.get("ok"):
            log.warning("telegram_bot: getUpdates not-ok: %s",
                        payload.get("description"))
            return []
        return payload.get("result", []) or []

    def _handle_update(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0))
        # Always advance offset so a malformed update doesn't loop forever.
        new_offset = update_id + 1
        with self._lock:
            if new_offset > self._update_offset:
                self._update_offset = new_offset

        message = update.get("message")
        if not isinstance(message, dict):
            self._save_state()
            return

        chat = message.get("chat") or {}
        sender_chat_id = str(chat.get("id") or "")
        if sender_chat_id != self._chat_id:
            log.info("telegram_bot: dropping message from unwhitelisted chat_id=%s",
                     sender_chat_id)
            self._save_state()
            return

        message_id = int(message.get("message_id", 0))
        received_at = time.time()

        incoming: IncomingMessage | None = None
        if "voice" in message or "audio" in message:
            voice = message.get("voice") or message.get("audio") or {}
            file_id = voice.get("file_id")
            if file_id:
                local_path = self._download_file(file_id, message_id)
                if local_path is not None:
                    incoming = IncomingMessage(
                        kind="voice",
                        text="",
                        audio_path=str(local_path),
                        message_id=message_id,
                        received_at=received_at,
                    )
        elif isinstance(message.get("text"), str):
            incoming = IncomingMessage(
                kind="text",
                text=message["text"],
                audio_path="",
                message_id=message_id,
                received_at=received_at,
            )
        else:
            log.info("telegram_bot: ignoring message_id=%d (no voice/audio/text)",
                     message_id)

        if incoming is not None:
            with self._lock:
                self._unread = incoming
                self._save_state()
            log.info("telegram_bot: new %s message_id=%d from Gulu",
                     incoming.kind, incoming.message_id)
            if self._callback is not None:
                try:
                    self._callback(incoming)
                except Exception:
                    log.exception("telegram_bot: on_new_message callback raised")
        else:
            self._save_state()

    def _download_file(self, file_id: str, message_id: int) -> Path | None:
        assert self._client is not None
        try:
            resp = self._client.get("/getFile", params={"file_id": file_id}, timeout=15.0)
            payload = resp.json()
            if resp.status_code != 200 or not payload.get("ok"):
                log.warning("telegram_bot: getFile failed: %s", payload)
                return None
            file_path = (payload.get("result") or {}).get("file_path")
            if not file_path:
                return None
            # file downloads use a different URL shape, not base_url.
            download_url = (
                f"https://api.telegram.org/file/bot{self._token}/{file_path}"
            )
            # Extension: preserve Telegram's (usually .oga for voice notes).
            suffix = Path(file_path).suffix or ".oga"
            local = self._inbox_dir / f"msg_{message_id}_{int(time.time())}{suffix}"
            with self._client.stream("GET", download_url, timeout=60.0) as r:
                if r.status_code != 200:
                    log.warning("telegram_bot: file download HTTP %s", r.status_code)
                    return None
                with open(local, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            log.info("telegram_bot: downloaded voice to %s (%d bytes)",
                     local.name, local.stat().st_size)
            return local
        except Exception:
            log.exception("telegram_bot: voice download failed")
            return None

    # -- persistence -----------------------------------------------------
    def _load_state(self) -> None:
        try:
            raw = self._state_path.read_text()
        except FileNotFoundError:
            return
        except OSError:
            log.exception("telegram_bot: state read failed")
            return
        try:
            data = json.loads(raw) or {}
        except json.JSONDecodeError:
            log.warning("telegram_bot: state file corrupt; starting fresh")
            return
        self._update_offset = int(data.get("update_offset", 0) or 0)
        unread = data.get("unread")
        if isinstance(unread, dict):
            try:
                self._unread = IncomingMessage(
                    kind=str(unread.get("kind", "")),
                    text=str(unread.get("text", "")),
                    audio_path=str(unread.get("audio_path", "")),
                    message_id=int(unread.get("message_id", 0)),
                    received_at=float(unread.get("received_at", 0.0)),
                )
            except (TypeError, ValueError):
                log.warning("telegram_bot: unread slot malformed; dropping")
                self._unread = None

    def _save_state(self) -> None:
        payload = {
            "update_offset": self._update_offset,
            "unread": asdict(self._unread) if self._unread is not None else None,
        }
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._state_path)
        except OSError:
            log.exception("telegram_bot: state write failed")
