"""Sample-peak and true-peak (inter-sample) measurement by polyphase oversampling.

Oversampling factor: 4x below 96 kHz, 2x below 192 kHz (BS.1770-4 Annex 2 guidance).
The interpolation filter is SciPy's Kaiser-windowed polyphase FIR (resample_poly),
processed in overlapping chunks to bound memory. Cross-checked in tests against FFmpeg
``ebur128=peak=true``.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy.signal import firwin, resample_poly

CHUNK = 1 << 17
PAD = 256


def oversample_factor(fs: int) -> int:
    if fs < 96000:
        return 4
    if fs < 192000:
        return 2
    return 1


@lru_cache(maxsize=8)
def _interp_filter(up: int) -> np.ndarray:
    # Long Kaiser-windowed sinc (64 taps per phase): passband ripple << 0.01 dB below 0.45*fs.
    return firwin(up * 64 + 1, 1.0 / up, window=("kaiser", 9.0))  # resample_poly applies the xup gain


def db(v: float) -> float:
    return float(20.0 * np.log10(max(float(v), 1e-12)))


def peak_envelope(x: np.ndarray, fs: int, factor: int | None = None) -> np.ndarray:
    """Per input sample: max |interpolated value| over the oversampled phases, linked across channels.

    Returns an array of length n (one value per original sample).
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    n = x.shape[0]
    up = factor or oversample_factor(fs)
    env = np.abs(x).max(axis=1)
    if up == 1:
        return env
    out = np.empty(n)
    for start in range(0, n, CHUNK):
        stop = min(n, start + CHUNK)
        a = max(0, start - PAD)
        b = min(n, stop + PAD)
        seg = resample_poly(x[a:b], up, 1, axis=0, window=_interp_filter(up))
        seg = np.abs(seg).max(axis=1)
        seg = seg[: (b - a) * up].reshape(b - a, up).max(axis=1)
        out[start:stop] = seg[start - a: start - a + (stop - start)]
    return np.maximum(out, env)


def true_peak_per_channel(x: np.ndarray, fs: int) -> list[float]:
    x = np.asarray(x)
    if x.ndim == 1:
        x = x[:, None]
    return [float(peak_envelope(x[:, c], fs).max()) for c in range(x.shape[1])]


def measure_peaks(x: np.ndarray, fs: int) -> dict:
    x = np.asarray(x)
    if x.ndim == 1:
        x = x[:, None]
    sp = np.abs(x).max(axis=0)
    tp = true_peak_per_channel(x, fs)
    env = peak_envelope(x, fs)
    return {
        "sample_peak_dbfs": round(db(sp.max()), 3),
        "sample_peak_dbfs_per_channel": [round(db(v), 3) for v in sp],
        "true_peak_dbtp": round(db(max(tp)), 3),
        "true_peak_dbtp_per_channel": [round(db(v), 3) for v in tp],
        "oversampling": oversample_factor(fs),
        "intersample_over_count": int(np.count_nonzero((env > 1.0) & (np.abs(x).max(axis=1) <= 1.0))),
        "samples_at_or_above_0dbfs": int(np.count_nonzero(np.abs(x) >= 1.0)),
    }
