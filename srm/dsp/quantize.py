"""Final quantization and PCM file writing.

* Dither (TPDF, 2 LSB peak-to-peak) is applied exactly once, at the final float->int step.
* The bit-exact path (``write_int``) is used when integer source samples are passed through
  unchanged - no dither, no rounding.
* Writes go to a temporary sibling and are renamed into place (never overwrite).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from srm.core.paths import commit_tmp, tmp_sibling

SUBTYPES = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}


class ClipPolicyError(RuntimeError):
    pass


def quantize(x: np.ndarray, bits: int, *, dither: str = "tpdf", seed: int = 0) -> tuple[np.ndarray, dict]:
    """Float [-1,1) -> left-justified int32 (for soundfile) at ``bits`` resolution."""
    if bits not in SUBTYPES:
        raise ValueError(f"unsupported bit depth {bits}")
    x = np.asarray(x, dtype=np.float64)
    scale = float(2 ** (bits - 1))
    y = x * scale
    info = {"bits": bits, "dither": dither, "seed": seed}
    if dither == "tpdf":
        rng = np.random.default_rng(seed)
        y = y + (rng.random(y.shape) - rng.random(y.shape))  # TPDF, +-1 LSB
    elif dither != "none":
        raise ValueError("dither must be 'tpdf' or 'none'")
    q = np.round(y)
    lo, hi = -scale, scale - 1
    clipped = int(np.count_nonzero((q < lo) | (q > hi)))
    info["clipped_samples"] = clipped
    q = np.clip(q, lo, hi).astype(np.int64)
    return (q << (32 - bits)).astype(np.int32), info


def write_wav_int(path: str | Path, q32: np.ndarray, fs: int, bits: int) -> Path:
    """Write left-justified int32 samples as PCM WAV at ``bits`` (exclusive create)."""
    target = Path(path)
    tmp = tmp_sibling(target, ".wav")
    sf.write(str(tmp), q32, fs, subtype=SUBTYPES[bits], format="WAV")
    return commit_tmp(tmp, target)


def write_flac_int(path: str | Path, q32: np.ndarray, fs: int, bits: int) -> Path:
    if bits not in (16, 24):
        raise ValueError("FLAC supports 16/24-bit here")
    target = Path(path)
    tmp = tmp_sibling(target, ".flac")
    sf.write(str(tmp), q32, fs, subtype=SUBTYPES[bits], format="FLAC")
    return commit_tmp(tmp, target)


def write_float(path: str | Path, x: np.ndarray, fs: int, bits: int, *, dither: str = "tpdf",
                seed: int = 0, fmt: str = "WAV", clip_policy: str = "fail") -> dict:
    """Quantize once and write. ``clip_policy``: 'fail' (default) or 'allow' (logged)."""
    q, info = quantize(x, bits, dither=dither, seed=seed)
    if info["clipped_samples"] and clip_policy == "fail":
        raise ClipPolicyError(f"{info['clipped_samples']} samples would clip at {bits}-bit quantization")
    if fmt == "WAV":
        write_wav_int(path, q, fs, bits)
    elif fmt == "FLAC":
        write_flac_int(path, q, fs, bits)
    else:
        raise ValueError(fmt)
    return info
