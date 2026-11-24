"""Hum audio scoring: baseline-deviation signal processing (numpy only).

This is NOT a trained model. It compares a clip's average spectral shape with
the same appliance's own healthy baseline and reports how far it has drifted.
"""
from __future__ import annotations

import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SR = 16_000
FRAME = 2048
HOP = 1024
N_BANDS = 32
F_MIN, F_MAX = 50.0, 8000.0
STD_FLOOR = 0.05
MIN_SECONDS = 1.0

# Health mapping: health = clip(100 - (deviation - DEV_OFFSET) * SCALE, 0, 100).
# Tuned on the bundled samples so a fresh healthy take lands ~88-95 and the
# faulty sample ~50-65 (see verify_tools.py). Re-tune with real recordings.
DEV_OFFSET = -0.15
SCALE = 15.0

LOW_MAX_HZ = 300.0
MID_MAX_HZ = 2000.0

BAND_EDGES = np.logspace(np.log10(F_MIN), np.log10(F_MAX), N_BANDS + 1)
BAND_CENTERS = np.sqrt(BAND_EDGES[:-1] * BAND_EDGES[1:])
FEATURE_NAMES = [f"band_{int(round(c))}Hz" for c in BAND_CENTERS] + ["log_rms", "centroid_khz"]


class AudioError(ValueError):
    """Raised for unreadable or unusable audio."""


def _resample(y: np.ndarray, sr: int, target: int = SR) -> np.ndarray:
    if sr == target:
        return y
    n_out = int(round(len(y) * target / sr))
    x_old = np.linspace(0.0, 1.0, num=len(y), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, y).astype(np.float32)


def _load_with_wave(path: Path) -> tuple[np.ndarray, int]:
    try:
        with wave.open(str(path), "rb") as wf:
            n_ch, width, sr, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(n)
    except (wave.Error, EOFError) as exc:
        raise AudioError(
            f"Could not read {path.name} as PCM WAV ({exc}). Convert it to 16-bit PCM WAV, "
            "or `pip install soundfile` for more formats."
        ) from exc
    if width == 2:
        y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        y = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioError(f"Unsupported WAV sample width: {width * 8}-bit. Use 16-bit PCM.")
    if n_ch > 1:
        y = y[: len(y) - len(y) % n_ch].reshape(-1, n_ch).mean(axis=1)
    return y, sr


def load_audio(path: str | Path) -> np.ndarray:
    """Load audio as mono float32 at 16 kHz. Rejects clips shorter than 1 s."""
    path = Path(path)
    if not path.is_file():
        raise AudioError(f"Audio file not found: {path}")
    try:
        import soundfile as sf  # optional
    except ImportError:
        y, sr = _load_with_wave(path)
    else:
        try:
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        except Exception as exc:
            raise AudioError(f"Could not read {path.name}: {exc}") from exc
        y = data.mean(axis=1)
    y = _resample(np.asarray(y, dtype=np.float32), int(sr))
    if len(y) < SR * MIN_SECONDS:
        raise AudioError(f"Clip is {len(y) / SR:.2f} s long; record at least {MIN_SECONDS:.0f} s (10 s is best).")
    if float(np.max(np.abs(y))) < 1e-4:
        raise AudioError("Clip is silent. Hold the phone 10-20 cm from the appliance while it runs.")
    return y


def frame_features(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """Per-frame features: 32 log-band energies (log10), log10 RMS, spectral centroid (kHz).

    Returns an array of shape (n_frames, 34).
    """
    if len(y) < FRAME:
        y = np.pad(y, (0, FRAME - len(y)))
    n_frames = 1 + (len(y) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hanning(FRAME)[None, :]
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(FRAME, d=1.0 / sr)

    bands = np.empty((n_frames, N_BANDS), dtype=np.float64)
    for b in range(N_BANDS):
        lo, hi = BAND_EDGES[b], BAND_EDGES[b + 1]
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():  # very narrow low band: use the nearest bin
            mask = np.zeros_like(freqs, dtype=bool)
            mask[int(np.argmin(np.abs(freqs - BAND_CENTERS[b])))] = True
        bands[:, b] = np.log10(power[:, mask].sum(axis=1) + 1e-10)

    raw = y[idx]
    log_rms = np.log10(np.sqrt(np.mean(raw**2, axis=1)) + 1e-8)
    centroid = (power * freqs[None, :]).sum(axis=1) / (power.sum(axis=1) + 1e-12) / 1000.0
    return np.column_stack([bands, log_rms, centroid])


def build_profile(path: str | Path) -> dict:
    """Healthy profile: per-feature mean and std over frames (std floor 0.05). JSON-serialisable."""
    feats = frame_features(load_audio(path))
    return {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": Path(path).name,
        "n_frames": int(feats.shape[0]),
        "feature_names": FEATURE_NAMES,
        "mean": [round(float(v), 5) for v in feats.mean(axis=0)],
        "std": [round(float(v), 5) for v in np.maximum(feats.std(axis=0), STD_FLOOR)],
    }


def health_from_deviation(deviation: float) -> float:
    return float(np.clip(100.0 - (deviation - DEV_OFFSET) * SCALE, 0.0, 100.0))


def score_against(profile: dict, path: str | Path) -> dict:
    """Score a clip against a healthy profile.

    deviation = mean |z| of the clip's mean features vs the baseline (mean, std).
    """
    mean = np.asarray(profile["mean"], dtype=np.float64)
    std = np.asarray(profile["std"], dtype=np.float64)
    clip_mean = frame_features(load_audio(path)).mean(axis=0)
    z = (clip_mean - mean) / std
    deviation = float(np.mean(np.abs(z)))

    band_z = np.abs(z[:N_BANDS])
    regions = {
        "low": band_z[BAND_CENTERS < LOW_MAX_HZ],
        "mid": band_z[(BAND_CENTERS >= LOW_MAX_HZ) & (BAND_CENTERS < MID_MAX_HZ)],
        "high": band_z[BAND_CENTERS >= MID_MAX_HZ],
    }
    region_scores = {k: round(float(v.mean()), 2) for k, v in regions.items()}
    changed_region = max(region_scores, key=region_scores.get)

    loud_db = 20.0 * float(clip_mean[N_BANDS] - mean[N_BANDS])
    centroid_hz = 1000.0 * float(clip_mean[N_BANDS + 1] - mean[N_BANDS + 1])
    return {
        "health": round(health_from_deviation(deviation), 1),
        "deviation": round(deviation, 3),
        "changed_region": changed_region,
        "region_change_scores": region_scores,
        "loudness_change_db": round(loud_db, 1),
        "centroid_change_hz": round(centroid_hz, 0),
    }
