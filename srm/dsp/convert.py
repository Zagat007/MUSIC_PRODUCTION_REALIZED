"""CONVERT_ONLY: format conversion without discretionary processing.

Decision table
--------------
* Lossless integer source (PCM/FLAC/ALAC), allowed native rate, source bits <= target bits:
  **bit-exact path** - integer samples copied, no dither, verified sample-for-sample after write.
  (16-bit -> 24-bit is zero padding: lossless, but adds no resolution; reported as such.)
* Anything else (lossy AAC/MP3 decode, float sources, rate change, bit-depth reduction):
  float path - decode once, resample once only if required (soxr VHQ), quantize once with TPDF.
* Decoded lossy audio can exceed 0 dBFS. ``overs_policy``: 'trim' (default; static gain so the
  sample peak sits at -0.1 dBFS - a level change, logged), 'clip' (hard clip, logged), 'fail'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from srm.analysis.truepeak import db
from srm.dsp.quantize import write_float, write_wav_int
from srm.ingest.decode import decode
from srm.ingest.probe import ProbeResult

ALLOWED_RATES = (44100, 48000)


@dataclass
class ConversionPlan:
    bit_exact: bool
    source_rate: int
    target_rate: int
    target_bits: int
    target_channels: int
    resample: bool
    mono_to_dual_mono: bool
    notes: list[str] = field(default_factory=list)


def choose_rate(src_rate: int, allowed=ALLOWED_RATES) -> int:
    if src_rate in allowed:
        return src_rate
    if src_rate % 44100 == 0 and 44100 in allowed:
        return 44100
    if 48000 in allowed:
        return 48000
    return allowed[0]


def plan_conversion(pr: ProbeResult, target_bits: int = 24, allowed_rates=ALLOWED_RATES,
                    fixed_rate: int | None = None) -> ConversionPlan:
    src_rate = int(pr.sample_rate or 0)
    tgt_rate = fixed_rate or choose_rate(src_rate, allowed_rates)
    notes: list[str] = []
    resample = tgt_rate != src_rate
    lossless_int = (not pr.is_lossy) and pr.family in ("lossless_pcm", "lossless_compressed") and \
        not (pr.codec or "").startswith(("pcm_f", "pcm_s32")) and (pr.bits_per_sample or 99) <= 24
    bit_exact = lossless_int and not resample and (pr.bits_per_sample or 99) <= target_bits
    if pr.is_lossy:
        notes.append(f"Lossy {pr.codec} source decoded to PCM: transcoding, not restoration. "
                     "Not Hi-Res / Apple Digital Masters eligible.")
    if resample:
        notes.append(f"Sample rate {src_rate} Hz -> {tgt_rate} Hz with high-quality resampler (single pass).")
    if bit_exact and (pr.bits_per_sample or 0) < target_bits:
        notes.append(f"{pr.bits_per_sample}-bit samples placed in a {target_bits}-bit container (zero padding; "
                     "no added resolution).")
    if not bit_exact and not pr.is_lossy and (pr.bits_per_sample or 0) > target_bits:
        notes.append(f"Bit depth reduced {pr.bits_per_sample} -> {target_bits} with TPDF dither (once).")
    mono = (pr.channels or 2) == 1
    if mono:
        notes.append("Mono source exported as dual-mono stereo (identical channels, no spatial processing).")
    return ConversionPlan(bit_exact=bit_exact, source_rate=src_rate, target_rate=tgt_rate, target_bits=target_bits,
                          target_channels=2, resample=resample, mono_to_dual_mono=mono, notes=notes)


def _to_stereo(a: np.ndarray) -> np.ndarray:
    if a.shape[1] == 1:
        return np.repeat(a, 2, axis=1)
    if a.shape[1] > 2:
        raise ValueError(f"{a.shape[1]}-channel source: stereo downmix is not automatic; needs a decision")
    return a


def convert(src: str | Path, pr: ProbeResult, out_path: str | Path, *, target_bits: int = 24,
            allowed_rates=ALLOWED_RATES, overs_policy: str = "trim", seed: int = 0) -> dict:
    plan = plan_conversion(pr, target_bits, allowed_rates)
    rec: dict = {"mode": "CONVERT_ONLY", "plan": plan.__dict__.copy(), "output": str(out_path)}
    if plan.bit_exact:
        dec = decode(src, sample_rate=plan.source_rate, channels=pr.channels or 2, integer=True)
        q = _to_stereo(dec.audio)
        write_wav_int(out_path, q, plan.target_rate, target_bits)
        rec.update(dither="none", gain_db=0.0, decoder_warnings=dec.warnings)
        # Verify sample-for-sample
        from srm.ingest.probe import probe as _probe
        opr = _probe(out_path)
        back = decode(out_path, sample_rate=opr.sample_rate, channels=2, integer=True).audio
        rec["bit_exact_verified"] = bool(back.shape == q.shape and np.array_equal(back, q))
        rec["float_audio_for_measurement"] = None
        return rec
    dec = decode(src, sample_rate=plan.source_rate, channels=pr.channels or 2, target_rate=plan.target_rate)
    x = _to_stereo(dec.audio).astype(np.float64)
    peak = float(np.abs(x).max()) if x.size else 0.0
    gain_db = 0.0
    rec["decoded_sample_peak_dbfs"] = round(db(peak), 3)
    if peak >= 1.0:
        if overs_policy == "trim":
            gain_db = -0.1 - db(peak)
            x *= 10 ** (gain_db / 20)
            rec["overs_action"] = f"static gain {gain_db:.3f} dB to avoid clipping decoded overs (level change only)"
        elif overs_policy == "clip":
            n = int(np.count_nonzero(np.abs(x) >= 1.0))
            x = np.clip(x, -1.0, 1.0 - 2 ** -(target_bits - 1))
            rec["overs_action"] = f"hard-clipped {n} samples (user policy)"
        else:
            raise RuntimeError(f"decoded audio peaks at {db(peak):+.2f} dBFS; overs_policy=fail")
    qinfo = write_float(out_path, x, plan.target_rate, target_bits, dither="tpdf", seed=seed,
                        clip_policy="allow" if overs_policy == "clip" else "fail")
    rec.update(dither="tpdf", quantize=qinfo, gain_db=round(gain_db, 4), resampler=dec.resampler or None,
               decoder_warnings=dec.warnings, bit_exact_verified=None)
    rec["float_audio_for_measurement"] = None
    return rec
