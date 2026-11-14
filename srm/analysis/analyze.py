"""Full-track analysis orchestration (pure function of decoded audio)."""

from __future__ import annotations

import numpy as np

from srm.analysis import defects, metrics, tempo
from srm.analysis.genre import estimate_style
from srm.analysis.loudness import measure_loudness
from srm.analysis.truepeak import measure_peaks

ANALYSIS_VERSION = "analysis-1.0"


def analyze_audio(x: np.ndarray, fs: int, *, prompt_text: str | None = None, with_defects: bool = True,
                  with_tempo: bool = True, timelines: bool = True) -> dict:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    dur = x.shape[0] / fs
    loud = measure_loudness(x, fs)
    peaks = measure_peaks(x, fs)
    integ = loud.integrated_lufs
    meas: dict = {
        "analysis_version": ANALYSIS_VERSION,
        "sample_rate": fs,
        "channels": int(x.shape[1]),
        "duration_s": round(dur, 4),
        "samples": int(x.shape[0]),
        "loudness": loud.summary(),
        "peaks": peaks,
        "dynamics": metrics.level_dynamics(x, fs, integ, peaks["true_peak_dbtp"]),
        "stereo": metrics.stereo_metrics(x, fs),
        "spectral": metrics.spectral_metrics(x, fs),
        "silence": metrics.silence_metrics(x, fs),
    }
    if with_tempo:
        meas["tempo"] = tempo.estimate_tempo(x, fs)
    meas["style"] = estimate_style(meas, prompt_text)
    if with_defects:
        meas["defects"] = defects.detect_all(x, fs, meas)
    if timelines:
        def ser(a):
            return [None if not np.isfinite(v) or v < -70 else round(float(v), 2) for v in a]
        meas["timelines"] = {
            "hop_s": loud.hop_s,
            "momentary_lufs": ser(loud.momentary),
            "short_term_lufs": ser(loud.short_term),
            "waveform": metrics.waveform_overview(x),
        }
    return meas


def technical_status(meas: dict, *, source_is_lossy: bool | None = None, decode_warnings: list | None = None) -> dict:
    """Separate technical QA verdict. Never an aesthetic approval."""
    fails, warns = [], []
    if meas.get("silence", {}).get("entirely_silent"):
        fails.append("entirely silent")
    if meas.get("silence", {}).get("silent_channels"):
        fails.append("silent channel(s)")
    if decode_warnings:
        warns.append(f"decoder reported {len(decode_warnings)} warning(s)")
    for c in meas.get("defects", {}).get("candidates", []):
        if c["severity"] == "SEVERE" and c["confidence"] in ("HIGH", "MEDIUM"):
            warns.append(f"{c['type']} at {c['start_s']}s ({c['severity']}/{c['confidence']})")
    if source_is_lossy:
        warns.append("lossy source: transcoded, not restored")
    status = "FAILED" if fails else "NEEDS_LISTENING_REVIEW"
    # TECHNICAL_PASS is only a technical statement; listening review is still a separate gate.
    if not fails and not warns:
        status = "TECHNICAL_PASS"
    return {
        "technical_status": status,
        "failures": fails,
        "warnings": warns,
        "listening_signoff": "NOT_DONE",
        "note": "Technical QA only. Metrics cannot establish that a master sounds professional.",
    }
