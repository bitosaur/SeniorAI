# SeniorAI

Voice-assistant appliance for elderly parents — three USB buttons, a
webcam, mic, and speaker. Runs on a Debian 12 mini-PC.

- **Button 1 — Chat** (press-and-hold): Gemini voice chat via push-to-talk.
  Spoken reply through Google Cloud TTS (female, language auto-detected).
  Separate "medicine / document" sub-flows if Gemini sees a pill bottle
  or letter in the frame.
- **Button 2 — Vision**: capture the webcam, Gemini describes what's in
  front of the camera, TTS reads it back.
- **Button 3 — Telegram**: send a voice message to a whitelisted family
  member ("Gulu") or listen to their latest incoming voice/text. Text
  messages are spoken with a male voice; Bengali script is auto-detected
  and read with the Bangla-Indian voice.

Background threads also handle: network-outage announcer (quiet-hours
aware), unread-message reminder, and an ERROR-level log forwarder that
pages the admin's Telegram chat when anything in the stack goes wrong.

## Layout

```
src/
  main.py             # entry point, state machine, all button flows
  config.py           # .env loader
  camera.py           # OpenCV capture
  emailer.py          # Gmail SMTP
  reader.py           # Gemini Vision → { text, language }
  vision.py           # Vision-assist mode (button 2)
  chat.py             # Gemini chat mode (button 1)
  mic.py              # sounddevice capture + energy VAD
  tts.py              # Google Cloud TTS + pygame playback
  audio_player.py     # pygame.mixer wrapper (interruptible)
  announcer.py        # pre-recorded cues (ding, namashkar, errors)
  input_handler.py    # pynput (Windows/dev) + evdev (Linux prod)
  state_machine.py    # IDLE / CHAT / VISION / TELEGRAM
  network.py          # connectivity monitor + quiet-hours helper
  telegram_bot.py     # two-way voice chat with Gulu (button 3)
  telegram_alert.py   # ERROR log forwarder to admin chat
  logger.py           # rotating file + stdout
systemd/
  seniorai.service               # user-unit (runs under login user)
  99-seniorai-keypad.rules       # udev → /dev/input/seniorai-keypad
  51-seniorai-audio.lua          # WirePlumber rule pinning USB audio
scripts/
  install_linux.sh    # sets up apt deps, venv, udev, WirePlumber, user unit
  preflight_linux.sh  # validates audio/mic/camera/evdev/network/APIs
  test_*.py           # per-subsystem smoke tests
  generate_assets.py  # one-time pre-recorded audio generator
```

## Windows 10 dev setup

1. Install Python 3.11+ (tick "Add to PATH").
2. Install VS Code + extensions: **Python**, **Pylance**, **Black Formatter**, **GitLens**.
3. Clone this repo, then from the project root:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```
4. Fill in `.env` with Gmail app password + Gemini API key.
5. In VS Code: `Ctrl+Shift+P` → **Python: Select Interpreter** → `.venv`.
6. Smoke test the camera:
   ```
   python scripts/test_camera.py
   ```
7. Run the app:
   ```
   python -m src.main
   ```
   Press **F1** to scan+email, **F2** to scan+read. (Configurable in `.env`.)

## Linux production setup

Target: a Debian 12 Bookworm mini-PC (tested on Intel NUC6CAYH) with
PipeWire audio and a graphical session. SeniorAI ships as a **systemd
user service** that runs under your login user (not a synthetic system
account) so it inherits PipeWire / WirePlumber without any audio-routing
gymnastics. `loginctl enable-linger` keeps it running without an active
login.

### First-boot checklist

1. **Install prerequisites and disable idle suspend** (one-time).

   Some Debian desktop installs ship with GDM's idle suspend enabled —
   your NUC will suspend itself after a few minutes of inactivity unless
   you turn that off:

   ```bash
   # Block the suspend/sleep targets outright
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

   # Hard block at logind's policy layer — can't be bypassed by any app
   sudo mkdir -p /etc/systemd/sleep.conf.d
   sudo tee /etc/systemd/sleep.conf.d/seniorai-no-sleep.conf > /dev/null <<'EOF'
   [Sleep]
   AllowSuspend=no
   AllowHibernation=no
   AllowSuspendThenHibernate=no
   AllowHybridSleep=no
   EOF

   # Tell GDM's greeter not to suspend when idle (dconf override)
   sudo mkdir -p /etc/dconf/db/gdm.d
   sudo tee /etc/dconf/db/gdm.d/02-no-suspend > /dev/null <<'EOF'
   [org/gnome/settings-daemon/plugins/power]
   sleep-inactive-ac-type='nothing'
   sleep-inactive-ac-timeout=0
   sleep-inactive-battery-type='nothing'
   sleep-inactive-battery-timeout=0
   idle-dim=false
   [org/gnome/desktop/session]
   idle-delay=uint32 0
   [org/gnome/desktop/screensaver]
   lock-enabled=false
   idle-activation-enabled=false
   EOF

   # CRITICAL: Debian's GDM ships without a dconf profile by default, so
   # the override above is inert unless we also create the profile that
   # points GDM at the compiled db.
   sudo tee /etc/dconf/profile/gdm > /dev/null <<'EOF'
   user-db:user
   system-db:gdm
   EOF

   sudo dconf update

   # Belt-and-suspenders: logind ignores idle/lid entirely
   sudo sed -i \
       -e 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/' \
       -e 's/^#*HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' \
       -e 's/^#*IdleAction=.*/IdleAction=ignore/' \
       /etc/systemd/logind.conf
   sudo systemctl restart systemd-logind
   ```

2. **Plug in the peripherals**: USB camera, 3-button keypad, USB speaker,
   USB mic. (Headset in the 3.5mm jack is fine for bring-up but the
   WirePlumber rules assume the USB devices are present.)

3. **Clone the repo** to your home directory:

   ```bash
   cd ~
   git clone <repo-url> seniorai
   cd seniorai
   cp .env.example .env
   chmod 600 .env
   $EDITOR .env   # fill in secrets + apply the "Linux production overrides"
                  # block from the bottom of .env.example
   ```

4. **Run the installer** as your normal user (NOT root):

   ```bash
   ./scripts/install_linux.sh
   ```

   It will:
   - `apt install` system packages (Python, SDL, PortAudio, PipeWire tools, evtest)
   - add you to `input`, `audio`, `video` groups
   - `loginctl enable-linger` your user so the service survives logout
   - create `~/seniorai/.venv` and install Python deps
   - install the udev rule (creates `/dev/input/seniorai-keypad`)
   - install the WirePlumber rule (pins USB speaker + USB mic as defaults)
   - install the `systemctl --user` unit and enable it (but not start it)

5. **Log out and back in** (or reboot). This is mandatory — the new group
   memberships (`input`, `audio`, `video`) don't apply to your existing
   session.

6. **Run preflight** to validate end-to-end:

   ```bash
   ./scripts/preflight_linux.sh
   ```

   It checks: groups, linger, keypad symlink, camera device + test frame
   capture, PipeWire default sink/source, mic RMS level, network, Telegram
   `getMe`, Gemini models.list, Google TTS voices.list, and systemd unit
   state. Green = safe to start.

7. **Start the service**:

   ```bash
   systemctl --user start seniorai
   systemctl --user status seniorai
   journalctl --user -u seniorai -f
   ```

### Day-to-day operations

```bash
# Restart after a config change
systemctl --user restart seniorai

# Stop (doesn't disable — auto-starts again on next boot since lingered)
systemctl --user stop seniorai

# Disable auto-start on boot
systemctl --user disable seniorai

# View logs (live)
journalctl --user -u seniorai -f

# View logs (since boot)
journalctl --user -u seniorai -b
```

## Key bindings

Set `BUTTON_CHAT` / `BUTTON_VISION` / `BUTTON_TELEGRAM` in `.env`. Values:

- Single chars: `a`, `b`, `c`, ...
- Function keys: `f1`..`f12`
- Specials: `esc`, `space`, `enter`, `tab`
- Multimedia keycodes: `copy`, `paste`, `cut`

**Keypad gotcha**: the tested production keypad (USB `514c:8851`) reports
itself as a generic keyboard and sends **Ctrl+C / Ctrl+V / Ctrl+X** for
its three physical buttons — not real `KEY_COPY`/`KEY_PASTE`/`KEY_CUT`.
For that device use `BUTTON_CHAT=c`, `BUTTON_VISION=v`, `BUTTON_TELEGRAM=x`.
Other vendors' keypads may emit the real keycodes — use `copy`/`paste`/`cut`
there.

## Secrets

`.env` stays off git (`.gitignore`). Production install does `chmod 600`
on it. In the user-service model the file stays owned by your login user
(the same user the service runs as), so no `chown` dance is needed.

## Troubleshooting

- **Camera won't open on Linux:** verify `ls /dev/video*` and that your
  user is in the `video` group (`id -nG`). If you just ran the installer,
  log out and back in.
- **Keypad not detected:** confirm the symlink exists with
  `ls -la /dev/input/seniorai-keypad`. If missing, unplug/replug the
  keypad; if still missing, run
  `sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input`.
- **No audio coming out / mic silent:** check
  `pactl info | grep Default` — the sink should be the Dell AC511 soundbar
  and the source should be the Medeli USB FS AUDIO mic. If PipeWire picked
  the wrong defaults, restart WirePlumber:
  `systemctl --user restart wireplumber`. The `51-seniorai-audio.lua`
  rule in `/etc/wireplumber/main.lua.d/` is what pins them.
- **Service exits immediately with SDL audio error:** the unit MUST set
  `SDL_AUDIODRIVER=pulseaudio` (not `alsa`) so it goes through the
  pipewire-pulse shim instead of bypassing PipeWire.
- **TTS / Gemini timeout:** needs internet. Check DNS + outbound HTTPS.
  Development machines behind a corporate proxy need `truststore` (already
  in `requirements.txt`) to pick up the OS CA store.
- **Gmail rejects login:** must be an **App Password** (16 chars), not the
  account password. 2FA must be enabled on the Gmail account.
- **Idle suspend kicks in anyway:** you missed the step-1 dconf override.
  The broadcast comes from the `Debian-gdm` user, not your login — the
  GDM greeter session is the one suspending. Verify with
  `systemctl status sleep.target` (should show `Loaded: masked`).
