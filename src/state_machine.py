"""Global runtime state for the app.

Primary ``Mode`` is exclusive (one at a time). Flags overlay on top (e.g. a
degraded network can coexist with any mode; unread telegram can coexist with
IDLE). Buttons consult both when deciding what to do.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum, auto

log = logging.getLogger("seniorai.state")


class Mode(Enum):
    IDLE = auto()
    CHATTING = auto()          # Button 1 active conversation
    MEDICINE_WINDOW = auto()   # 30-second grace after medicine detection


@dataclass
class Flags:
    network_degraded: bool = False
    network_reason: str = ""  # "no_wifi" or "no_internet" or ""
    has_unread_telegram: bool = False
    unread_message_text: str = ""  # populated when unread=True


@dataclass
class State:
    mode: Mode = Mode.IDLE
    flags: Flags = field(default_factory=Flags)


class StateMachine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = State()

    # ---- reads -----------------------------------------------------------
    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._state.mode

    @property
    def flags(self) -> Flags:
        with self._lock:
            # Return a copy so callers can read without holding the lock.
            return Flags(**self._state.flags.__dict__)

    # ---- writes ----------------------------------------------------------
    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            if self._state.mode is not mode:
                log.info("mode: %s -> %s", self._state.mode.name, mode.name)
                self._state.mode = mode

    def set_network(self, degraded: bool, reason: str = "") -> None:
        with self._lock:
            prev = (self._state.flags.network_degraded, self._state.flags.network_reason)
            self._state.flags.network_degraded = degraded
            self._state.flags.network_reason = reason if degraded else ""
            if prev != (degraded, self._state.flags.network_reason):
                log.info("network: degraded=%s reason=%s", degraded, self._state.flags.network_reason)

    def set_unread(self, has_unread: bool, message_text: str = "") -> None:
        with self._lock:
            if self._state.flags.has_unread_telegram != has_unread:
                log.info("telegram unread: %s", has_unread)
            self._state.flags.has_unread_telegram = has_unread
            self._state.flags.unread_message_text = message_text if has_unread else ""
