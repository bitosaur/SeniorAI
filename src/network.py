"""Network status detection + periodic degraded-state announcer.

Distinguishes two failure modes the user's parents need to hear about:

* **no_wifi**    — no non-loopback interface with a real IPv4. Also covers
                   wired-unplugged / WiFi radio off / modem off / APIPA only.
* **no_internet** — local link is up but ``NETWORK_CHECK_URL`` is
                    unreachable (ISP outage, DNS misconfig, captive portal).

The ``NetworkMonitor`` thread polls every ``NETWORK_CHECK_INTERVAL_SEC`` and
updates the StateMachine. During an outage, it re-announces every
``NETWORK_REMINDER_INTERVAL_MIN`` minutes, suppressed inside quiet hours.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import psutil

from . import announcer
from .config import Config
from .state_machine import StateMachine

log = logging.getLogger("seniorai.network")


# ---------------------------------------------------------------------------
# Stateless checks
# ---------------------------------------------------------------------------
def _has_link() -> bool:
    """True when at least one non-loopback interface has a routable IPv4."""
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        log.exception("psutil failed; assuming link is up")
        return True

    for name, stat in stats.items():
        n = name.lower()
        if n.startswith("lo") or n == "loopback":
            continue
        if not stat.isup:
            continue
        for addr in addrs.get(name, []):
            if addr.family != socket.AF_INET:
                continue
            # Skip APIPA / link-local.
            if addr.address.startswith("169.254."):
                continue
            return True
    return False


def _can_reach(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def check_network(url: str, timeout: float) -> tuple[bool, str]:
    """Return ``(is_degraded, reason)``. Reason is ``"no_wifi"`` / ``"no_internet"``."""
    if not _has_link():
        return True, "no_wifi"
    if not _can_reach(url, timeout):
        return True, "no_internet"
    return False, ""


def in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    """True when ``now.hour`` is inside the quiet window.

    ``start == end`` disables quiet hours (always announce).
    ``start > end`` means the window wraps midnight (e.g. 22..7).
    """
    if start == end:
        return False
    h = now.hour
    if start > end:
        return h >= start or h < end
    return start <= h < end


# ---------------------------------------------------------------------------
# Background monitor
# ---------------------------------------------------------------------------
class NetworkMonitor(threading.Thread):
    def __init__(self, cfg: Config, sm: StateMachine) -> None:
        super().__init__(daemon=True, name="NetworkMonitor")
        self.cfg = cfg
        self.sm = sm
        self._stop_evt = threading.Event()
        self._next_alert_epoch = 0.0

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        reminder_sec = self.cfg.network_reminder_interval_min * 60
        log.info("network monitor started (check=%ds, reminder=%dmin)",
                 self.cfg.network_check_interval_sec,
                 self.cfg.network_reminder_interval_min)
        while not self._stop_evt.is_set():
            self._tick(reminder_sec)
            self._stop_evt.wait(timeout=self.cfg.network_check_interval_sec)
        log.info("network monitor stopped")

    def _tick(self, reminder_sec: int) -> None:
        try:
            degraded, reason = check_network(
                url=self.cfg.network_check_url,
                timeout=self.cfg.network_check_timeout_sec,
            )
        except Exception:
            log.exception("network check raised")
            return

        was_degraded = self.sm.flags.network_degraded
        self.sm.set_network(degraded, reason)
        now = time.time()

        if not degraded:
            self._next_alert_epoch = 0.0
            return

        if not was_degraded:
            # Outage just started. First reminder lands one interval from now.
            self._next_alert_epoch = now + reminder_sec
            return

        if now < self._next_alert_epoch:
            return

        if in_quiet_hours(datetime.now(), self.cfg.quiet_hours_start, self.cfg.quiet_hours_end):
            log.info("network alert suppressed (quiet hours): %s", reason)
        else:
            log.info("network alert: %s", reason)
            try:
                announcer.play(reason)
            except Exception:
                log.exception("failed to play network alert")
        self._next_alert_epoch = now + reminder_sec
