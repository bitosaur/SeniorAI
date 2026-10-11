from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    gmail_user: str
    gmail_app_password: str
    email_recipient: str

    gemini_api_key: str
    gemini_model: str

    google_tts_api_key: str

    camera_index: int
    camera_warmup_frames: int
    camera_width: int
    camera_height: int
    camera_jpeg_quality: int

    button_chat: str
    button_vision: str
    button_telegram: str
    input_backend: str
    evdev_device: str

    chat_idle_timeout_sec: int
    medicine_chat_window_sec: int
    document_prompt_timeout_sec: int
    document_prompt_max_attempts: int

    mic_device_index: int  # -1 = system default
    mic_sample_rate: int
    vad_energy_threshold: float
    vad_silence_sec: float
    vad_max_wait_sec: float
    vad_max_utterance_sec: float

    capture_dir: Path
    log_dir: Path
    log_level: str

    tts_default_lang: str
    tts_slow: bool

    network_check_interval_sec: int
    network_reminder_interval_min: int
    network_check_url: str
    network_check_timeout_sec: float
    quiet_hours_start: int
    quiet_hours_end: int

    telegram_bot_token: str
    telegram_admin_chat_id: str
    telegram_gulu_chat_id: str
    telegram_unread_reminder_min: int


def load_config(env_file: str | None = None) -> Config:
    load_dotenv(env_file) if env_file else load_dotenv()

    cfg = Config(
        gmail_user=_env("GMAIL_USER", required=True),
        gmail_app_password=_env("GMAIL_APP_PASSWORD", required=True).replace(" ", ""),
        email_recipient=_env("EMAIL_RECIPIENT", required=True),
        gemini_api_key=_env("GEMINI_API_KEY", required=True),
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        google_tts_api_key=_env("GOOGLE_TTS_API_KEY", required=True),
        camera_index=_env_int("CAMERA_INDEX", 0),
        camera_warmup_frames=_env_int("CAMERA_WARMUP_FRAMES", 10),
        camera_width=_env_int("CAMERA_RESOLUTION_WIDTH", 1920),
        camera_height=_env_int("CAMERA_RESOLUTION_HEIGHT", 1080),
        camera_jpeg_quality=_env_int("CAMERA_JPEG_QUALITY", 92),
        button_chat=_env("BUTTON_CHAT", "copy").strip().lower(),
        button_vision=_env("BUTTON_VISION", "paste").strip().lower(),
        button_telegram=_env("BUTTON_TELEGRAM", "cut").strip().lower(),
        input_backend=_env("INPUT_BACKEND", "auto").strip().lower(),
        evdev_device=_env("EVDEV_DEVICE", ""),
        chat_idle_timeout_sec=_env_int("CHAT_IDLE_TIMEOUT_SEC", 150),
        medicine_chat_window_sec=_env_int("MEDICINE_CHAT_WINDOW_SEC", 30),
        document_prompt_timeout_sec=_env_int("DOCUMENT_PROMPT_TIMEOUT_SEC", 30),
        document_prompt_max_attempts=_env_int("DOCUMENT_PROMPT_MAX_ATTEMPTS", 2),
        mic_device_index=int(_env("MIC_DEVICE_INDEX", "") or "-1"),
        mic_sample_rate=_env_int("MIC_SAMPLE_RATE", 16000),
        vad_energy_threshold=float(_env("VAD_ENERGY_THRESHOLD", "0.01")),
        vad_silence_sec=float(_env("VAD_SILENCE_SEC", "1.2")),
        vad_max_wait_sec=float(_env("VAD_MAX_WAIT_SEC", "30")),
        vad_max_utterance_sec=float(_env("VAD_MAX_UTTERANCE_SEC", "30")),
        capture_dir=Path(_env("CAPTURE_DIR", "./captures")).resolve(),
        log_dir=Path(_env("LOG_DIR", "./logs")).resolve(),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        tts_default_lang=_env("TTS_DEFAULT_LANG", "en"),
        tts_slow=_env_bool("TTS_SLOW", False),
        network_check_interval_sec=_env_int("NETWORK_CHECK_INTERVAL_SEC", 30),
        network_reminder_interval_min=_env_int("NETWORK_REMINDER_INTERVAL_MIN", 60),
        network_check_url=_env("NETWORK_CHECK_URL", "http://connectivitycheck.gstatic.com/generate_204"),
        network_check_timeout_sec=float(_env("NETWORK_CHECK_TIMEOUT_SEC", "5")),
        quiet_hours_start=_env_int("QUIET_HOURS_START", 22),
        quiet_hours_end=_env_int("QUIET_HOURS_END", 7),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", ""),
        telegram_admin_chat_id=_env("TELEGRAM_ADMIN_CHAT_ID", ""),
        telegram_gulu_chat_id=_env("TELEGRAM_GULU_CHAT_ID", ""),
        telegram_unread_reminder_min=_env_int("TELEGRAM_UNREAD_REMINDER_MIN", 60),
    )

    cfg.capture_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg
