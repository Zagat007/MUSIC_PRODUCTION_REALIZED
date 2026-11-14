"""ITU-R BS.1770-4 / EBU R128 loudness: integrated, momentary, short-term and LRA.

K-weighting coefficients are derived for arbitrary sample rates using the same analog
prototype as libebur128. Results are cross-validated in the test-suite against
``pyloudnorm`` and FFmpeg's ``ebur128`` filter.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import lfilter

ABS_GATE = -70.0


def k_weighting_coeffs(fs: float):
    f0 = 1681.974450955533
    G = 3.999843853973347
    Q = 0.7071752369554196
    K = np.tan(np.pi * f0 / fs)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b1 = np.array([(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0])
    a1 = np.array([1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0])
    f0 = 38.13547087602444
    Q = 0.5003270373238773
    K = np.tan(np.pi * f0 / fs)
    b2 = np.array([1.0, -2.0, 1.0])
    d = 1.0 + K / Q + K * K
    a2 = np.array([1.0, 2.0 * (K * K - 1.0) / d, (1.0 - K / Q + K * K) / d])
    return (b1, a1), (b2, a2)


def channel_weights(channels: int) -> np.ndarray:
    if channels == 6:  # L R C LFE Ls Rs
        return np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41])
    if channels == 5:
        return np.array([1.0, 1.0, 1.0, 1.41, 1.41])
    return np.ones(channels)


def _lufs(ms: np.ndarray | float) -> np.ndarray | float:
    with np.errstate(divide="ignore"):
        return -0.691 + 10.0 * np.log10(np.maximum(ms, 1e-30))


@dataclass
class LoudnessResult:
    integrated_lufs: float | None
    lra_lu: float | None
    momentary_max_lufs: float | None
    short_term_max_lufs: float | None
    momentary: np.ndarray  # LUFS every 100 ms (400 ms window)
    short_term: np.ndarray  # LUFS every 100 ms (3 s window)
    hop_s: float = 0.1

    def summary(self) -> dict:
        def r(v):
            return None if v is None or not np.isfinite(v) else round(float(v), 2)

        return {
            "integrated_lufs": r(self.integrated_lufs),
            "lra_lu": r(self.lra_lu),
            "momentary_max_lufs": r(self.momentary_max_lufs),
            "short_term_max_lufs": r(self.short_term_max_lufs),
        }


def segment_powers(x: np.ndarray, fs: int, hop_s: float = 0.1) -> np.ndarray:
    """Channel-weighted mean square of K-weighted signal per ``hop_s`` segment."""
    (b1, a1), (b2, a2) = k_weighting_coeffs(fs)
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    y = lfilter(b2, a2, lfilter(b1, a1, x, axis=0), axis=0)
    hop = int(round(hop_s * fs))
    nseg = y.shape[0] // hop
    if nseg == 0:
        return np.zeros(0)
    y = y[: nseg * hop]
    w = channel_weights(x.shape[1])
    ms = (y.reshape(nseg, hop, x.shape[1]) ** 2).mean(axis=1)  # (nseg, ch)
    return ms @ w


def _windowed(seg: np.ndarray, k: int) -> np.ndarray:
    if seg.size < k:
        return np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(seg)])
    return (c[k:] - c[:-k]) / k


def integrated_from_blocks(blocks_ms: np.ndarray) -> float | None:
    if blocks_ms.size == 0:
        return None
    l = _lufs(blocks_ms)
    gated = blocks_ms[l > ABS_GATE]
    if gated.size == 0:
        return None
    rel = _lufs(gated.mean()) - 10.0
    gated2 = blocks_ms[(l > ABS_GATE) & (l > rel)]
    if gated2.size == 0:
        return None
    return float(_lufs(gated2.mean()))


def lra_from_short_term(st_ms: np.ndarray) -> float | None:
    if st_ms.size == 0:
        return None
    l = _lufs(st_ms)
    g = st_ms[l > ABS_GATE]
    if g.size == 0:
        return None
    rel = _lufs(g.mean()) - 20.0
    lg = _lufs(st_ms[(l > ABS_GATE) & (l > rel)])
    if lg.size < 2:
        return 0.0
    return float(np.percentile(lg, 95) - np.percentile(lg, 10))


def measure_loudness(x: np.ndarray, fs: int) -> LoudnessResult:
    seg = segment_powers(x, fs, 0.1)
    mom_ms = _windowed(seg, 4)
    st_ms = _windowed(seg, 30)
    integ = integrated_from_blocks(mom_ms)
    lra = lra_from_short_term(st_ms)
    mom = _lufs(mom_ms) if mom_ms.size else np.zeros(0)
    st = _lufs(st_ms) if st_ms.size else np.zeros(0)
    return LoudnessResult(
        integrated_lufs=integ,
        lra_lu=lra,
        momentary_max_lufs=float(mom.max()) if mom.size else None,
        short_term_max_lufs=float(st.max()) if st.size else None,
        momentary=np.asarray(mom, dtype=np.float32),
        short_term=np.asarray(st, dtype=np.float32),
    )


def integrated_lufs(x: np.ndarray, fs: int) -> float | None:
    return integrated_from_blocks(_windowed(segment_powers(x, fs, 0.1), 4))
