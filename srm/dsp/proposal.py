"""Explainable mastering proposals: problem -> evidence -> remedy -> risk -> settings.

A proposal is *data*; nothing is applied until a version is rendered, and nothing is
release-approved until a human approves it.
"""

from __future__ import annotations

import copy

from srm.dsp.chain import default_chain, validate_chain
from srm.dsp.presets import BAND_REFERENCE, PRESETS

MODES = ("ANALYZE_ONLY", "CONVERT_ONLY", "SAFE_MASTER", "RESTORE_AND_MASTER")


def pick_preset(meas: dict, override: str | None) -> tuple[str, str]:
    if override:
        if override not in PRESETS:
            raise ValueError(f"unknown preset {override}")
        return override, "user override"
    st = meas.get("style", {})
    ranked = st.get("ranked") or []
    if not ranked or st.get("mixed_or_uncertain"):
        return "generic", "style uncertain/mixed -> generic (heuristic guesses are not used to pick aggressive presets)"
    top = ranked[0]["style"]
    if st.get("confidence", "LOW") != "HIGH":
        # A LOW-confidence guess must not move loudness: on a real album it raised one track +4 dB
        # into a -11..-8 LUFS window while its neighbours stayed at -14. The user's style choice decides.
        return "generic", (f"heuristic hint '{top}' is LOW confidence -> generic; set the style "
                           f"(track-meta --style {top}) to use the '{top}' preset")
    return (top if top in PRESETS else "generic"), f"style '{top}' (HIGH confidence)"


def propose(meas: dict, *, mode: str = "SAFE_MASTER", preset: str | None = None, ceiling_dbtp: float = -1.0,
            target_lufs: float | None = None, loudness_policy: str = "preset_window",
            album_offset_db: float | None = None) -> dict:
    """Build an explainable chain proposal from source measurements.

    loudness_policy: 'preserve' (no level change except peak safety), 'preset_window' (move into the
    preset's suggested window only if outside it), 'target' (explicit target_lufs), 'album_offset'
    (apply a precomputed album-relative gain).
    """
    if mode not in ("SAFE_MASTER", "RESTORE_AND_MASTER"):
        raise ValueError("proposals are for SAFE_MASTER / RESTORE_AND_MASTER")
    pname, why = pick_preset(meas, preset)
    P = PRESETS[pname]
    chain = default_chain()
    actions: list[dict] = []
    il = (meas.get("loudness") or {}).get("integrated_lufs")
    tp = (meas.get("peaks") or {}).get("true_peak_dbtp")
    dyn = meas.get("dynamics") or {}
    bands = (meas.get("spectral") or {}).get("band_energy_db_rel") or {}
    dense = any(c["type"] == "heavy_brickwall_limiting" for c in (meas.get("defects") or {}).get("candidates", []))

    # DC / subsonic
    dc = dyn.get("dc_offset_dbfs_max")
    if dc is not None and dc > -50:
        chain["dc_hpf"] = {"enabled": True, "freq_hz": 5.0}
        actions.append({"stage": "dc_hpf", "problem": "DC offset", "evidence": {"dc_offset_dbfs": dc},
                        "remedy": "2nd-order high-pass at 5 Hz", "risk": "negligible (below audible band)",
                        "settings": chain["dc_hpf"], "enabled": True})

    # Tonal: bounded, only for large deviations; disabled suggestions otherwise
    bands_out = []
    hm = bands.get("highmid_2k_6k")
    if hm is not None and hm > BAND_REFERENCE["highmid_2k_6k"][1] + 2:
        b = {"type": "peak", "freq_hz": 3500.0, "gain_db": -1.5, "q": 0.8}
        enabled = mode == "SAFE_MASTER" and hm > BAND_REFERENCE["highmid_2k_6k"][1] + 4
        actions.append({"stage": "eq", "problem": "possible high-mid harshness",
                        "evidence": {"highmid_2k_6k_rel_db": hm, "reference_max": BAND_REFERENCE["highmid_2k_6k"][1]},
                        "remedy": "-1.5 dB broad bell at 3.5 kHz", "risk": "may dull vocal presence; confirm by A-B",
                        "settings": b, "enabled": enabled})
        if enabled:
            bands_out.append(b)
    lp = (meas.get("spectral") or {}).get("possible_encoder_lowpass_hz")
    if lp:
        actions.append({"stage": "eq", "problem": "lossy encoder bandwidth limit",
                        "evidence": {"effective_bandwidth_hz": lp},
                        "remedy": "NONE - boosting 'air' above the cutoff would only add noise/artifacts",
                        "risk": "n/a", "settings": None, "enabled": False})
    if bands_out:
        chain["eq"] = {"enabled": True, "bands": bands_out}

    # Dynamics
    lra = (meas.get("loudness") or {}).get("lra_lu")
    if P["allow_compression"] and lra is not None and lra > 14 and not dense:
        chain["compressor"] = {"enabled": False, "threshold_db": -20.0, "ratio": 1.4, "attack_ms": 30.0,
                               "release_ms": 300.0, "knee_db": 8.0}
        actions.append({"stage": "compressor", "problem": "very wide loudness range for this style",
                        "evidence": {"lra_lu": lra}, "remedy": "gentle 1.4:1 soft-knee compression (OFF by default)",
                        "risk": "reduces intended dynamics; enable only after listening",
                        "settings": chain["compressor"], "enabled": False})
    elif dense:
        actions.append({"stage": "compressor", "problem": "source already densely limited",
                        "evidence": {"median_short_window_crest_db": dyn.get("median_short_window_crest_db")},
                        "remedy": "no compression; avoid additional limiting", "risk": "n/a",
                        "settings": None, "enabled": False})

    # Level
    gain = 0.0
    lo, hi = P["loudness_range_lufs"]
    max_gr = P["max_limiter_gr_db"]
    note = ""
    if il is None:
        note = "integrated loudness undefined (silence?) - no gain"
    elif loudness_policy == "preserve":
        note = "level preserved"
    elif loudness_policy == "target":
        if target_lufs is None:
            raise ValueError("target_lufs required for loudness_policy='target'")
        gain = target_lufs - il
        note = f"explicit target {target_lufs} LUFS"
    elif loudness_policy == "album_offset":
        gain = float(album_offset_db or 0.0)
        note = "album-relative gain (from album cohesion plan)"
    else:
        if il < lo:
            gain = lo - il
            note = f"below preset window [{lo}, {hi}] -> raise to {lo}"
        elif il > hi:
            gain = hi - il
            note = f"above preset window -> lower to {hi} (reduces limiting need)"
        else:
            note = f"inside preset window [{lo}, {hi}] -> unchanged"
    if il is not None and tp is not None and gain > 0:
        needed_gr = (tp + gain) - ceiling_dbtp
        if needed_gr > max_gr:
            capped = max(0.0, ceiling_dbtp + max_gr - tp)
            note += (f"; capped from +{gain:.2f} to +{capped:.2f} dB so peak reduction stays <= {max_gr} dB "
                     "(protects transients)")
            gain = capped
    gain = round(gain, 2)
    if abs(gain) >= 0.05:
        chain["gain"] = {"enabled": True, "gain_db": gain}
    actions.append({"stage": "gain", "problem": "level planning", "evidence": {"integrated_lufs": il,
                    "true_peak_dbtp": tp, "preset_window": [lo, hi], "policy": loudness_policy},
                    "remedy": f"static gain {gain:+.2f} dB", "risk": "louder output needs more peak control",
                    "settings": chain["gain"], "enabled": chain["gain"]["enabled"], "note": note})
    chain["limiter"] = {"enabled": True, "ceiling_dbtp": ceiling_dbtp, "lookahead_ms": 1.5, "release_ms": 80.0}
    est_gr = max(0.0, (tp or -99) + gain - ceiling_dbtp)
    actions.append({"stage": "limiter", "problem": "true-peak safety", "evidence": {"est_peak_reduction_db": round(est_gr, 2)},
                    "remedy": f"true-peak limiter, ceiling {ceiling_dbtp} dBTP (verified after render)",
                    "risk": "audible only when reduction is large; measured in report", "settings": chain["limiter"],
                    "enabled": True})
    validate_chain(chain)
    return {
        "mode": mode, "preset": pname, "preset_reason": why, "preset_info": copy.deepcopy(P),
        "chain": chain, "actions": actions,
        "listening_required": True,
        "disclaimer": ("Proposal derived from measurements and a heuristic style guess. It must be auditioned "
                       "(loudness-matched A-B) and explicitly approved; metrics cannot certify a professional master."),
    }
