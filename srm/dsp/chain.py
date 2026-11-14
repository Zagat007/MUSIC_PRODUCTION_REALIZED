"""Modular, bounded, non-destructive mastering chain.

Stage order (fixed): dc_hpf -> eq -> compressor -> stereo -> gain -> limiter.
Every stage has ``enabled`` (bypass) and bounded parameters; out-of-bounds values are
rejected, never silently clamped. All filters are causal IIR applied offline; the
limiter's look-ahead is implemented without delay, so the output has exactly the same
sample count and alignment as the input (zero latency, verified in tests).
IIR EQ introduces frequency-dependent (minimum) phase shift; this is documented.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import minimum_filter1d
from scipy.signal import butter, sosfilt

from srm.analysis.loudness import integrated_lufs
from srm.analysis.truepeak import db, peak_envelope

STAGES = ("dc_hpf", "eq", "compressor", "stereo", "gain", "limiter")

BOUNDS = {
    "dc_hpf.freq_hz": (3.0, 40.0),
    "eq.gain_db": (-4.0, 4.0),
    "eq.freq_hz": (20.0, 20000.0),
    "eq.q": (0.3, 4.0),
    "compressor.threshold_db": (-40.0, 0.0),
    "compressor.ratio": (1.0, 3.0),
    "compressor.attack_ms": (1.0, 200.0),
    "compressor.release_ms": (20.0, 1000.0),
    "compressor.knee_db": (0.0, 12.0),
    "stereo.width": (0.7, 1.3),
    "stereo.bass_mono_below_hz": (0.0, 200.0),
    "gain.gain_db": (-24.0, 12.0),
    "limiter.ceiling_dbtp": (-6.0, -0.1),
    "limiter.lookahead_ms": (0.5, 10.0),
    "limiter.release_ms": (10.0, 1000.0),
}
EQ_TYPES = ("peak", "low_shelf", "high_shelf", "highpass", "lowpass")


class ChainConfigError(ValueError):
    pass


def default_chain() -> dict:
    return {
        "dc_hpf": {"enabled": False, "freq_hz": 5.0},
        "eq": {"enabled": False, "bands": []},
        "compressor": {"enabled": False, "threshold_db": -18.0, "ratio": 1.5, "attack_ms": 30.0,
                       "release_ms": 250.0, "knee_db": 6.0},
        "stereo": {"enabled": False, "width": 1.0, "bass_mono_below_hz": 0.0},
        "gain": {"enabled": False, "gain_db": 0.0},
        "limiter": {"enabled": True, "ceiling_dbtp": -1.0, "lookahead_ms": 1.5, "release_ms": 80.0},
    }


def _chk(key: str, v) -> None:
    lo, hi = BOUNDS[key]
    if not isinstance(v, (int, float)) or not np.isfinite(v) or not (lo <= float(v) <= hi):
        raise ChainConfigError(f"{key}={v!r} outside allowed bounds [{lo}, {hi}]")


def validate_chain(cfg: dict) -> dict:
    out = copy.deepcopy(default_chain())
    for st, params in (cfg or {}).items():
        if st not in STAGES:
            raise ChainConfigError(f"unknown stage {st!r}")
        out[st].update(params)
    for st in STAGES:
        p = out[st]
        if not isinstance(p.get("enabled"), bool):
            raise ChainConfigError(f"{st}.enabled must be boolean")
        for k, v in p.items():
            if k in ("enabled", "bands", "note"):
                continue
            key = f"{st}.{k}"
            if key not in BOUNDS:
                raise ChainConfigError(f"unknown parameter {key}")
            _chk(key, v)
    for i, b in enumerate(out["eq"]["bands"]):
        if b.get("type") not in EQ_TYPES:
            raise ChainConfigError(f"eq band {i}: type must be one of {EQ_TYPES}")
        _chk("eq.freq_hz", b.get("freq_hz"))
        _chk("eq.q", b.get("q", 0.707))
        if b["type"] in ("peak", "low_shelf", "high_shelf"):
            _chk("eq.gain_db", b.get("gain_db"))
    return out


# ---------------------------------------------------------------- filters
def rbj_sos(kind: str, f0: float, fs: int, gain_db: float = 0.0, q: float = 0.707) -> np.ndarray:
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * min(f0, 0.49 * fs) / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * q)
    if kind == "peak":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind in ("low_shelf", "high_shelf"):
        s = 2 * np.sqrt(A) * alpha
        if kind == "low_shelf":
            b = [A * ((A + 1) - (A - 1) * cw + s), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - s)]
            a = [(A + 1) + (A - 1) * cw + s, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - s]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + s), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - s)]
            a = [(A + 1) - (A - 1) * cw + s, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - s]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    else:
        raise ChainConfigError(kind)
    b = np.array(b) / a[0]
    a = np.array(a) / a[0]
    return np.concatenate([b, a])[None, :]


def stage_dc_hpf(x, fs, p):
    sos = butter(2, p["freq_hz"], btype="highpass", fs=fs, output="sos")
    return sosfilt(sos, x, axis=0), {}


def stage_eq(x, fs, p):
    if not p["bands"]:
        return x, {"bands": 0}
    sos = np.concatenate([rbj_sos(b["type"], b["freq_hz"], fs, b.get("gain_db", 0.0), b.get("q", 0.707))
                          for b in p["bands"]])
    return sosfilt(sos, x, axis=0), {"bands": len(p["bands"])}


def _block_gain_smooth(target_db_blocks: np.ndarray, att_coef: float, rel_coef: float) -> np.ndarray:
    """Attack/release smoothing of a gain-reduction curve (dB, <=0) in the block domain."""
    out = np.empty_like(target_db_blocks)
    g = 0.0
    for i, t in enumerate(target_db_blocks.tolist()):
        coef = att_coef if t < g else rel_coef
        g = t + (g - t) * coef
        out[i] = g
    return out


def stage_compressor(x, fs, p, block: int = 64):
    n = x.shape[0]
    nb = int(np.ceil(n / block))
    pad = nb * block - n
    xx = np.pad(x, ((0, pad), (0, 0))) if pad else x
    ms = (xx.reshape(nb, block, -1) ** 2).mean(axis=1).max(axis=1)  # linked: loudest channel
    # RMS detector with ~10 ms integration
    a = np.exp(-block / (0.010 * fs))
    from scipy.signal import lfilter

    ms = lfilter([1 - a], [1, -a], ms)
    lvl = 10 * np.log10(np.maximum(ms, 1e-12)) + 3.01  # sine-RMS to peak-ish reference
    T, R, K = p["threshold_db"], p["ratio"], p["knee_db"]
    over = lvl - T
    gr = np.zeros_like(lvl)
    if K > 0:
        knee = (over > -K / 2) & (over < K / 2)
        gr[knee] = (1 / R - 1) * (over[knee] + K / 2) ** 2 / (2 * K)
    hard = over >= K / 2
    gr[hard] = (1 / R - 1) * over[hard]
    att = np.exp(-block / (p["attack_ms"] / 1000 * fs))
    rel = np.exp(-block / (p["release_ms"] / 1000 * fs))
    grs = _block_gain_smooth(gr, att, rel)
    centers = (np.arange(nb) + 0.5) * block
    g = 10 ** (np.interp(np.arange(n), centers, grs) / 20)
    return x * g[:, None], {"max_gr_db": round(float(-grs.min()), 2) if nb else 0.0,
                            "mean_gr_db": round(float(-grs.mean()), 2) if nb else 0.0}


def stage_stereo(x, fs, p):
    if x.shape[1] != 2:
        return x, {"skipped": "not stereo"}
    mid = 0.5 * (x[:, 0] + x[:, 1])
    side = 0.5 * (x[:, 0] - x[:, 1]) * p["width"]
    if p["bass_mono_below_hz"] > 0:
        sos = butter(2, p["bass_mono_below_hz"], btype="lowpass", fs=fs, output="sos")
        side = side - sosfilt(sos, sosfilt(sos, side))  # LR4-style low band removed from side only
    return np.stack([mid + side, mid - side], axis=1), {}


def stage_gain(x, fs, p):
    return x * 10 ** (p["gain_db"] / 20), {}


def true_peak_limit(x: np.ndarray, fs: int, ceiling_dbtp: float = -1.0, lookahead_ms: float = 1.5,
                    release_ms: float = 80.0, max_iter: int = 6, block: int = 32,
                    internal_margin_db: float = 0.1) -> tuple[np.ndarray, dict]:
    """Look-ahead true-peak limiter with measured verification.

    Gain curve g <= ceiling / interpolated_peak at every sample (sliding-min + causal box average),
    with exponential release. Output true peak is re-measured; if any over remains, the internal
    target is lowered and the pass repeated (bounded iterations). Zero added latency.
    """
    # internal_margin_db covers the documented <=0.14 dB under-read of 4x oversampled metering
    c = 10 ** ((ceiling_dbtp - internal_margin_db) / 20)
    n = x.shape[0]
    L = max(1, int(round(lookahead_ms / 1000 * fs)))
    env = peak_envelope(x, fs)
    margin = 1.0
    info = {"iterations": 0}
    info["internal_margin_db"] = internal_margin_db
    if env.max() <= c:
        info.update(max_gr_db=0.0, gr_time_s_over_1db=0.0, final_true_peak_dbtp=round(db(env.max()), 3),
                    untouched=True)
        return x.copy(), info
    rel_coef = np.exp(-block / (release_ms / 1000 * fs))
    for it in range(max_iter):
        g_raw = np.minimum(1.0, (c * margin) / np.maximum(env, 1e-12))
        m = minimum_filter1d(g_raw, size=2 * L - 1, mode="nearest")
        nb = int(np.ceil(n / block))
        pad = nb * block - n
        mb = np.pad(m, (0, pad), constant_values=1.0).reshape(nb, block).min(axis=1)
        rb = np.empty(nb)
        r = 1.0
        for i, v in enumerate(mb.tolist()):
            r = min(v, 1.0 - (1.0 - r) * rel_coef)
            if r > 1.0 - 1e-9:  # release reached unity (gain error < 1e-9, far below 1 LSB at 24-bit)
                r = 1.0 if v >= 1.0 else r
            rb[i] = r
        rr = np.repeat(rb, block)[:n]
        # Causal box average of the gain *deficit* (1-g). Padding uses the first value so g <= g_raw holds
        # at the file start; residue < 1e-12 from cumulative-sum rounding is snapped to 0 so regions that
        # need no limiting stay bit-identical (any real deficit in a window is >= ~1e-7).
        d = 1.0 - rr
        cs = np.concatenate([np.full(L, d[0]), d])
        C = np.concatenate([[0.0], np.cumsum(cs)])
        D = (C[L + 1: L + 1 + n] - C[1: 1 + n]) / L
        D[D < 1e-12] = 0.0
        g = 1.0 - D
        y = x * g[:, None]
        tp = float(peak_envelope(y, fs).max())
        info["iterations"] = it + 1
        if tp <= c * (1 + 1e-6):
            break
        margin *= (c / tp) * 0.9995
    gr_db = -20 * np.log10(np.maximum(g, 1e-12))
    info.update(max_gr_db=round(float(gr_db.max()), 3),
                gr_time_s_over_1db=round(float(np.count_nonzero(gr_db > 1.0) / fs), 3),
                limited_sample_ratio=round(float(np.count_nonzero(g < 0.9999) / n), 5),
                final_true_peak_dbtp=round(db(tp), 3), untouched=False)
    return y, info


def stage_limiter(x, fs, p):
    return true_peak_limit(x, fs, p["ceiling_dbtp"], p["lookahead_ms"], p["release_ms"])


FUNCS = {"dc_hpf": stage_dc_hpf, "eq": stage_eq, "compressor": stage_compressor, "stereo": stage_stereo,
         "gain": stage_gain, "limiter": stage_limiter}


@dataclass
class ChainResult:
    audio: np.ndarray
    stages: list[dict]


def _quick_meas(x, fs) -> dict:
    il = integrated_lufs(x, fs)
    return {"integrated_lufs": None if il is None else round(il, 2),
            "true_peak_dbtp": round(db(peak_envelope(x, fs).max()), 2)}


def run_chain(x: np.ndarray, fs: int, cfg: dict, *, measure_stages: bool = True) -> ChainResult:
    cfg = validate_chain(cfg)
    y = np.asarray(x, dtype=np.float64)
    n0 = y.shape[0]
    stages = []
    if measure_stages:
        stages.append({"stage": "input", **_quick_meas(y, fs)})
    for st in STAGES:
        p = cfg[st]
        if not p["enabled"]:
            stages.append({"stage": st, "bypassed": True})
            continue
        y, info = FUNCS[st](y, fs, p)
        rec = {"stage": st, "bypassed": False, "info": info}
        if measure_stages:
            rec.update(_quick_meas(y, fs))
        stages.append(rec)
    assert y.shape[0] == n0, "chain changed the sample count"
    return ChainResult(audio=y, stages=stages)
