"""Mic + VAD probe.

    python scripts/test_mic.py            # record one utterance, save to captures/mic_test.wav
    python scripts/test_mic.py --list     # list input devices
    python scripts/test_mic.py --monitor  # stream live RMS so you can tune VAD_ENERGY_THRESHOLD
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.mic import list_devices, record_until_silence


def _cmd_list() -> int:
    for i, d in enumerate(list_devices()):
        if d.get("max_input_channels", 0) > 0:
            print(f"[{i}] {d['name']}  ({d['max_input_channels']} ch, "
                  f"{int(d['default_samplerate'])} Hz)")
    return 0


def _cmd_monitor(cfg) -> int:
    import numpy as np
    import sounddevice as sd

    print(f"Monitoring RMS (threshold={cfg.vad_energy_threshold}). Ctrl+C to stop.")
    device = cfg.mic_device_index if cfg.mic_device_index >= 0 else None
    blocksize = int(cfg.mic_sample_rate * 0.05)
    with sd.InputStream(
        samplerate=cfg.mic_sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        device=device,
    ) as stream:
        try:
            while True:
                data, _ = stream.read(blocksize)
                rms = float(np.sqrt(np.mean(data[:, 0] ** 2)))
                bar = "#" * min(40, int(rms * 400))
                flag = "  SPEECH" if rms > cfg.vad_energy_threshold else ""
                print(f"rms={rms:.4f}  {bar}{flag}", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            return 0


def _cmd_record(cfg) -> int:
    print(f"Speak now. Will stop after {cfg.vad_silence_sec}s of silence "
          f"(threshold={cfg.vad_energy_threshold}).")
    result = record_until_silence(
        sample_rate=cfg.mic_sample_rate,
        silence_sec=cfg.vad_silence_sec,
        max_wait_sec=cfg.vad_max_wait_sec,
        max_utterance_sec=cfg.vad_max_utterance_sec,
        energy_threshold=cfg.vad_energy_threshold,
        device_index=cfg.mic_device_index if cfg.mic_device_index >= 0 else None,
    )
    if not result.spoken:
        print(f"No speech detected (peak_rms={result.peak_rms:.4f}). "
              f"Try --monitor to tune VAD_ENERGY_THRESHOLD.")
        return 1
    out = cfg.capture_dir / "mic_test.wav"
    out.write_bytes(result.audio_wav)
    print(f"Captured {result.duration_sec:.2f}s, peak_rms={result.peak_rms:.4f}")
    print(f"Saved: {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="List input devices")
    p.add_argument("--monitor", action="store_true", help="Stream live RMS to tune threshold")
    args = p.parse_args()

    if args.list:
        return _cmd_list()
    cfg = load_config()
    if args.monitor:
        return _cmd_monitor(cfg)
    return _cmd_record(cfg)


if __name__ == "__main__":
    sys.exit(main())
