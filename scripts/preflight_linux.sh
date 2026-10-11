#!/usr/bin/env bash
# SeniorAI preflight — verify the production Linux box is wired up
# correctly before enabling the service. Exits 0 if everything is green,
# nonzero if any hard failure is found. Warnings don't fail the run.
#
# Usage (as your normal login user, NOT root):
#   cd ~/seniorai
#   ./scripts/preflight_linux.sh

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$APP_DIR/.venv/bin/python"

# ---- output helpers --------------------------------------------------------
if [[ -t 1 ]]; then
    C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_RST=$'\033[0m'
else
    C_OK=""; C_ERR=""; C_WARN=""; C_RST=""
fi
FAILED=0
WARNED=0
pass() { printf "  ${C_OK}✔${C_RST} %s\n" "$1"; }
fail() { printf "  ${C_ERR}✘${C_RST} %s\n" "$1"; FAILED=$((FAILED+1)); }
warn() { printf "  ${C_WARN}!${C_RST} %s\n" "$1"; WARNED=$((WARNED+1)); }
info() { printf "  · %s\n" "$1"; }

# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------
echo "==> Environment"
if [[ $EUID -eq 0 ]]; then
    fail "running as root — rerun as your normal login user"
    exit 1
fi
pass "user: $(id -un) (uid=$EUID)"
pass "app dir: $APP_DIR"

if [[ ! -x "$PY" ]]; then
    fail "Python venv missing at $PY — run scripts/install_linux.sh first"
    exit 1
fi
pass "venv: $PY"

if [[ -f "$APP_DIR/.env" ]]; then
    pass ".env present"
    PERMS=$(stat -c '%a' "$APP_DIR/.env")
    if [[ "$PERMS" == "600" ]]; then
        pass ".env permissions = 600"
    else
        warn ".env permissions = $PERMS (recommend chmod 600)"
    fi
else
    fail ".env not found — copy .env.example and fill in"
fi

# ---------------------------------------------------------------------------
# 2. Groups (evdev/audio/video access)
# ---------------------------------------------------------------------------
echo
echo "==> Groups"
for g in input audio video; do
    if id -nG | tr ' ' '\n' | grep -qx "$g"; then
        pass "group: $g"
    else
        fail "not in group: $g — log out and back in after running install_linux.sh"
    fi
done

# ---------------------------------------------------------------------------
# 3. Linger (user service persistence)
# ---------------------------------------------------------------------------
echo
echo "==> Linger"
if loginctl show-user "$(id -un)" 2>/dev/null | grep -q '^Linger=yes'; then
    pass "linger enabled (service runs without active login)"
else
    warn "linger not enabled — service will stop when you log out"
fi

# ---------------------------------------------------------------------------
# 4. Keypad (udev symlink + readable)
# ---------------------------------------------------------------------------
echo
echo "==> Keypad"
KEYPAD=/dev/input/seniorai-keypad
if [[ -L "$KEYPAD" ]]; then
    pass "$KEYPAD -> $(readlink "$KEYPAD")"
    if [[ -r "$KEYPAD" ]]; then
        pass "keypad readable by $(id -un)"
    else
        fail "keypad not readable — group membership hasn't taken effect (log out/in)"
    fi
else
    fail "$KEYPAD missing — replug keypad, or 'sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input'"
fi

# ---------------------------------------------------------------------------
# 5. Camera
# ---------------------------------------------------------------------------
echo
echo "==> Camera"
if [[ -c /dev/video0 ]]; then
    pass "/dev/video0 present"
else
    fail "/dev/video0 missing — is the USB camera plugged in?"
fi
if command -v v4l2-ctl >/dev/null 2>&1; then
    if v4l2-ctl --device=/dev/video0 --info >/dev/null 2>&1; then
        CAM_NAME=$(v4l2-ctl --device=/dev/video0 --info 2>/dev/null | awk -F': ' '/Card type/{print $2}')
        pass "v4l2 info OK${CAM_NAME:+ ($CAM_NAME)}"
    else
        fail "v4l2-ctl cannot open /dev/video0"
    fi
else
    warn "v4l2-ctl not installed"
fi

# Capture one JPEG frame end-to-end via ffmpeg.
if command -v ffmpeg >/dev/null 2>&1; then
    FRAME=/tmp/ds_preflight_cam.jpg
    rm -f "$FRAME"
    if ffmpeg -hide_banner -loglevel error -f v4l2 -video_size 1280x720 \
              -i /dev/video0 -frames:v 1 "$FRAME" 2>/dev/null \
       && [[ -s "$FRAME" ]]; then
        pass "captured test frame ($(stat -c%s "$FRAME") bytes)"
    else
        fail "ffmpeg capture failed"
    fi
else
    warn "ffmpeg not installed"
fi

# ---------------------------------------------------------------------------
# 6. PipeWire defaults
# ---------------------------------------------------------------------------
echo
echo "==> PipeWire defaults"
if command -v pactl >/dev/null 2>&1; then
    SINK=$(pactl info 2>/dev/null | awk -F': ' '/^Default Sink/{print $2}')
    SRC=$(pactl info 2>/dev/null  | awk -F': ' '/^Default Source/{print $2}')
    case "$SINK" in
        alsa_output.usb-Dell_Dell_AC511*) pass "sink:   $SINK" ;;
        "")                               fail "no default sink" ;;
        *)                                fail "sink is '$SINK' — expected Dell AC511 USB soundbar" ;;
    esac
    case "$SRC" in
        alsa_input.usb-USB_FS_AUDIO*) pass "source: $SRC" ;;
        "")                           fail "no default source" ;;
        *)                            fail "source is '$SRC' — expected USB_FS_AUDIO mic" ;;
    esac
else
    fail "pactl not found — 'sudo apt install pulseaudio-utils'"
fi

# ---------------------------------------------------------------------------
# 7. Audio playback (non-interactive, exit code only)
# ---------------------------------------------------------------------------
echo
echo "==> Audio playback"
if command -v speaker-test >/dev/null 2>&1; then
    # speaker-test via PipeWire's ALSA shim often takes 4-6s even with -l 1,
    # so we cap the run at 3s. Exit 124 = timeout killed it WHILE producing
    # audio (the tone was playing the whole time), which is the successful
    # outcome we care about. Any other nonzero is a real failure.
    timeout 3 speaker-test -t sine -f 440 -l 1 -s 1 >/dev/null 2>&1
    rc=$?
    if [[ $rc -eq 0 || $rc -eq 124 ]]; then
        pass "speaker-test ran (listen for a 440Hz tone)"
    else
        fail "speaker-test failed (exit $rc)"
    fi
else
    warn "speaker-test not installed"
fi

# ---------------------------------------------------------------------------
# 8. Mic capture — record 3s, check RMS
# ---------------------------------------------------------------------------
echo
echo "==> Mic capture"
info "recording 3 seconds — say something now..."
WAV=/tmp/ds_preflight_mic.wav
if arecord -q -f cd -d 3 "$WAV" 2>/dev/null; then
    PEAK=$("$PY" - <<PYEOF
import wave, numpy as np, sys
try:
    with wave.open("$WAV","rb") as w:
        raw = w.readframes(w.getnframes())
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
    print(f"{float(np.sqrt(np.mean(arr*arr))):.4f}")
except Exception as e:
    print("0")
    sys.exit(1)
PYEOF
)
    if awk -v p="$PEAK" 'BEGIN{exit !(p+0 > 0.005)}'; then
        pass "mic rms=$PEAK (above floor)"
        # Play it back so the operator can confirm end-to-end mic+speaker
        # together (not just that each side works in isolation).
        info "playing back — you should hear your own voice now..."
        aplay -q "$WAV" 2>/dev/null || warn "aplay of captured wav failed"
    else
        fail "mic rms=$PEAK — silent. Check default source, mic knob, unmute state"
    fi
else
    fail "arecord failed"
fi

# ---------------------------------------------------------------------------
# 9. Network
# ---------------------------------------------------------------------------
echo
echo "==> Network"
if curl -fsS -m 5 -o /dev/null -w '%{http_code}\n' \
       http://connectivitycheck.gstatic.com/generate_204 | grep -q '^204$'; then
    pass "internet reachable (generate_204 → 204)"
else
    fail "connectivity probe failed"
fi

# ---------------------------------------------------------------------------
# 10. Telegram bot token (getMe, non-destructive)
# ---------------------------------------------------------------------------
echo
echo "==> Telegram bot token"
if [[ -f "$APP_DIR/.env" ]]; then
    TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP_DIR/.env" | head -n1 | cut -d= -f2- | tr -d '"')
    if [[ -z "$TOKEN" ]]; then
        warn "TELEGRAM_BOT_TOKEN empty in .env"
    else
        HTTP=$(curl -fsS -m 10 -o /tmp/ds_getme.json -w '%{http_code}' \
               "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || echo "000")
        if [[ "$HTTP" == "200" ]]; then
            BOT=$("$PY" -c "import json; d=json.load(open('/tmp/ds_getme.json')); print(d['result']['username'])" 2>/dev/null || echo "?")
            pass "getMe OK (bot: @$BOT)"
        else
            fail "getMe returned HTTP $HTTP"
        fi
    fi
else
    warn ".env missing — skipped Telegram check"
fi

# ---------------------------------------------------------------------------
# 11. Gemini API (lightweight model list call)
# ---------------------------------------------------------------------------
echo
echo "==> Gemini API"
if [[ -f "$APP_DIR/.env" ]]; then
    GKEY=$(grep -E '^GEMINI_API_KEY=' "$APP_DIR/.env" | head -n1 | cut -d= -f2- | tr -d '"')
    if [[ -z "$GKEY" ]]; then
        warn "GEMINI_API_KEY empty in .env"
    else
        HTTP=$(curl -fsS -m 10 -o /dev/null -w '%{http_code}' \
               "https://generativelanguage.googleapis.com/v1beta/models?key=${GKEY}" 2>/dev/null || echo "000")
        if [[ "$HTTP" == "200" ]]; then
            pass "models.list OK"
        else
            fail "Gemini models.list returned HTTP $HTTP"
        fi
    fi
else
    warn ".env missing — skipped Gemini check"
fi

# ---------------------------------------------------------------------------
# 12. Google TTS API key
# ---------------------------------------------------------------------------
echo
echo "==> Google Cloud TTS API"
if [[ -f "$APP_DIR/.env" ]]; then
    TTSK=$(grep -E '^GOOGLE_TTS_API_KEY=' "$APP_DIR/.env" | head -n1 | cut -d= -f2- | tr -d '"')
    if [[ -z "$TTSK" ]]; then
        warn "GOOGLE_TTS_API_KEY empty in .env"
    else
        HTTP=$(curl -fsS -m 10 -o /dev/null -w '%{http_code}' \
               "https://texttospeech.googleapis.com/v1/voices?key=${TTSK}" 2>/dev/null || echo "000")
        if [[ "$HTTP" == "200" ]]; then
            pass "voices.list OK"
        else
            fail "TTS voices.list returned HTTP $HTTP"
        fi
    fi
else
    warn ".env missing — skipped TTS check"
fi

# ---------------------------------------------------------------------------
# 13. systemd user unit
# ---------------------------------------------------------------------------
echo
echo "==> systemd user unit"
if systemctl --user list-unit-files seniorai.service >/dev/null 2>&1 \
   && systemctl --user is-enabled seniorai.service >/dev/null 2>&1; then
    pass "seniorai.service enabled"
else
    warn "seniorai.service not enabled — run: systemctl --user enable seniorai"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "==========================================================="
if (( FAILED == 0 )); then
    printf "%sPREFLIGHT OK%s" "$C_OK" "$C_RST"
    if (( WARNED > 0 )); then
        printf " (%d warning(s) — review above)" "$WARNED"
    fi
    printf "\n"
    echo "Safe to start: systemctl --user start seniorai"
    exit 0
else
    printf "%sPREFLIGHT FAILED%s — %d failure(s), %d warning(s)\n" \
           "$C_ERR" "$C_RST" "$FAILED" "$WARNED"
    exit 1
fi
