"""Print current network status (wifi link + internet reachability).

    python scripts/test_network.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.network import _has_link, check_network, in_quiet_hours
from datetime import datetime


def main() -> int:
    cfg = load_config()
    now = datetime.now()

    has_link = _has_link()
    print(f"link (non-loopback IPv4 present): {has_link}")

    print(f"probe url: {cfg.network_check_url}")
    print(f"probe timeout: {cfg.network_check_timeout_sec}s")

    degraded, reason = check_network(cfg.network_check_url, cfg.network_check_timeout_sec)
    print(f"degraded: {degraded}  reason: {reason or '(healthy)'}")

    quiet = in_quiet_hours(now, cfg.quiet_hours_start, cfg.quiet_hours_end)
    print(f"current hour: {now.hour:02d}")
    print(f"quiet hours: [{cfg.quiet_hours_start}, {cfg.quiet_hours_end})  "
          f"currently quiet: {quiet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
