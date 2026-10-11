"""App entry point.

Wires the 3-button input to action flows:

* Button 1 (F1, chat) — push-to-talk: press-and-hold to record, release
  to send to Gemini and hear the reply. If TTS is currently playing
  (a Gemini reply or a document being read aloud), pressing F1 stops
  the playback instead of starting a new turn.
* Button 2 (F2, vision) — capture a photo. Plays a shutter cue during
  capture and a "thinking" cue while Gemini is analyzing. If a chat
  session is warm, the photo is injected into that session for follow-up.
  Otherwise, classify the image and branch: medicine (speak + short
  session), document (F1 to answer "read or send"), or other (speak +
  warm session).
* Button 3 (F3, telegram) — phase 6 stub.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import announcer, audio_player, camera, emailer, reader, telegram_alert, vision
from .chat import ChatReply, GeminiChat
from .config import Config, load_config
from .input_handler import make_handler
from .logger import setup_logger
from .mic import RecordResult, record_while_held
from .network import NetworkMonitor, in_quiet_hours
from .state_machine import Mode, StateMachine
from .telegram_bot import IncomingMessage, TelegramBot
from .tts import speak
from .vision import DocumentIntent, VisionCategory, VisionResult


_MID_CHAT_PHOTO_PROMPT = (
    "The user is now showing you this new photo. Briefly describe what "
    "you see in 1-2 short sentences, then wait for their follow-up "
    "question. Use standard Bangla (bn-IN) unless the image is clearly "
    "English-only. Same JSON output shape as before."
)

_MEDICINE_SEED_PROMPT = (
    "The user just showed me this medicine and I have already read out "
    "the name and dosage info. If they ask a follow-up in the next 30 "
    "seconds (e.g. 'when to take it', 'with food?'), answer briefly in "
    "standard Bangla (bn-IN). For anything risky or unclear, tell them "
    "to ask their doctor. Acknowledge this instruction with a one-word "
    "JSON reply now: {\"language\":\"bn\",\"text\":\"ঠিক আছে\"}"
)

_OTHER_SEED_PROMPT = (
    "The user just showed me this photo. I will speak the description "
    "to them separately. If they follow up with a question, answer "
    "briefly in standard Bangla (bn-IN) or English as appropriate. "
    "Acknowledge this instruction now with a one-word JSON reply: "
    "{\"language\":\"bn\",\"text\":\"ঠিক আছে\"}"
)

_DOCUMENT_FALLBACK_PROMPT = "What's this?"


def _detect_script_language(text: str, default: str = "en") -> str:
    """Cheap script-based language hint for incoming Telegram text.

    We don't have a full LID model here, but Gulu writes in either
    English or Bangla. Any character in the Bangla Unicode block
    (U+0980–U+09FF) flips the whole message to ``bn`` so Cloud TTS
    picks the Bengali voice. Falls back to ``default`` otherwise.
    """
    for ch in text:
        if "\u0980" <= ch <= "\u09FF":
            return "bn"
    return default or "en"


class App:
    def __init__(
        self,
        cfg: Config,
        sm: StateMachine,
        logger: logging.Logger,
        bot: TelegramBot,
    ) -> None:
        self.cfg = cfg
        self.sm = sm
        self.log = logger
        self.bot = bot
        self._action_lock = threading.Lock()
        # PTT state
        self._ptt_busy = threading.Event()     # recording or processing a turn
        self._record_stop = threading.Event()  # signals record_while_held to return
        # F1 routing override: when set, F1 press/release are delegated to
        # these callbacks instead of starting a normal chat PTT (used by
        # the document flow to collect the user's "read/send" answer).
        self._ptt_override: tuple[Callable[[], None], Callable[[], None]] | None = None
        # TTS interrupt: F1 during TTS stops playback instead of starting PTT.
        self._tts_playing = threading.Event()
        self._tts_interrupted = False
        # Chat session (persistent; may be seeded by vision for short or long TTL)
        self._session_lock = threading.Lock()
        self._session: GeminiChat | None = None
        self._session_expires_at: float = 0.0   # monotonic deadline
        self._session_ttl: float = float(cfg.chat_idle_timeout_sec)

    # -- guards -----------------------------------------------------------
    def _announce_degraded(self) -> None:
        reason = self.sm.flags.network_reason or "no_wifi"
        announcer.play(reason)

    def _run_exclusive(self, label: str, fn) -> None:
        def target() -> None:
            if not self._action_lock.acquire(blocking=False):
                self.log.warning("%s ignored: another action is running", label)
                return
            try:
                self.log.info("action start: %s", label)
                fn()
            except Exception:
                self.log.exception("action failed: %s", label)
            finally:
                self._action_lock.release()
                self.log.info("action end: %s", label)

        threading.Thread(target=target, daemon=True).start()

    # -- session helpers --------------------------------------------------
    def _session_is_warm(self) -> bool:
        with self._session_lock:
            return self._session is not None and time.monotonic() < self._session_expires_at

    def _ensure_chat_session(self) -> GeminiChat:
        """Get or (re)create a normal-chat session with full idle TTL."""
        with self._session_lock:
            now = time.monotonic()
            if self._session is None or now >= self._session_expires_at:
                self._session = GeminiChat(self.cfg.gemini_api_key, self.cfg.gemini_model)
                self._session_ttl = float(self.cfg.chat_idle_timeout_sec)
                self.log.info("chat: new Gemini session")
            self._session_expires_at = now + self._session_ttl
            return self._session

    def _new_vision_session(self, ttl_sec: float) -> GeminiChat:
        """Discard any current session and start a fresh one for vision seeding."""
        with self._session_lock:
            self._session = GeminiChat(self.cfg.gemini_api_key, self.cfg.gemini_model)
            self._session_ttl = float(ttl_sec)
            self._session_expires_at = time.monotonic() + ttl_sec
            self.log.info("chat: new Gemini session (vision-seeded, ttl=%ds)", int(ttl_sec))
            return self._session

    def _touch_session(self) -> None:
        with self._session_lock:
            if self._session is not None:
                self._session_expires_at = time.monotonic() + self._session_ttl

    # -- F1 routing --------------------------------------------------------
    def on_chat_press(self) -> None:
        # 1. If TTS is currently playing, F1 interrupts it and returns.
        if self._tts_playing.is_set():
            self.log.info("F1: stopping TTS playback")
            self._tts_interrupted = True
            audio_player.stop()
            return
        # 2. If a flow has claimed PTT (e.g. document "read/send" prompt),
        #    route there instead of starting a new chat turn.
        override = self._ptt_override
        if override is not None:
            override[0]()
            return
        # 3. Normal chat PTT path.
        if self.sm.flags.network_degraded:
            self._announce_degraded()
            return
        if self._ptt_busy.is_set():
            self.log.debug("chat: press ignored (previous turn still processing)")
            return
        if self._action_lock.locked():
            self.log.info("chat: press ignored (another action is running)")
            return
        self._ptt_busy.set()
        self._record_stop.clear()
        self.sm.set_mode(Mode.CHATTING)
        announcer.play("ding_start", block=False)
        threading.Thread(target=self._ptt_turn, daemon=True).start()

    def on_chat_release(self) -> None:
        override = self._ptt_override
        if override is not None:
            override[1]()
            return
        if self._ptt_busy.is_set():
            self._record_stop.set()

    # -- chat (push-to-talk) ---------------------------------------------
    def _ptt_turn(self) -> None:
        try:
            result = record_while_held(
                stop_event=self._record_stop,
                sample_rate=self.cfg.mic_sample_rate,
                device_index=self.cfg.mic_device_index if self.cfg.mic_device_index >= 0 else None,
                min_sec=0.25,
                min_peak_rms=self.cfg.vad_energy_threshold * 0.5,
                max_sec=self.cfg.vad_max_utterance_sec,
            )
            if not result.spoken:
                announcer.play("say_again")
                return
            session = self._ensure_chat_session()
            announcer.play("thinking", block=False, loop=True)
            try:
                reply = session.send_audio(result.audio_wav, self.cfg.tts_default_lang)
            except Exception as e:
                self.log.exception("chat: Gemini turn failed")
                telegram_alert.alert(
                    "gemini_api_failure",
                    f"SeniorAI: Gemini API call failed.\n"
                    f"{type(e).__name__}: {e}",
                )
                audio_player.stop()
                announcer.play("say_again")
                return
            self._speak_reply(reply)
            self._touch_session()
        finally:
            self._ptt_busy.clear()
            self.sm.set_mode(Mode.IDLE)

    # -- vision (Button 2) -----------------------------------------------
    def on_vision(self) -> None:
        if self.sm.flags.network_degraded:
            self._announce_degraded()
            return
        if self._ptt_busy.is_set():
            self.log.info("vision button ignored: chat PTT in progress")
            return
        self._run_exclusive("vision", self._vision_flow)

    def _vision_flow(self) -> None:
        mid_chat = self._session_is_warm()
        announcer.play("shutter", block=False)
        try:
            image_path = camera.capture(
                self.cfg.capture_dir,
                index=self.cfg.camera_index,
                width=self.cfg.camera_width,
                height=self.cfg.camera_height,
                warmup_frames=self.cfg.camera_warmup_frames,
                jpeg_quality=self.cfg.camera_jpeg_quality,
            )
        except Exception as e:
            self.log.exception("vision: camera capture failed")
            telegram_alert.alert(
                "camera_failure",
                f"SeniorAI: Camera capture failed.\n{type(e).__name__}: {e}",
            )
            announcer.play("say_again")
            return

        if mid_chat:
            self._mid_chat_photo(image_path)
        else:
            self._classify_and_branch(image_path)

    def _mid_chat_photo(self, image_path: Path) -> None:
        with self._session_lock:
            session = self._session
        if session is None:
            self._classify_and_branch(image_path)
            return
        self.sm.set_mode(Mode.CHATTING)
        announcer.play("thinking", block=False, loop=True)
        try:
            image_bytes = image_path.read_bytes()
            reply = session.send_image(
                image_bytes,
                _MID_CHAT_PHOTO_PROMPT,
                self.cfg.tts_default_lang,
            )
        except Exception as e:
            self.log.exception("vision: mid-chat image send failed")
            telegram_alert.alert(
                "gemini_api_failure",
                f"SeniorAI: Gemini API call failed.\n{type(e).__name__}: {e}",
            )
            audio_player.stop()
            announcer.play("say_again")
            self.sm.set_mode(Mode.IDLE)
            return
        self._speak_reply(reply)
        self._touch_session()
        self.sm.set_mode(Mode.IDLE)

    def _classify_and_branch(self, image_path: Path) -> None:
        announcer.play("thinking", block=False, loop=True)
        try:
            result = vision.classify_image(
                image_path, self.cfg.gemini_api_key, self.cfg.gemini_model,
            )
        except Exception as e:
            self.log.exception("vision: classify failed")
            telegram_alert.alert(
                "gemini_api_failure",
                f"SeniorAI: Gemini API call failed.\n{type(e).__name__}: {e}",
            )
            audio_player.stop()
            announcer.play("say_again")
            return

        if result.category is VisionCategory.MEDICINE:
            self._medicine_flow(result)
        elif result.category is VisionCategory.DOCUMENT:
            self._document_flow(result)
        else:
            self._other_flow(result)

    # -- vision branches --------------------------------------------------
    def _medicine_flow(self, result: VisionResult) -> None:
        self._speak(result.description, result.language)
        # Seed a short-TTL session so user can ask follow-ups for ~30s.
        try:
            image_bytes = result.image_path.read_bytes()
            session = self._new_vision_session(self.cfg.medicine_chat_window_sec)
            session.send_image(image_bytes, _MEDICINE_SEED_PROMPT, self.cfg.tts_default_lang)
            self._touch_session()
            self.sm.set_mode(Mode.MEDICINE_WINDOW)
        except Exception:
            self.log.exception("vision: medicine seed failed (non-fatal)")

    def _document_flow(self, result: VisionResult) -> None:
        attempts = max(1, self.cfg.document_prompt_max_attempts)
        for attempt in range(attempts):
            announcer.play("document_prompt" if attempt == 0 else "say_again")
            reply_audio = self._await_ptt_utterance(self.cfg.document_prompt_timeout_sec)
            if reply_audio is None or not reply_audio.spoken:
                self.log.info(
                    "document prompt: attempt %d/%d got no PTT response",
                    attempt + 1, attempts,
                )
                continue
            announcer.play("thinking", block=False, loop=True)
            try:
                intent = vision.parse_document_intent(
                    reply_audio.audio_wav, self.cfg.gemini_api_key, self.cfg.gemini_model,
                )
            except Exception as e:
                self.log.exception("vision: intent parse failed")
                telegram_alert.alert(
                    "gemini_api_failure",
                    f"SeniorAI: Gemini API call failed.\n{type(e).__name__}: {e}",
                )
                audio_player.stop()
                continue
            audio_player.stop()
            self.log.info("vision: document intent=%s", intent.value)
            if intent is DocumentIntent.READ:
                self._document_read(result.image_path)
                return
            if intent is DocumentIntent.SEND:
                self._document_send(result.image_path)
                return
            # UNKNOWN → next attempt (say_again will play at top of loop)
        # All attempts exhausted: silently seed chat with "What's this?"
        self.log.info("document prompt: all attempts exhausted, seeding fallback session")
        self._document_fallback_seed(result.image_path)

    def _document_fallback_seed(self, image_path: Path) -> None:
        try:
            image_bytes = image_path.read_bytes()
            session = self._new_vision_session(self.cfg.chat_idle_timeout_sec)
            session.send_image(
                image_bytes, _DOCUMENT_FALLBACK_PROMPT, self.cfg.tts_default_lang,
            )
            self._touch_session()
        except Exception:
            self.log.exception("vision: document fallback seed failed")

    def _document_read(self, image_path: Path) -> None:
        announcer.play("thinking", block=False, loop=True)
        try:
            doc = reader.extract_text(image_path, self.cfg.gemini_api_key, self.cfg.gemini_model)
        except Exception as e:
            self.log.exception("vision: document read failed")
            telegram_alert.alert(
                "gemini_api_failure",
                f"SeniorAI: Gemini API call failed.\n{type(e).__name__}: {e}",
            )
            audio_player.stop()
            announcer.play("say_again")
            return
        if not doc.text.strip():
            self.log.info("vision: no text extracted from document")
            audio_player.stop()
            announcer.play("say_again")
            return
        interrupted = self._speak(doc.text, doc.language)
        if not interrupted:
            announcer.play("ding_end", block=False)

    def _document_send(self, image_path: Path) -> None:
        announcer.play("thinking", block=False, loop=True)
        try:
            emailer.send_scan(
                self.cfg.gmail_user,
                self.cfg.gmail_app_password,
                self.cfg.email_recipient,
                image_path,
            )
        except Exception as e:
            self.log.exception("vision: document email failed")
            telegram_alert.alert(
                "email_failure",
                f"SeniorAI: Gmail send failed.\n{type(e).__name__}: {e}",
            )
            audio_player.stop()
            announcer.play("say_again")
            return
        audio_player.stop()
        announcer.play("document_sent")

    def _other_flow(self, result: VisionResult) -> None:
        # Seed a normal-TTL session with the image for any follow-up PTT.
        try:
            image_bytes = result.image_path.read_bytes()
            session = self._new_vision_session(self.cfg.chat_idle_timeout_sec)
            session.send_image(image_bytes, _OTHER_SEED_PROMPT, self.cfg.tts_default_lang)
            self._touch_session()
        except Exception:
            self.log.exception("vision: other-flow seed failed (will still speak description)")
        self._speak(result.description, result.language)

    # -- F1 PTT capture (for document-flow response) ---------------------
    def _await_ptt_utterance(self, timeout_sec: float) -> RecordResult | None:
        """Block up to ``timeout_sec`` for the user to press F1 and speak.

        Hooks into the existing F1 handler via ``_ptt_override`` so normal
        chat-PTT doesn't trigger. Returns the recorded utterance (even if
        marked ``spoken=False``) or ``None`` if no F1 press happened.
        """
        press_event = threading.Event()
        done_event = threading.Event()
        captured: dict[str, RecordResult | None] = {"result": None}

        def on_press() -> None:
            if self._ptt_busy.is_set():
                return
            self._ptt_busy.set()
            self._record_stop.clear()
            press_event.set()
            announcer.play("ding_start", block=False)

            def record_thread() -> None:
                try:
                    captured["result"] = record_while_held(
                        stop_event=self._record_stop,
                        sample_rate=self.cfg.mic_sample_rate,
                        device_index=(
                            self.cfg.mic_device_index
                            if self.cfg.mic_device_index >= 0 else None
                        ),
                        min_sec=0.25,
                        min_peak_rms=self.cfg.vad_energy_threshold * 0.5,
                        max_sec=self.cfg.vad_max_utterance_sec,
                    )
                except Exception:
                    self.log.exception("await-PTT: record failed")
                finally:
                    self._ptt_busy.clear()
                    done_event.set()

            threading.Thread(target=record_thread, daemon=True).start()

        def on_release() -> None:
            if self._ptt_busy.is_set():
                self._record_stop.set()

        self._ptt_override = (on_press, on_release)
        try:
            if not press_event.wait(timeout_sec):
                return None
            done_event.wait()
            return captured["result"]
        finally:
            self._ptt_override = None

    # -- telegram (Button 3) ---------------------------------------------
    def on_telegram(self) -> None:
        # F3 during TTS playback (e.g. while reading out a Telegram voice
        # message) stops playback, matching F1's interrupt behavior.
        if self._tts_playing.is_set():
            self.log.info("F3: stopping playback")
            self._tts_interrupted = True
            audio_player.stop()
            return
        if not self.bot.configured:
            self.log.info("telegram button ignored: bot not configured")
            return
        if self.sm.flags.network_degraded:
            self._announce_degraded()
            return
        if self.sm.mode is Mode.CHATTING or self._ptt_busy.is_set():
            self.log.info("telegram button ignored: chat in progress")
            return
        if self.bot.has_unread():
            self._run_exclusive("telegram-read", self._telegram_read_flow)
        else:
            self._run_exclusive("telegram-send", self._telegram_send_flow)

    def _telegram_send_flow(self) -> None:
        """Prompt the user to speak a reply, capture via F1 PTT, send to Gulu."""
        announcer.play("ask_message")
        # Re-use the F1 PTT override plumbing the document flow uses.
        reply = self._await_ptt_utterance(self.cfg.document_prompt_timeout_sec)
        if reply is None or not reply.spoken:
            self.log.info("telegram send: no PTT reply captured")
            announcer.play("say_again")
            return
        announcer.play("thinking", block=False, loop=True)
        try:
            ok = self.bot.send_voice(reply.audio_wav, title="From Ma/Baba")
        except Exception as e:
            self.log.exception("telegram send: send_voice raised")
            telegram_alert.alert(
                "telegram_send_failure",
                f"SeniorAI: sending voice to Gulu failed.\n"
                f"{type(e).__name__}: {e}",
            )
            audio_player.stop()
            announcer.play("say_again")
            return
        audio_player.stop()
        if ok:
            announcer.play("document_sent")
        else:
            telegram_alert.alert(
                "telegram_send_failure",
                "SeniorAI: sendAudio to Gulu returned not-ok.",
            )
            announcer.play("say_again")

    def _telegram_read_flow(self) -> None:
        """Play back the unread message from Gulu, then clear the unread slot."""
        msg = self.bot.peek_unread()
        if msg is None:
            # Race: reminder flipped unread off between button press and now.
            self.sm.set_unread(False)
            return
        announcer.play("ding_start", block=False)
        interrupted = False
        played_something = False
        try:
            if msg.kind == "voice" and msg.audio_path:
                interrupted = self._play_voice_file(Path(msg.audio_path))
                played_something = True
            elif msg.kind == "text" and msg.text:
                # Detect Bangla vs English from the script so Cloud TTS
                # picks the right voice pack, and use the male narrator
                # voice since the parents are hearing Gulu's text read
                # aloud by a "reader".
                lang = _detect_script_language(
                    msg.text, self.cfg.tts_default_lang
                )
                self.log.info(
                    "telegram read: text lang=%s (%d chars)", lang, len(msg.text)
                )
                interrupted = self._speak(
                    msg.text, lang, voice_gender="male"
                )
                played_something = True
            else:
                self.log.warning("telegram read: unread has no playable content")
        except Exception:
            self.log.exception("telegram read: playback failed")
        finally:
            message_id_for_receipt = msg.message_id if played_something else 0
            self.bot.mark_read()
            self.sm.set_unread(False)
        if not interrupted:
            announcer.play("ding_end", block=False)
        # Send Gulu a read-receipt once the parents have actually pressed
        # F3. Fires off the action thread so a slow Telegram POST doesn't
        # delay the end-of-message ding.
        if message_id_for_receipt:
            threading.Thread(
                target=self._send_read_receipt,
                args=(message_id_for_receipt,),
                daemon=True,
                name="TgReadReceipt",
            ).start()

    def _send_read_receipt(self, message_id: int) -> None:
        try:
            ok = self.bot.send_read_receipt(message_id)
            if ok:
                self.log.info("telegram: read-receipt sent for msg_id=%d",
                              message_id)
            else:
                self.log.warning("telegram: read-receipt not acknowledged "
                                 "for msg_id=%d", message_id)
        except Exception:
            self.log.exception("telegram: read-receipt send raised")

    def _play_voice_file(self, path: Path) -> bool:
        """Play a downloaded Telegram voice file. Returns True if F1/F3
        interrupted mid-playback."""
        if not path.exists():
            self.log.warning("telegram read: voice file missing: %s", path)
            announcer.play("say_again")
            return False
        audio_player.stop()
        self._tts_interrupted = False
        self._tts_playing.set()
        try:
            audio_player.play_audio_file(path, block=True)
        finally:
            self._tts_playing.clear()
        return self._tts_interrupted

    # -- inbound telegram --------------------------------------------------
    def on_new_telegram_message(self, msg: IncomingMessage) -> None:
        """Long-poll callback: new message arrived from Gulu.

        Fires on the TelegramBot poll thread — keep it cheap. Updates the
        state-machine flag so F3 routes to the read flow next press, and
        (when safe) plays the "new_message" announcement immediately.
        """
        self.sm.set_unread(True, message_text=msg.text[:200] if msg.text else "")
        if self._should_announce_new_message():
            try:
                announcer.play("new_message", block=False)
            except Exception:
                self.log.exception("telegram: new-message announce failed")

    def _should_announce_new_message(self) -> bool:
        if in_quiet_hours(datetime.now(),
                          self.cfg.quiet_hours_start,
                          self.cfg.quiet_hours_end):
            return False
        if self.sm.mode is not Mode.IDLE:
            return False
        if self._action_lock.locked() or self._ptt_busy.is_set():
            return False
        if self._tts_playing.is_set():
            return False
        return True

    # -- shared helpers ---------------------------------------------------
    def _speak_reply(self, reply: ChatReply) -> None:
        if not reply.text:
            audio_player.stop()
            announcer.play("say_again")
            return
        interrupted = self._speak(reply.text, reply.language)
        if not interrupted:
            announcer.play("ding_end", block=False)

    def _speak(self, text: str, language: str, voice_gender: str = "female") -> bool:
        """Speak ``text`` via Cloud TTS. Returns True if the user interrupted
        playback by pressing F1. Any looped ``thinking`` cue is stopped
        before speech begins.
        """
        if not text.strip():
            audio_player.stop()
            announcer.play("say_again")
            return False
        # Halt any in-progress non-speech playback (e.g. the thinking loop).
        audio_player.stop()
        self._tts_interrupted = False
        self._tts_playing.set()
        try:
            speak(
                text,
                language,
                self.cfg.google_tts_api_key,
                self.cfg.tts_default_lang,
                self.cfg.tts_slow,
                voice_gender=voice_gender,
            )
        except Exception:
            self.log.exception("TTS playback failed")
            announcer.play("say_again")
        finally:
            self._tts_playing.clear()
        return self._tts_interrupted


class UnreadReminder(threading.Thread):
    """Periodically re-announces "new message from Gulu" while unread.

    Runs once per minute, fires when ``elapsed >= reminder_interval``.
    Quiet hours and any non-idle mode suppress the announcement (but the
    interval timer still advances, so we don't bunch up reminders).
    """

    def __init__(self, app: "App") -> None:
        super().__init__(daemon=True, name="UnreadReminder")
        self.app = app
        self._stop_evt = threading.Event()
        self._next_alert_epoch = 0.0

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        interval_sec = max(60, self.app.cfg.telegram_unread_reminder_min * 60)
        log = self.app.log
        log.info("unread reminder started (interval=%ds)", interval_sec)
        while not self._stop_evt.is_set():
            try:
                self._tick(interval_sec)
            except Exception:
                log.exception("unread reminder tick failed")
            self._stop_evt.wait(timeout=60)
        log.info("unread reminder stopped")

    def _tick(self, interval_sec: int) -> None:
        if not self.app.bot.has_unread():
            self._next_alert_epoch = 0.0
            return
        now = time.time()
        if self._next_alert_epoch == 0.0:
            # First time we noticed unread; schedule next reminder.
            self._next_alert_epoch = now + interval_sec
            return
        if now < self._next_alert_epoch:
            return
        if self.app._should_announce_new_message():
            self.app.log.info("unread reminder: replaying new_message")
            try:
                announcer.play("new_message", block=False)
            except Exception:
                self.app.log.exception("unread reminder: play failed")
        else:
            self.app.log.info(
                "unread reminder suppressed (quiet hours / busy)"
            )
        self._next_alert_epoch = now + interval_sec


def _warn_missing_assets(log: logging.Logger) -> None:
    missing = announcer.missing_assets()
    if missing:
        log.warning(
            "Missing audio assets: %s. Run: python scripts/generate_assets.py",
            ", ".join(missing),
        )


def main() -> int:
    cfg = load_config()
    log = setup_logger(cfg.log_dir, cfg.log_level)
    log.info("SeniorAI starting")
    log.info("  buttons: chat=%s vision=%s telegram=%s  backend=%s",
             cfg.button_chat, cfg.button_vision, cfg.button_telegram, cfg.input_backend)
    _warn_missing_assets(log)

    telegram_alert.configure(
        cfg.telegram_bot_token, cfg.telegram_admin_chat_id, cfg.log_dir
    )
    telegram_alert.attach_log_handler(log)

    sm = StateMachine()
    bot = TelegramBot(
        cfg.telegram_bot_token,
        cfg.telegram_gulu_chat_id,
        cfg.log_dir,
    )
    app = App(cfg, sm, log, bot)
    # Attach the callback after App exists so it can touch app state.
    bot.set_callback(app.on_new_telegram_message)

    # Restore unread flag across restarts so the hourly reminder and F3
    # read-flow still fire if the app was killed mid-unread.
    if bot.has_unread():
        stored = bot.peek_unread()
        text_preview = stored.text[:200] if stored and stored.text else ""
        sm.set_unread(True, message_text=text_preview)
        log.info("telegram: restored unread state from previous run")

    announcer.play("namashkar")

    monitor = NetworkMonitor(cfg, sm)
    monitor.start()

    bot.start()

    reminder = UnreadReminder(app)
    reminder.start()

    handler = make_handler(cfg.input_backend, cfg.evdev_device)
    handler.on_hold(cfg.button_chat, app.on_chat_press, app.on_chat_release)
    handler.on(cfg.button_vision, app.on_vision)
    handler.on(cfg.button_telegram, app.on_telegram)

    def _shutdown(_sig, _frm):
        log.info("shutdown signal")
        monitor.stop()
        reminder.stop()
        bot.stop()
        handler.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    handler.run()
    monitor.stop()
    reminder.stop()
    bot.stop()
    log.info("SeniorAI stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
