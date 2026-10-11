#!/usr/bin/env bash
# SeniorAI Linux installer — user-service model (Option A).
#
# Target:  Debian 12 Bookworm, PipeWire + graphical.target, login user.
# Run as: your normal user (e.g. 'alice'). The script sudoes for the bits
# that need root. Do NOT run as root — we need your user session so
# `systemctl --user` and `wpctl` work.
#
# Layout after install:
#   ~/seniorai/                                 (this repo checkout)
#   ~/seniorai/.venv/                           (Python venv)
#   ~/seniorai/.env                             (your secrets, chmod 600)
#   /etc/udev/rules.d/99-seniorai-keypad.rules  (keypad symlink)
#   /etc/wireplumber/main.lua.d/51-seniorai-audio.lua
#   /etc/systemd/user/seniorai.service          (user unit)
#
# Usage:
#   cd ~/seniorai
#   ./scripts/install_linux.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
if [[ $EUID -eq 0 ]]; then
    echo "ERROR: Do NOT run as root. Run as your normal login user — the script sudoes when needed." >&2
    exit 1
fi
if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required but not installed." >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_USER="$(id -un)"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11

echo "==> Target user: $REPO_USER"
echo "==> App dir:     $APP_DIR"

# ---------------------------------------------------------------------------
# apt packages
# ---------------------------------------------------------------------------
echo "==> apt packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    libgl1 libglib2.0-0 \
    libsdl2-2.0-0 libsdl2-mixer-2.0-0 \
    libportaudio2 portaudio19-dev \
    ffmpeg v4l-utils evtest \
    alsa-utils pulseaudio-utils \
    pipewire-pulse wireplumber \
    curl ca-certificates

# ---------------------------------------------------------------------------
# Python version
# ---------------------------------------------------------------------------
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%.*}"
PY_MINOR="${PY_VER#*.}"
if (( PY_MAJOR < PY_MIN_MAJOR )) || { (( PY_MAJOR == PY_MIN_MAJOR )) && (( PY_MINOR < PY_MIN_MINOR )); }; then
    echo "ERROR: Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ required; found $PY_VER" >&2
    exit 1
fi
echo "==> Python $PY_VER OK"

# ---------------------------------------------------------------------------
# Groups — evdev needs input, ALSA fallback needs audio, camera needs video
# ---------------------------------------------------------------------------
echo "==> Adding $REPO_USER to input, audio, video groups"
sudo usermod -aG input,audio,video "$REPO_USER"
# Note: new group membership only takes effect on next login. We warn at end.

# ---------------------------------------------------------------------------
# Linger — user service must run without an active login
# ---------------------------------------------------------------------------
echo "==> Enabling linger for $REPO_USER"
sudo loginctl enable-linger "$REPO_USER"

# ---------------------------------------------------------------------------
# Python venv + deps
# ---------------------------------------------------------------------------
echo "==> Python venv"
cd "$APP_DIR"
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime directories
# ---------------------------------------------------------------------------
mkdir -p captures logs assets/audio

# ---------------------------------------------------------------------------
# udev rule for the Unicode-named keypad
# ---------------------------------------------------------------------------
echo "==> Installing udev rule for keypad"
sudo install -m 644 "$APP_DIR/systemd/99-seniorai-keypad.rules" \
    /etc/udev/rules.d/99-seniorai-keypad.rules
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=input

if [[ -L /dev/input/seniorai-keypad ]]; then
    echo "    /dev/input/seniorai-keypad -> $(readlink /dev/input/seniorai-keypad)"
else
    echo "    WARNING: /dev/input/seniorai-keypad not present. Unplug the keypad, replug it, then re-check." >&2
fi

# ---------------------------------------------------------------------------
# WirePlumber audio priority rule
# ---------------------------------------------------------------------------
echo "==> Installing WirePlumber audio-priority rule"
sudo install -d -m 755 /etc/wireplumber/main.lua.d
sudo install -m 644 "$APP_DIR/systemd/51-seniorai-audio.lua" \
    /etc/wireplumber/main.lua.d/51-seniorai-audio.lua

if systemctl --user is-active --quiet wireplumber; then
    systemctl --user restart wireplumber
    echo "    wireplumber restarted"
else
    echo "    wireplumber user service not running — it will pick up the rule on next login"
fi

# ---------------------------------------------------------------------------
# systemd user unit
# ---------------------------------------------------------------------------
echo "==> Installing systemd user unit"
sudo install -d -m 755 /etc/systemd/user
sudo install -m 644 "$APP_DIR/systemd/seniorai.service" \
    /etc/systemd/user/seniorai.service
systemctl --user daemon-reload
systemctl --user enable seniorai.service

# ---------------------------------------------------------------------------
# .env permissions
# ---------------------------------------------------------------------------
if [[ -f .env ]]; then
    chmod 600 .env
    echo "==> .env permissions secured (chmod 600)"
else
    echo "==> WARNING: .env not found at $APP_DIR/.env" >&2
    echo "    Copy .env.example → .env and fill in secrets before starting the service." >&2
fi

# ---------------------------------------------------------------------------
# Pre-recorded audio assets
# ---------------------------------------------------------------------------
if [[ -f .env ]] && [[ ! -f assets/audio/namashkar.mp3 ]]; then
    echo "==> Generating pre-recorded audio assets (one-time, needs TTS key)"
    .venv/bin/python scripts/generate_assets.py
elif [[ -f assets/audio/namashkar.mp3 ]]; then
    echo "==> Pre-recorded assets already present; skipping regeneration"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<'EOF'

==> Install complete.

Next steps (in order):

  1. If you haven't yet, copy and edit .env:
       cp .env.example .env
       chmod 600 .env
       $EDITOR .env

     For production on this box, set:
       INPUT_BACKEND=evdev
       EVDEV_DEVICE=/dev/input/seniorai-keypad
       BUTTON_CHAT=c
       BUTTON_VISION=v
       BUTTON_TELEGRAM=x
       CAMERA_INDEX=0
       MIC_DEVICE_INDEX=        (empty = PipeWire default)

  2. LOG OUT AND BACK IN (or reboot) so the new group memberships
     (input, audio, video) take effect. Without this, evdev can't
     open /dev/input/seniorai-keypad.

  3. Run the preflight validator:
       ./scripts/preflight_linux.sh

  4. When preflight is all-green, start the service:
       systemctl --user start seniorai
       systemctl --user status seniorai
       journalctl --user -u seniorai -f
EOF
