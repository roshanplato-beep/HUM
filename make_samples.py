"""Generate SYNTHETIC demo recordings for Hum (no real appliance needed).

samples/mixer_healthy.wav    motor tone + harmonics + mild noise (baseline)
samples/mixer_healthy_2.wav  second healthy take, different noise seed / tiny speed drift
samples/mixer_faulty.wav     same motor + intermittent high-frequency squeal + rattle bursts
data/references/mixer.json   generic "mixer" reference profile used when no baseline exists

These are synthetic. Replace them with real 10 s recordings of your own fan or mixer
(normal vs. a pen lightly touching the blade guard / a loose lid) for a real demo.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
SR = 16_000
SECONDS = 10.0


def motor(rng: np.random.Generator, f0: float = 190.0, speed: float = 1.0, gain_db: float = 0.0) -> np.ndarray:
    t = np.arange(int(SR * SECONDS)) / SR
    wobble = 1.0 + 0.004 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 6.28))
    phase = 2 * np.pi * f0 * speed * np.cumsum(wobble) / SR
    y = np.zeros_like(t)
    for k, amp in enumerate([1.0, 0.55, 0.35, 0.22, 0.14, 0.09, 0.06, 0.04], start=1):
        y += amp * np.sin(k * phase + rng.uniform(0, 6.28))
    y *= 1.0 + 0.05 * np.sin(2 * np.pi * 3.1 * t)  # gentle load modulation
    noise = rng.normal(0, 1, len(t))
    noise = np.convolve(noise, np.ones(4) / 4, mode="same")  # slightly coloured
    y = 0.22 * y / np.max(np.abs(y)) + 0.012 * noise
    return y * 10 ** (gain_db / 20)


def add_faults(y: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(len(y)) / SR
    out = y.copy()
    # Intermittent squeal: ~3.4 kHz with frequency wobble, on for ~0.5 s every ~1.3 s.
    gate = (np.sin(2 * np.pi * t / 1.3) > 0.25).astype(float)
    gate = np.convolve(gate, np.hanning(800) / np.hanning(800).sum(), mode="same")
    f = 3400 + 120 * np.sin(2 * np.pi * 5 * t)
    out += 0.030 * gate * np.sin(2 * np.pi * np.cumsum(f) / SR)
    # Rattle bursts: short decaying high-passed clicks in irregular clusters.
    click = rng.normal(0, 1, 240) * np.exp(-np.arange(240) / 40)
    click = np.diff(click, prepend=0.0)
    for start in rng.uniform(0, SECONDS - 0.05, 70):
        i = int(start * SR)
        out[i : i + len(click)] += 0.06 * rng.uniform(0.5, 1.0) * click[: len(out) - i]
    return out


def write_wav(path: Path, y: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def main() -> None:
    samples = ROOT / "samples"
    write_wav(samples / "mixer_healthy.wav", motor(np.random.default_rng(1)))
    write_wav(samples / "mixer_healthy_2.wav", motor(np.random.default_rng(2), speed=1.003, gain_db=0.3))
    write_wav(samples / "mixer_faulty.wav", add_faults(motor(np.random.default_rng(7), speed=0.997), np.random.default_rng(8)))

    # Generic reference for the "mixer" type (cold start). Built from a separate synthetic take
    # with a slightly different motor speed, so it is less precise than a real own-baseline.
    from audio_features import build_profile

    ref_wav = ROOT / "samples" / "_mixer_reference.wav"
    write_wav(ref_wav, motor(np.random.default_rng(3), f0=193.0, gain_db=-0.5))
    profile = build_profile(ref_wav)
    # A generic reference must tolerate more variation than an appliance's own baseline.
    profile["std"] = [round(v * 2.0, 5) for v in profile["std"]]
    profile["note"] = "Generic synthetic reference for appliance_type=mixer. Less accurate than the appliance's own baseline."
    refs = ROOT / "data" / "references"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "mixer.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    ref_wav.unlink()
    print("Wrote synthetic samples to", samples)
    print("Wrote generic mixer reference to", refs / "mixer.json")


if __name__ == "__main__":
    main()
