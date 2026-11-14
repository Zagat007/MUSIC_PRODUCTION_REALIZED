"""Localized, reversible repair with objective regression checks.

Policy (spec §6):
* Only short, localized technical defects are repairable here (clicks, very short clipped runs,
  fades for abrupt edges). Everything else maps to REQUIRES_LISTENING_REVIEW or
  NEEDS_SOURCE_REGENERATION_OR_STEMS.
* Every edit is an explicit record (type, region, channel, method, params). Edits are applied to a
  copy; the original branch is never modified.
* An edit is accepted only if regression checks pass: samples outside the region are bit-identical,
  length unchanged, no new click is detected nearby, and the local energy change is bounded.
* AUTO is permitted only for HIGH-confidence clicks that pass the checks; anything else must be approved.
"""

from __future__ import annotations

import numpy as np
from scipy.linalg import solve_toeplitz

from srm.analysis.defects import detect_clicks

ARTIFACT_REGISTRY = {
    "click_pop": {"method": "lsar_interpolation", "max_len_ms": 3.0, "auto_allowed_if": "HIGH confidence + checks pass",
                  "status": "REPAIRABLE"},
    "hard_clipping": {"method": "lsar_declip_candidate", "max_len_ms": 2.0, "auto_allowed_if": "never",
                      "status": "REPAIRABLE_WITH_APPROVAL",
                      "note": "Interpolates a plausible waveform; lost original peaks cannot be truly restored."},
    "abrupt_start": {"method": "fade_in", "auto_allowed_if": "never", "status": "REPAIRABLE_WITH_APPROVAL"},
    "abrupt_end_cut": {"method": "fade_out", "auto_allowed_if": "never", "status": "REPAIRABLE_WITH_APPROVAL",
                       "note": "A fade hides a truncation; regenerating the source may be better."},
    "dc_offset": {"method": "chain.dc_hpf", "auto_allowed_if": "via SAFE_MASTER proposal", "status": "HANDLED_BY_CHAIN"},
    "dropout": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS",
                "note": "Gap filling would invent music; listen first, regenerate or use stems."},
    "internal_digital_silence": {"method": None, "status": "REQUIRES_LISTENING_REVIEW"},
    "possible_harshness": {"method": "chain.eq (bounded, approval)", "status": "REQUIRES_LISTENING_REVIEW"},
    "excess_sub_bass": {"method": "chain.eq/dc_hpf (approval)", "status": "REQUIRES_LISTENING_REVIEW"},
    "heavy_brickwall_limiting": {"method": None, "status": "SOURCE_LIMITATION",
                                 "note": "Cannot un-limit audio; avoid adding more limiting."},
    "lossy_bandwidth_limit": {"method": None, "status": "SOURCE_LIMITATION",
                              "note": "Never fabricate high-frequency content."},
    "phase_mono_cancellation": {"method": None, "status": "REQUIRES_LISTENING_REVIEW"},
    "local_phase_cancellation": {"method": None, "status": "REQUIRES_LISTENING_REVIEW"},
    "silent_channel": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS"},
    "digital_silence": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS"},
    "metallic_or_watery_vocal": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS"},
    "phasey_reverb_smeared_cymbals": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS"},
    "pre_echo_or_smearing": {"method": None, "status": "SOURCE_LIMITATION"},
    "synthetic_timbre_or_lyric_garble": {"method": None, "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS"},
}


class RepairError(RuntimeError):
    pass


def _ar_coeffs(ctx: np.ndarray, p: int) -> np.ndarray:
    ctx = ctx - ctx.mean()
    r = np.correlate(ctx, ctx, mode="full")[len(ctx) - 1: len(ctx) + p]
    if r[0] <= 1e-15:
        return np.zeros(p)
    r = r.copy()
    r[0] *= 1.0 + 1e-6  # white-noise regularisation
    a = solve_toeplitz(r[:p], r[1: p + 1])
    return a  # x[n] ~ sum_k a[k] x[n-1-k]


def lsar_interpolate(sig: np.ndarray, s: int, e: int, p: int = 32, ctx: int = 1024, fs: int | None = None,
                     trend_hz: float = 250.0) -> np.ndarray:
    """Interpolate sig[s:e]: low-frequency trend (zero-phase low-pass over a linearly bridged gap) plus
    least-squares AR interpolation (Janssen/Vaseghi) of the residual. Returns a repaired copy.

    The trend split matters: a short AR model cannot carry long-period bass across the gap.
    """
    if fs:
        from scipy.signal import butter, sosfiltfilt

        a0 = max(0, s - 4 * ctx)
        b0 = min(len(sig), e + 4 * ctx)
        local = sig[a0:b0].astype(np.float64).copy()
        ls, le = s - a0, e - a0
        lv = local[ls - 1] if ls > 0 else local[le]
        rv = local[le] if le < len(local) else lv
        local[ls:le] = np.linspace(lv, rv, le - ls + 2)[1:-1]
        sos = butter(4, trend_hz, btype="lowpass", fs=fs, output="sos")
        trend_local = sosfiltfilt(sos, local)
        resid = sig.astype(np.float64).copy()
        resid[a0:b0] = sig[a0:b0] - trend_local
        rep = _lsar(resid, s, e, p, ctx)
        out = sig.copy()
        out[s:e] = rep[s:e] + trend_local[ls:le]
        return out
    return _lsar(sig, s, e, p, ctx)


def _lsar(sig: np.ndarray, s: int, e: int, p: int, ctx: int) -> np.ndarray:
    n = len(sig)
    if e - s <= 0:
        return sig.copy()
    a0 = max(0, s - ctx)
    b0 = min(n, e + ctx)
    context = np.concatenate([sig[a0:s], sig[e:b0]])
    if len(context) < 4 * p:
        raise RepairError("insufficient context for AR interpolation")
    a = _ar_coeffs(context, p)
    lo = max(0, s - p)
    hi = min(n, e + p)
    seg = sig[lo:hi].astype(np.float64).copy()
    m = len(seg)
    # prediction error e[k] = seg[k] - sum a_j seg[k-1-j], k = p..m-1
    rows = m - p
    A = np.zeros((rows, m))
    for k in range(rows):
        A[k, k + p] = 1.0
        A[k, k: k + p] = -a[::-1]
    unknown = np.zeros(m, bool)
    unknown[s - lo: e - lo] = True
    Au, Ak = A[:, unknown], A[:, ~unknown]
    xk = seg[~unknown]
    xu, *_ = np.linalg.lstsq(Au, -Ak @ xk, rcond=None)
    out = sig.copy()
    out[s:e] = xu
    return out


def _fade(sig: np.ndarray, s: int, e: int, kind: str) -> np.ndarray:
    out = sig.copy()
    L = e - s
    if L <= 0:
        return out
    ramp = np.sin(np.linspace(0, np.pi / 2, L)) ** 2  # raised-cosine
    out[s:e] = out[s:e] * (ramp if kind == "in" else ramp[::-1])
    if kind == "out":
        out[e:] = 0.0
    return out


def apply_edit(x: np.ndarray, fs: int, edit: dict) -> np.ndarray:
    """Apply one edit to a copy of x (n, ch). Region grows by 0.25 ms guard on each side for interpolation."""
    y = x.copy()
    s = int(round(edit["start_s"] * fs))
    e = max(s + 1, int(round(edit["end_s"] * fs)))
    chans = [edit["channel"]] if edit.get("channel") is not None else list(range(x.shape[1]))
    method = edit["method"]
    if method == "lsar_declip_candidate":
        # Interpolate each flat-top run inside the candidate region separately (runs re-detected).
        max_run = int(edit.get("max_len_ms", 2.0) / 1000 * fs)
        runs_done = 0
        for c in chans:
            sig = x[:, c]
            pk = np.abs(sig).max()
            seg = sig[s:e]
            flat = np.abs(seg) >= pk * 0.99995
            d = np.diff(np.concatenate([[0], flat.astype(np.int8), [0]]))
            for rs, re_ in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
                if re_ - rs < 3:
                    continue
                if re_ - rs > max_run:
                    raise RepairError("clipped run longer than the declip limit")
                a_, b_ = max(0, s + rs - 1), min(x.shape[0], s + re_ + 1)
                y[:, c] = lsar_interpolate(y[:, c], a_, b_, fs=fs)
                runs_done += 1
        edit["applied_region"] = [max(0, s - 1) / fs, min(x.shape[0], e + 1) / fs]
        edit["runs_interpolated"] = runs_done
        return y
    if method in ("lsar_interpolation", "lsar_declip_candidate"):
        g = max(1, int(0.00025 * fs))
        s2, e2 = max(0, s - g), min(x.shape[0], e + g)
        if (e2 - s2) / fs * 1000 > edit.get("max_len_ms", 3.0) + 0.6:
            raise RepairError("region too long for interpolation")
        for c in chans:
            y[:, c] = lsar_interpolate(x[:, c], s2, e2, fs=fs)
        edit["applied_region"] = [s2 / fs, e2 / fs]
    elif method == "fade_in":
        L = int(edit.get("params", {}).get("fade_ms", 10) / 1000 * fs)
        for c in chans:
            y[:, c] = _fade(x[:, c], 0, min(L, x.shape[0]), "in")
        edit["applied_region"] = [0.0, L / fs]
    elif method == "fade_out":
        L = int(edit.get("params", {}).get("fade_ms", 30) / 1000 * fs)
        n = x.shape[0]
        for c in chans:
            y[:, c] = _fade(x[:, c], max(0, n - L), n, "out")
        edit["applied_region"] = [max(0, n - L) / fs, n / fs]
    else:
        raise RepairError(f"method {method!r} not supported for automatic application")
    return y


def regression_check(x: np.ndarray, y: np.ndarray, fs: int, edit: dict) -> dict:
    """Objective checks for one applied edit."""
    a, b = edit["applied_region"]
    s, e = int(round(a * fs)), int(round(b * fs))
    res: dict = {}
    res["length_unchanged"] = x.shape == y.shape
    outside = np.ones(x.shape[0], bool)
    outside[s:e] = False
    res["outside_region_bit_identical"] = bool(np.array_equal(x[outside], y[outside]))
    res["finite"] = bool(np.all(np.isfinite(y)))
    chans = [edit["channel"]] if edit.get("channel") is not None else list(range(x.shape[1]))
    # Other channels must be untouched everywhere
    others = [c for c in range(x.shape[1]) if c not in chans]
    res["other_channels_bit_identical"] = bool(np.array_equal(x[:, others], y[:, others])) if others else True
    x, y = x[:, chans], y[:, chans]  # remaining checks apply to the edited channel(s)
    if edit["method"].startswith("lsar"):
        # (a) no click left in/near the region: run the detector on real surrounding audio (1 s each side)
        lo, hi = max(0, s - fs), min(x.shape[0], e + fs)
        near = [c for c in detect_clicks(y[lo:hi], fs)
                if (s - lo) / fs - 0.005 <= c["start_s"] <= (e - lo) / fs + 0.005]
        res["no_click_after_repair"] = len(near) == 0
        # (b) no new high-frequency energy: 2nd-difference RMS in region vs +-10 ms context
        w = max(4, int(0.01 * fs))
        def d2(a):
            return np.diff(a, n=2, axis=0)
        ctx = np.concatenate([x[max(0, s - w): s], x[e: e + w]])
        hf_ctx = float(np.sqrt(np.mean(d2(ctx) ** 2)) + 1e-12) if len(ctx) > 4 else 1e-12
        reg = y[max(0, s - 1): e + 1]
        hf_rep = float(np.sqrt(np.mean(d2(reg) ** 2)) + 1e-12) if len(reg) > 2 else 0.0
        res["hf_ratio_vs_context"] = round(hf_rep / hf_ctx, 2)
        res["no_new_hf_energy"] = bool(hf_rep <= 3.0 * hf_ctx)
        # (c) amplitude stays within the local envelope (except declip, which may exceed the clip level)
        peak_ctx = float(np.abs(ctx).max()) if len(ctx) else 0.0
        peak_rep = float(np.abs(y[s:e]).max()) if e > s else 0.0
        res["repaired_peak_vs_context_db"] = round(20 * np.log10((peak_rep + 1e-12) / (peak_ctx + 1e-12)), 2)
        if edit["method"] == "lsar_declip_candidate":
            res["exceeds_clip_level_consistently"] = bool(peak_rep >= float(np.abs(x[s:e]).max()) * 0.999)
            res["peak_plausible"] = bool(res["repaired_peak_vs_context_db"] < 6.0)
        else:
            res["peak_plausible"] = bool(res["repaired_peak_vs_context_db"] < 1.0)
    res["passed"] = all(v for k, v in res.items() if isinstance(v, bool))
    return res


def plan_from_candidates(candidates: list[dict], listening: list[dict]) -> dict:
    edits, other = [], []
    n = 0
    for c in candidates:
        reg = ARTIFACT_REGISTRY.get(c["type"], {"status": "REQUIRES_LISTENING_REVIEW", "method": None})
        meth = reg.get("method")
        if meth in ("lsar_interpolation", "lsar_declip_candidate", "fade_in", "fade_out"):
            if meth == "lsar_declip_candidate" and (c["evidence"].get("longest_run_samples", 999) > 96
                                                     or c["end_s"] - c["start_s"] > 0.25):
                other.append({**_brief(c), "status": "NEEDS_SOURCE_REGENERATION_OR_STEMS",
                              "reason": "clipping too long or widespread for safe interpolation; lost peaks "
                                        "cannot be restored - regenerate the source or use stems"})
                continue
            n += 1
            auto = bool(c.get("auto_repair_eligible")) and meth == "lsar_interpolation" and c["confidence"] == "HIGH"
            edits.append({"edit_id": n, "type": c["type"], "start_s": c["start_s"], "end_s": c["end_s"],
                          "channel": c.get("channel"), "method": meth, "max_len_ms": reg.get("max_len_ms", 3.0),
                          "params": {"fade_ms": 10} if meth == "fade_in" else ({"fade_ms": 30} if meth == "fade_out" else {}),
                          "confidence": c["confidence"], "severity": c["severity"], "evidence": c["evidence"],
                          "auto_eligible": auto, "decision": "PENDING", "regression": None,
                          "note": reg.get("note")})
        else:
            other.append({**_brief(c), "status": reg["status"], "reason": reg.get("note") or "not automatically repairable"})
    for f in listening:
        other.append({"type": f["type"], "status": ARTIFACT_REGISTRY.get(f["type"], {}).get("status", f["status"]),
                      "reason": f["note"]})
    return {"edits": edits, "not_repairable": other,
            "policy": "AUTO only for HIGH-confidence clicks that pass regression checks; all else needs approval."}


def _brief(c):
    return {k: c.get(k) for k in ("type", "start_s", "end_s", "channel", "severity", "confidence")}


def apply_repair_plan(x: np.ndarray, fs: int, plan: dict) -> tuple[np.ndarray, list[dict]]:
    """Apply APPROVED (and AUTO-accepted) edits sequentially; each must pass checks again."""
    y = np.asarray(x, dtype=np.float64).copy()
    log = []
    for ed in plan.get("edits", []):
        if ed.get("decision") not in ("APPROVED", "AUTO_ACCEPTED"):
            continue
        ed = dict(ed)
        z = apply_edit(y, fs, ed)
        chk = regression_check(y, z, fs, ed)
        if chk["passed"]:
            y = z
            log.append({"edit_id": ed["edit_id"], "type": ed["type"], "region": ed["applied_region"],
                        "channel": ed.get("channel"), "method": ed["method"], "applied": True, "checks": chk})
        else:
            log.append({"edit_id": ed["edit_id"], "type": ed["type"], "applied": False,
                        "reason": "REJECTED_BY_REGRESSION", "checks": chk})
    return y, log
