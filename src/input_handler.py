"""Cross-platform key/button listener.

Two backends share the same ``InputHandler`` interface:

* ``PynputBackend`` — works on Windows (dev) and Linux with an X/Wayland
  session. Uses the ``pynput`` library.
* ``EvdevBackend`` — works on headless Linux by reading from
  ``/dev/input/eventN``. Requires the user to be in the ``input`` group.

Both backends track held-key state so callbacks fire once per press→release
transition (key-repeat events are filtered). ``on()`` registers a press-only
callback; ``on_hold()`` registers separate press and release callbacks for
push-to-talk.

Selection is automatic based on ``platform.system()`` unless overridden via
``INPUT_BACKEND`` in the env.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Callable, Protocol

log = logging.getLogger("seniorai.input")

Callback = Callable[[], None]


class InputHandler(Protocol):
    def on(self, key_name: str, callback: Callback) -> None: ...
    def on_hold(self, key_name: str, on_press: Callback, on_release: Callback) -> None: ...
    def run(self) -> None: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# pynput backend (Windows dev, Linux with display)
# ---------------------------------------------------------------------------
class PynputBackend:
    def __init__(self) -> None:
        from pynput import keyboard  # imported lazily
        self._kb = keyboard
        self._press_bindings: dict[str, Callback] = {}
        self._release_bindings: dict[str, Callback] = {}
        self._held: set[str] = set()
        self._listener = None
        self._stop_evt = threading.Event()

    @staticmethod
    def _normalize_pynput_key(key) -> str | None:
        from pynput import keyboard
        if isinstance(key, keyboard.KeyCode):
            if key.char:
                return key.char.lower()
            vk = getattr(key, "vk", None)
            if vk == 0x2C:  # VK_SNAPSHOT
                return "printscreen"
            return None
        name = getattr(key, "name", None)
        return name.lower() if name else None

    def on(self, key_name: str, callback: Callback) -> None:
        self._press_bindings[key_name.strip().lower()] = callback

    def on_hold(self, key_name: str, on_press: Callback, on_release: Callback) -> None:
        name = key_name.strip().lower()
        self._press_bindings[name] = on_press
        self._release_bindings[name] = on_release

    def _on_press(self, key) -> None:
        name = self._normalize_pynput_key(key)
        if not name or name in self._held:
            return
        self._held.add(name)
        cb = self._press_bindings.get(name)
        if cb:
            log.debug("key press: %s", name)
            try:
                cb()
            except Exception:
                log.exception("press callback for %s failed", name)

    def _on_release(self, key) -> None:
        name = self._normalize_pynput_key(key)
        if not name or name not in self._held:
            return
        self._held.discard(name)
        cb = self._release_bindings.get(name)
        if cb:
            log.debug("key release: %s", name)
            try:
                cb()
            except Exception:
                log.exception("release callback for %s failed", name)

    def run(self) -> None:
        with self._kb.Listener(
            on_press=self._on_press, on_release=self._on_release
        ) as listener:
            self._listener = listener
            while not self._stop_evt.wait(timeout=0.5):
                pass
            listener.stop()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._listener is not None:
            self._listener.stop()


# ---------------------------------------------------------------------------
# evdev backend (Linux production, headless-safe)
# ---------------------------------------------------------------------------
_EVDEV_NAME_MAP = {
    "copy": "COPY",
    "paste": "PASTE",
    "cut": "CUT",
    "esc": "ESC",
    "space": "SPACE",
    "enter": "ENTER",
    "tab": "TAB",
}


def _friendly_to_evdev_keyname(name: str) -> str:
    n = name.strip().lower()
    if n in _EVDEV_NAME_MAP:
        return f"KEY_{_EVDEV_NAME_MAP[n]}"
    if len(n) == 1 and n.isalnum():
        return f"KEY_{n.upper()}"
    if n.startswith("f") and n[1:].isdigit():
        return f"KEY_{n.upper()}"
    return f"KEY_{n.upper()}"


class EvdevBackend:
    def __init__(self, device_path: str = "") -> None:
        import evdev  # imported lazily
        self._evdev = evdev
        self._press_bindings: dict[str, Callback] = {}   # evdev KEY_* -> cb
        self._release_bindings: dict[str, Callback] = {}
        self._held: set[str] = set()
        self._device_path = device_path
        self._stop_evt = threading.Event()
        self._device = None

    def on(self, key_name: str, callback: Callback) -> None:
        self._press_bindings[_friendly_to_evdev_keyname(key_name)] = callback

    def on_hold(self, key_name: str, on_press: Callback, on_release: Callback) -> None:
        code = _friendly_to_evdev_keyname(key_name)
        self._press_bindings[code] = on_press
        self._release_bindings[code] = on_release

    def _pick_device(self):
        evdev = self._evdev
        if self._device_path:
            return evdev.InputDevice(self._device_path)

        wanted = set(self._press_bindings.keys())
        candidates = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            caps = dev.capabilities(verbose=False).get(evdev.ecodes.EV_KEY, [])
            key_names = {evdev.ecodes.KEY.get(c) if isinstance(evdev.ecodes.KEY, dict)
                         else evdev.ecodes.keys.get(c) for c in caps}
            key_names.discard(None)
            if wanted & set(key_names):
                return dev
            if evdev.ecodes.KEY_A in caps:
                candidates.append(dev)

        if candidates:
            return candidates[0]
        raise RuntimeError("No suitable /dev/input device found. Set EVDEV_DEVICE.")

    def _fire(self, bindings: dict[str, Callback], names: list[str], label: str) -> None:
        for n in names:
            cb = bindings.get(n)
            if cb:
                log.debug("evdev %s: %s", label, n)
                try:
                    cb()
                except Exception:
                    log.exception("%s callback for %s failed", label, n)
                return

    def run(self) -> None:
        evdev = self._evdev
        self._device = self._pick_device()
        log.info("evdev listening on %s (%s)", self._device.path, self._device.name)
        try:
            for event in self._device.read_loop():
                if self._stop_evt.is_set():
                    break
                if event.type != evdev.ecodes.EV_KEY:
                    continue
                key_event = evdev.categorize(event)
                names = key_event.keycode if isinstance(key_event.keycode, list) else [key_event.keycode]
                primary = names[0] if names else None
                if primary is None:
                    continue
                if key_event.keystate == key_event.key_down:
                    if primary in self._held:
                        continue  # belt-and-braces; key_hold is already filtered
                    self._held.add(primary)
                    self._fire(self._press_bindings, names, "press")
                elif key_event.keystate == key_event.key_up:
                    if primary not in self._held:
                        continue
                    self._held.discard(primary)
                    self._fire(self._release_bindings, names, "release")
                # key_hold ignored — that's kernel auto-repeat
        finally:
            if self._device is not None:
                self._device.close()

    def stop(self) -> None:
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_handler(backend: str = "auto", evdev_device: str = "") -> InputHandler:
    choice = backend.lower()
    if choice == "auto":
        choice = "evdev" if platform.system() == "Linux" else "pynput"

    if choice == "pynput":
        return PynputBackend()
    if choice == "evdev":
        return EvdevBackend(device_path=evdev_device)
    raise ValueError(f"Unknown INPUT_BACKEND: {backend}")
