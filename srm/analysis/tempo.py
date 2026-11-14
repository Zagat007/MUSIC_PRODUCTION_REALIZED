"""Basic tempo estimation (spectral-flux onset envelope + autocorrelation).

Deliberately reports *ambiguity*: half-/double-time candidates (e.g. 87 vs 174 BPM)
are always listed with relative scores. No claim of beat-accurate tracking.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import stft


def onset_envelope(x: np.ndarray, fs: int, hop: int = 512) -> tuple[np.ndarray, float]:
    mono = np.asarray(x, dtype=np.float64).mean(axis=1)
    # Cap analysis at 180 s from the middle of the track for speed
    max_n = 180 * fs
    if len(mono) > max_n:
        s = (len(mono) - max_n) // 2
        mono = mono[s: s + max_n]
    _, _, Z = stft(mono, fs=fs, nperseg=2048, noverlap=2048 - hop, boundary=None, padded=False)
    mag = np.log1p(100.0 * np.abs(Z))
    flux = np.maximum(0.0, np.diff(mag, axis=1)).sum(axis=0)
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")
    flux = np.maximum(flux, 0.0)
    return flux, fs / hop


def estimate_tempo(x: np.ndarray, fs: int, lo: float = 55.0, hi: float = 200.0) -> dict:
    env, frame_rate = onset_envelope(x, fs)
    if env.size < frame_rate * 8 or env.std() < 1e-9:
        return {"status": "UNRELIABLE", "reason": "too short or no rhythmic onsets", "candidates": []}
    env = (env - env.mean()) / (env.std() + 1e-12)
    n = len(env)
    ac = np.fft.irfft(np.abs(np.fft.rfft(env, 2 * n)) ** 2)[:n]
    ac /= ac[0] + 1e-12
    lags = np.arange(1, n)
    bpm = 60.0 * frame_rate / lags
    mask = (bpm >= lo) & (bpm <= hi)
    if not mask.any():
        return {"status": "UNRELIABLE", "reason": "no lag in range", "candidates": []}
    lag_sel = lags[mask]
    vals = ac[lag_sel]
    # Score each lag by its own peak plus harmonics (reduces octave jitter)
    scores = vals.copy()
    for k, lag in enumerate(lag_sel):
        if 2 * lag < n:
            scores[k] += 0.5 * ac[2 * lag]
    # Log-Gaussian tempo prior (centre 120 BPM, 1 octave s.d.) discourages 1/3- and 1/4-tempo locks
    prior = np.exp(-0.5 * (np.log2(bpm[mask] / 120.0)) ** 2)
    scores = np.maximum(scores, 0) * prior
    best_i = int(np.argmax(scores))
    best_lag = lag_sel[best_i]
    # parabolic refinement
    if 1 <= best_lag < n - 1:
        y0, y1, y2 = ac[best_lag - 1], ac[best_lag], ac[best_lag + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
    else:
        off = 0.0
    best_bpm = 60.0 * frame_rate / (best_lag + off)

    def score_at(b):
        if b < 30 or b > 400:
            return None
        lag = 60.0 * frame_rate / b
        li = int(round(lag))
        if li < 1 or li >= n:
            return None
        return float(ac[max(1, li - 1): li + 2].max())

    # Top distinct periodicity peaks (local maxima of the prior-weighted score)
    peaks_idx = [i for i in range(1, len(scores) - 1) if scores[i] >= scores[i - 1] and scores[i] >= scores[i + 1]]
    peaks_idx.sort(key=lambda i: -scores[i])
    tops: list[float] = []
    for i in peaks_idx:
        b = 60.0 * frame_rate / lag_sel[i]
        if all(abs(b / t - 1) > 0.04 for t in tops):
            tops.append(b)
        if len(tops) >= 3:
            break
    seeds = [best_bpm] + [t for t in tops if abs(t / best_bpm - 1) > 0.04]
    rel_names = {1.0: "primary", 2.0: "double_time", 0.5: "half_time", 1.5: "x1.5", 2 / 3: "x2/3",
                 3.0: "x3", 1 / 3: "x1/3", 0.75: "x3/4", 4 / 3: "x4/3"}
    cands: list[dict] = []
    for sd in seeds:
        for mult in (1.0, 2.0, 0.5):
            b = sd * mult
            if not 40 <= b <= 250 or any(abs(b / c["bpm"] - 1) < 0.03 for c in cands):
                continue
            s_ = score_at(b)
            if s_ is None:
                continue
            ratio = b / best_bpm
            rel = min(rel_names, key=lambda r: abs(np.log(ratio / r)))
            rel = rel_names[rel] if abs(np.log(ratio / rel)) < 0.03 else f"x{ratio:.2f}"
            cands.append({"bpm": round(b, 1), "relation": rel, "autocorr": round(s_, 3)})
    peak_val = float(vals[best_i])
    conf = "LOW" if peak_val < 0.15 else ("MEDIUM" if peak_val < 0.35 else "HIGH")
    amb = [c for c in cands if c["relation"] != "primary" and c["autocorr"] > 0.6 * peak_val]
    return {
        "status": "ESTIMATE",
        "bpm": round(best_bpm, 1),
        "confidence": conf,
        "octave_ambiguous": bool(amb),
        "candidates": cands,
        "note": ("Autocorrelation estimate. Listed alternatives (half/double time, e.g. 87 vs 174 BPM, and other strong "
                 "periodicities) may be the musically correct reading; confirm by listening."),
    }
