"""Level, dynamics, stereo, spectral and silence measurements."""

from __future__ import annotations

import numpy as np
from scipy.signal import welch

from srm.analysis.loudness import integrated_lufs
from srm.analysis.truepeak import db

BANDS = [
    ("sub_20_40", 20, 40),
    ("bass_40_120", 40, 120),
    ("lowmid_120_500", 120, 500),
    ("mid_500_2k", 500, 2000),
    ("highmid_2k_6k", 2000, 6000),
    ("high_6k_12k", 6000, 12000),
    ("air_12k_20k", 12000, 20000),
]
SILENCE_DBFS = -60.0


def _r(v, n=3):
    if v is None:
        return None
    v = float(v)
    return round(v, n) if np.isfinite(v) else None


def level_dynamics(x: np.ndarray, fs: int, integrated: float | None, true_peak_dbtp: float) -> dict:
    x64 = np.asarray(x, dtype=np.float64)
    rms = np.sqrt(np.mean(x64 ** 2, axis=0))
    peak = np.abs(x64).max(axis=0)
    dc = x64.mean(axis=0)
    rms_all = float(np.sqrt(np.mean(x64 ** 2)))
    crest = db(peak.max()) - db(rms_all) if rms_all > 0 else None
    # Short-window crest distribution (indicates brickwall limiting when uniformly low)
    win = int(0.05 * fs)
    nwin = x64.shape[0] // win
    loud_crest = None
    near_max_density = None
    if nwin > 10:
        m = np.abs(x64[: nwin * win]).max(axis=1).reshape(nwin, win)
        pk = m.max(axis=1)
        r = np.sqrt((x64[: nwin * win] ** 2).mean(axis=1).reshape(nwin, win).mean(axis=1))
        loud = r > (10 ** (-30 / 20))
        if loud.any():
            loud_crest = float(np.median(20 * np.log10(pk[loud] / np.maximum(r[loud], 1e-12))))
            gmax = pk.max()
            near_max_density = float(np.mean(pk[loud] > gmax * 10 ** (-0.5 / 20)))
    out = {
        "rms_dbfs_per_channel": [_r(db(v), 2) for v in rms],
        "dc_offset_per_channel": [_r(v, 6) for v in dc],
        "dc_offset_dbfs_max": _r(db(np.abs(dc).max()), 1),
        "crest_factor_db": _r(crest, 2),
        "median_short_window_crest_db": _r(loud_crest, 2),
        "near_max_peak_window_ratio": _r(near_max_density, 3),
        "plr_db": _r(true_peak_dbtp - integrated, 2) if integrated is not None else None,
    }
    if x64.shape[1] >= 2:
        out["channel_rms_imbalance_db"] = _r(db(rms[0]) - db(rms[1]), 2)
        out["channel_peak_imbalance_db"] = _r(db(peak[0]) - db(peak[1]), 2)
    return out


def stereo_metrics(x: np.ndarray, fs: int) -> dict:
    x64 = np.asarray(x, dtype=np.float64)
    if x64.shape[1] < 2:
        return {"channels": int(x64.shape[1]), "is_mono_file": True}
    L, R = x64[:, 0], x64[:, 1]
    identical = bool(np.array_equal(L, R))
    def corr(a, b):
        sa, sb = a.std(), b.std()
        if sa < 1e-12 or sb < 1e-12:
            return None
        return float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))
    c = corr(L, R)
    mid, side = 0.5 * (L + R), 0.5 * (L - R)
    em, es = float(np.mean(mid ** 2)), float(np.mean(side ** 2))
    side_to_mid = db(np.sqrt(es)) - db(np.sqrt(em)) if em > 0 else None
    # Windowed correlation: find worst sections (phase problems)
    win = int(0.5 * fs)
    nwin = len(L) // win
    worst = []
    if nwin > 0:
        Lw = L[: nwin * win].reshape(nwin, win)
        Rw = R[: nwin * win].reshape(nwin, win)
        Lc = Lw - Lw.mean(axis=1, keepdims=True)
        Rc = Rw - Rw.mean(axis=1, keepdims=True)
        num = (Lc * Rc).mean(axis=1)
        den = Lc.std(axis=1) * Rc.std(axis=1)
        energy = (Lw ** 2 + Rw ** 2).mean(axis=1)
        valid = (den > 1e-9) & (energy > 1e-6)
        cw = np.where(valid, num / np.where(den > 0, den, 1), np.nan)
        order = np.argsort(np.nan_to_num(cw, nan=2.0))
        for i in order[:5]:
            if np.isfinite(cw[i]) and cw[i] < 0.0:
                worst.append({"start_s": round(i * 0.5, 2), "end_s": round((i + 1) * 0.5, 2),
                              "correlation": round(float(cw[i]), 3)})
    st_l = integrated_lufs(x64[:, :2], fs)
    mono_l = integrated_lufs(np.stack([mid, mid], axis=1), fs)
    mono_loss = (st_l - mono_l) if (st_l is not None and mono_l is not None) else None
    return {
        "channels": int(x64.shape[1]),
        "dual_mono_identical_channels": identical,
        "correlation": _r(c, 3),
        "side_to_mid_db": _r(side_to_mid, 2),
        "mono_downmix_loudness_loss_lu": _r(mono_loss, 2),
        "worst_correlation_windows": worst,
    }


def spectral_metrics(x: np.ndarray, fs: int) -> dict:
    x64 = np.asarray(x, dtype=np.float64)
    mono = x64.mean(axis=1)
    # Limit work on long files: analyse up to ~4 minutes worth of samples spread over the file
    nper = 8192
    f, p = welch(mono, fs=fs, nperseg=nper, noverlap=nper // 2, scaling="spectrum")
    total = p[(f >= 20) & (f <= min(20000, fs / 2))].sum()
    bands = {}
    for name, lo, hi in BANDS:
        if lo >= fs / 2:
            bands[name] = None
            continue
        e = p[(f >= lo) & (f < min(hi, fs / 2))].sum()
        bands[name] = _r(10 * np.log10(max(e / total, 1e-15)), 2) if total > 0 else None
    # Effective bandwidth: highest frequency whose smoothed level is within 60 dB of the 1-4 kHz median.
    ref = np.median(p[(f >= 1000) & (f <= 4000)]) if np.any((f >= 1000) & (f <= 4000)) else 0
    bw = None
    if ref > 0:
        ker = np.ones(9) / 9
        ps = np.convolve(p, ker, mode="same")
        above = np.where(ps > ref * 1e-6)[0]
        if above.size:
            bw = float(f[above.max()])
    # Lowpass "shelf" typical of lossy encoders: sharp drop between bands
    cutoff = None
    if bw is not None and bw < 0.95 * fs / 2:
        cutoff = bw
    centroid = float((f * p).sum() / p.sum()) if p.sum() > 0 else None
    return {
        "band_energy_db_rel": bands,
        "spectral_centroid_hz": _r(centroid, 1),
        "effective_bandwidth_hz": _r(bw, 0),
        "possible_encoder_lowpass_hz": _r(cutoff, 0),
    }


def silence_metrics(x: np.ndarray, fs: int) -> dict:
    x64 = np.asarray(x, dtype=np.float64)
    a = np.abs(x64).max(axis=1)
    thr = 10 ** (SILENCE_DBFS / 20)
    nz = np.where(a > thr)[0]
    n = len(a)
    if nz.size == 0:
        return {"entirely_silent": True, "leading_silence_s": n / fs, "trailing_silence_s": n / fs}
    lead = nz[0] / fs
    trail = (n - 1 - nz[-1]) / fs
    # Abrupt start/end: energy in first/last 10 ms after/before silence trimmed.
    w = max(1, int(0.01 * fs))
    head = x64[nz[0]: nz[0] + w]
    tail = x64[max(0, nz[-1] - w + 1): nz[-1] + 1]
    head_db = db(np.sqrt(np.mean(head ** 2))) if head.size else -120
    tail_db = db(np.sqrt(np.mean(tail ** 2))) if tail.size else -120
    after = x64[nz[0] + w: nz[0] + 5 * w]
    before = x64[max(0, nz[-1] - 5 * w): max(0, nz[-1] - w)]
    after_db = db(np.sqrt(np.mean(after ** 2))) if after.size else -120
    before_db = db(np.sqrt(np.mean(before ** 2))) if before.size else -120
    # Abrupt = content starts/stops at full level without a ramp (fade) and without leading/trailing silence.
    abrupt_start = bool(lead < 0.002 and head_db > -30 and head_db >= after_db - 3)
    abrupt_end = bool(trail < 0.002 and tail_db > -30 and tail_db >= before_db - 3)
    # Per-channel absence
    ch_peaks = np.abs(x64).max(axis=0)
    silent_channels = [int(i) for i, v in enumerate(ch_peaks) if v <= thr]
    # Digital silence runs (exact zeros) longer than 0.5 s inside the programme
    zeros = np.all(x64 == 0.0, axis=1)
    runs = []
    if zeros.any():
        d = np.diff(np.concatenate([[0], zeros.astype(np.int8), [0]]))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            if (e - s) / fs >= 0.5 and s > nz[0] and e < nz[-1]:
                runs.append({"start_s": round(s / fs, 3), "end_s": round(e / fs, 3)})
    return {
        "entirely_silent": False,
        "leading_silence_s": round(lead, 3),
        "trailing_silence_s": round(trail, 3),
        "first_10ms_rms_dbfs": _r(head_db, 1),
        "abrupt_start": abrupt_start,
        "abrupt_end": abrupt_end,
        "last_10ms_rms_dbfs": _r(tail_db, 1),
        "silent_channels": silent_channels,
        "internal_digital_silence_runs": runs[:20],
    }


def waveform_overview(x: np.ndarray, points: int = 1500) -> dict:
    x = np.asarray(x, dtype=np.float32)
    mono_abs = np.abs(x).max(axis=1)
    n = len(mono_abs)
    points = max(1, min(points, n))
    edges = np.linspace(0, n, points + 1).astype(int)
    peaks = np.maximum.reduceat(mono_abs, edges[:-1])
    return {"points": points, "peak": [round(float(v), 4) for v in peaks]}
