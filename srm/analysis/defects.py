"""Candidate defect detection. Every finding is a *candidate* with timestamps, evidence,
confidence and an independent severity. Absence of a finding does not prove absence of
a defect; perceptual AI artifacts are explicitly reported as requiring listening.
"""

from __future__ import annotations

import numpy as np

from srm.analysis.truepeak import db

SEVERITIES = ("NONE", "MINOR", "MODERATE", "SEVERE", "UNCERTAIN")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")

LISTENING_ONLY = [
    ("metallic_or_watery_vocal", "Metallic/watery vocal timbre (codec or generative artifact)"),
    ("phasey_reverb_smeared_cymbals", "Phasey reverb, smeared cymbals or mangled transients"),
    ("pre_echo_or_smearing", "Pre-echo / temporal smearing around transients (lossy codec)"),
    ("synthetic_timbre_or_lyric_garble", "Synthetic timbre, garbled syllables, generative glitches"),
]


def _cand(kind, start, end, conf, sev, evidence, action, channel=None, auto=False, **extra):
    d = {
        "type": kind,
        "start_s": round(float(start), 4),
        "end_s": round(float(end), 4),
        "channel": channel,
        "confidence": conf,
        "severity": sev,
        "evidence": evidence,
        "recommended_action": action,
        "auto_repair_eligible": bool(auto),
    }
    d.update(extra)
    return d


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.where(d == 1)[0].tolist(), np.where(d == -1)[0].tolist()))


def _merge(regions: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(regions):
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def detect_clipping(x: np.ndarray, fs: int, max_items: int = 50) -> list[dict]:
    res = []
    for ch in range(x.shape[1]):
        a = np.abs(x[:, ch])
        pk = a.max()
        if pk < 10 ** (-6.0 / 20):
            continue  # flat tops below -6 dBFS are not treated as clipping evidence
        below_fs = pk < 10 ** (-0.3 / 20)  # clipped-then-attenuated material (plateau below full scale)
        sig = x[:, ch]
        thr = pk * 0.99995
        runs = [(s, e) for s, e in _runs(sig >= thr) + _runs(sig <= -thr) if e - s >= 3]
        if not runs:
            continue
        merged = _merge(runs, int(0.05 * fs))
        total = sum(e - s for s, e in runs)
        for s, e in merged[:max_items]:
            n_in = sum(min(e2, e) - max(s2, s) for s2, e2 in runs if s2 < e and e2 > s)
            longest = max(e2 - s2 for s2, e2 in runs if s2 < e and e2 > s)
            sev = "SEVERE" if longest >= 12 else ("MODERATE" if longest >= 6 else "MINOR")
            res.append(_cand(
                "hard_clipping", s / fs, e / fs, "HIGH" if (longest >= 4 and not below_fs) else "MEDIUM", sev,
                {"flat_top_samples": int(n_in), "longest_run_samples": int(longest),
                 "level_dbfs": round(db(pk), 3), "total_flat_samples_channel": int(total),
                 "plateau_below_full_scale": bool(below_fs)},
                "Review; optional cautious declip candidate. Lost peaks cannot be truly restored.",
                channel=ch, auto=False))
    return res


def detect_clicks(x: np.ndarray, fs: int, k: float = 14.0, max_items: int = 100) -> list[dict]:
    """Impulsive outliers in the 2nd difference that are NOT followed by a sustained onset."""
    res = []
    blk = 2048
    n = x.shape[0]
    if n < blk * 4:
        return res
    w5 = max(1, int(0.005 * fs))
    for ch in range(x.shape[1]):
        sig = x[:, ch].astype(np.float64)
        r = np.zeros(n)
        r[1:-1] = sig[2:] - 2 * sig[1:-1] + sig[:-2]
        ar = np.abs(r)
        nb = n // blk
        med = np.median(ar[: nb * blk].reshape(nb, blk), axis=1) + 1e-9
        # Local scale = rolling max of neighbouring block medians (protects loud transients)
        med_s = np.maximum.reduce([np.roll(med, i) for i in (-2, -1, 0, 1, 2)])
        scale = np.repeat(med_s, blk)
        scale = np.concatenate([scale, np.full(n - scale.size, med_s[-1])])
        idx = np.where(ar > k * scale)[0]
        if idx.size == 0:
            continue
        groups = _merge([(i, i + 1) for i in idx.tolist()], int(0.001 * fs))
        for s, e in groups:
            dur = (e - s) / fs
            if dur > 0.003:
                continue  # longer events are transients/bursts, not clicks
            pre = sig[max(0, s - w5 - w5): max(1, s - w5)]
            post = sig[e + w5: e + 2 * w5]
            if pre.size < 4 or post.size < 4:
                continue
            pre_rms = np.sqrt(np.mean(pre ** 2)) + 1e-9
            post_rms = np.sqrt(np.mean(post ** 2)) + 1e-9
            env_ratio = post_rms / pre_rms
            if env_ratio > 2.0:
                continue  # sustained onset afterwards: likely a musical transient
            # Isolation: HF residual right after the event must fall back to the pre-event level.
            g = max(1, int(0.0005 * fs))
            hf_pre = np.sqrt(np.mean(r[max(0, s - 2 * w5): max(1, s - g)] ** 2)) + 1e-12
            hf_post = np.sqrt(np.mean(r[e + g: e + 2 * w5] ** 2)) + 1e-12
            if hf_post > 2.5 * hf_pre:
                continue  # HF stays high (noise burst / hat / snare onset)
            isolation = float(ar[s:e].max() / max(hf_pre, hf_post))
            if isolation < 10.0:
                continue
            strength = float(ar[s:e].max() / scale[s])
            conf = "HIGH" if strength > 3 * k and env_ratio < 1.3 else ("MEDIUM" if strength > 1.8 * k else "LOW")
            sev = "MODERATE" if strength > 4 * k else "MINOR"
            res.append(_cand(
                "click_pop", s / fs, e / fs, conf, sev,
                {"impulse_strength_x_local": round(strength, 1), "isolation_x_hf_context": round(isolation, 1),
                 "post_pre_envelope_ratio": round(float(env_ratio), 2),
                 "duration_ms": round(dur * 1000, 2)},
                "Short-region interpolation candidate; A-B before approval.",
                channel=ch, auto=(conf == "HIGH")))
    res.sort(key=lambda c: -c["evidence"]["impulse_strength_x_local"])
    return res[:max_items]


def detect_dropouts(x: np.ndarray, fs: int, max_items: int = 30) -> list[dict]:
    res = []
    mono = np.abs(x).max(axis=1)
    w = max(1, int(0.01 * fs))
    nw = len(mono) // w
    if nw < 50:
        return res
    rms = np.sqrt((x[: nw * w].astype(np.float64) ** 2).mean(axis=1).reshape(nw, w).mean(axis=1))
    lvl = 20 * np.log10(rms + 1e-12)
    active = np.where(lvl > -50)[0]
    if active.size < 10:
        return res
    first, last = active[0], active[-1]
    ctx = 20  # 200 ms
    for i in range(first + ctx, last - ctx):
        if lvl[i] < -70:
            around = np.concatenate([lvl[i - ctx: i - 2], lvl[i + 3: i + ctx]])
            if np.median(around) > -30:
                res.append((i, i + 1))
    merged = _merge(res, 3)
    out = []
    for s, e in merged:
        if (e - s) * w / fs > 0.3:
            continue  # longer gaps are likely intentional breaks
        out.append(_cand(
            "dropout", s * w / fs, e * w / fs, "MEDIUM", "MODERATE",
            {"gap_ms": round((e - s) * w / fs * 1000, 1), "context_level_db": round(float(np.median(lvl[max(0, s - ctx): s])), 1)},
            "Listen: may be an intentional stop. Repair only with approval.", auto=False))
    return out[:max_items]


def detect_from_measurements(meas: dict, fs: int) -> list[dict]:
    """Detectors driven by already-computed measurements (no extra DSP)."""
    out = []
    sil = meas.get("silence", {})
    dyn = meas.get("dynamics", {})
    spec = meas.get("spectral", {})
    st = meas.get("stereo", {})
    dur = meas.get("duration_s", 0) or 0
    if sil.get("entirely_silent"):
        out.append(_cand("digital_silence", 0, dur, "HIGH", "SEVERE", {}, "File is silent: check source/export."))
        return out
    if sil.get("abrupt_start"):
        out.append(_cand("abrupt_start", 0, 0.01, "MEDIUM", "MINOR",
                         {"first_10ms_rms_dbfs": sil.get("first_10ms_rms_dbfs")},
                         "Listen to the start; optional short fade-in (not automatic)."))
    if sil.get("abrupt_end"):
        out.append(_cand("abrupt_end_cut", max(0, dur - 0.01), dur, "MEDIUM", "MODERATE",
                         {"last_10ms_rms_dbfs": sil.get("last_10ms_rms_dbfs")},
                         "Possible truncated ending; optional fade-out, or regenerate the source."))
    for ch in sil.get("silent_channels", []):
        out.append(_cand("silent_channel", 0, dur, "HIGH", "SEVERE", {"channel": ch},
                         "One channel is silent: likely export fault.", channel=ch))
    for r in sil.get("internal_digital_silence_runs", []):
        out.append(_cand("internal_digital_silence", r["start_s"], r["end_s"], "MEDIUM", "UNCERTAIN", r,
                         "Listen: could be an intentional break or a dropout."))
    dc = dyn.get("dc_offset_dbfs_max")
    if dc is not None and dc > -50:
        out.append(_cand("dc_offset", 0, dur, "HIGH", "MINOR" if dc < -40 else "MODERATE", {"dc_dbfs": dc},
                         "DC removal (high-pass ~5 Hz) is safe and is offered in SAFE_MASTER."))
    crest = dyn.get("median_short_window_crest_db")
    dens = dyn.get("near_max_peak_window_ratio")
    if crest is not None and dens is not None and crest < 6.0 and dens > 0.25:
        out.append(_cand("heavy_brickwall_limiting", 0, dur, "MEDIUM", "UNCERTAIN",
                         {"median_short_window_crest_db": crest, "near_max_peak_window_ratio": dens,
                          "plr_db": dyn.get("plr_db")},
                         "Source is already dense; avoid further limiting. Consider lowering level instead."))
    bands = spec.get("band_energy_db_rel", {}) or {}
    sub = bands.get("sub_20_40")
    if sub is not None and sub > -6:
        out.append(_cand("excess_sub_bass", 0, dur, "LOW", "UNCERTAIN", {"sub_20_40_rel_db": sub},
                         "Listen on full-range monitors; may be intentional (e.g. DnB/trap)."))
    hm = bands.get("highmid_2k_6k")
    if hm is not None and hm > -6:
        out.append(_cand("possible_harshness", 0, dur, "LOW", "UNCERTAIN", {"highmid_2k_6k_rel_db": hm},
                         "Listen for harshness; gentle, bounded high-mid cut only if confirmed."))
    lp = spec.get("possible_encoder_lowpass_hz")
    if lp is not None and lp < 19000:
        out.append(_cand("lossy_bandwidth_limit", 0, dur, "MEDIUM", "MINOR", {"effective_bandwidth_hz": lp},
                         "Source limitation (lossy encoder lowpass). Cannot be restored; do not fake 'air'."))
    corr = st.get("correlation")
    if corr is not None and corr < 0.0:
        out.append(_cand("phase_mono_cancellation", 0, dur, "HIGH" if corr < -0.3 else "MEDIUM", "SEVERE",
                         {"correlation": corr, "mono_loss_lu": st.get("mono_downmix_loudness_loss_lu")},
                         "Check polarity/stereo processing; mono playback will lose content."))
    for w in st.get("worst_correlation_windows", []):
        if w["correlation"] < -0.3:
            out.append(_cand("local_phase_cancellation", w["start_s"], w["end_s"], "MEDIUM", "UNCERTAIN", w,
                             "Listen in mono; may be an intentional wide effect."))
    return out


def listening_flags() -> list[dict]:
    return [
        {"type": k, "status": "REQUIRES_LISTENING_REVIEW", "description": d,
         "note": "Cannot be reliably detected or repaired by simple numerical tests; needs ears or stems."}
        for k, d in LISTENING_ONLY
    ]


def detect_all(x: np.ndarray, fs: int, meas: dict) -> dict:
    cands = []
    cands += detect_clipping(x, fs)
    cands += detect_clicks(x, fs)
    cands += detect_dropouts(x, fs)
    cands += detect_from_measurements(meas, fs)
    cands.sort(key=lambda c: (c["start_s"], c["type"]))
    return {
        "candidates": cands,
        "listening_required": listening_flags(),
        "note": "Detections are candidates. Absence of a detection does not prove absence of a defect.",
    }
