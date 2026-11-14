"""Full decoding through FFmpeg to NumPy, with decode verification.

Every analysis decodes the *whole* file; a file is only considered usable when FFmpeg
exits cleanly and the decoded length roughly matches the container duration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from srm.core.tools import ffmpeg_input, get_tools, run


class DecodeError(RuntimeError):
    pass


@dataclass
class Decoded:
    audio: np.ndarray  # float32 (n, ch) unless integer decode requested
    sample_rate: int
    channels: int
    warnings: list[str] = field(default_factory=list)
    resampled_from: int | None = None
    resampler: str = ""

    @property
    def n(self) -> int:
        return int(self.audio.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n / float(self.sample_rate)


def _resample_filter(target_rate: int) -> tuple[list[str], str]:
    t = get_tools()
    if t.has_soxr:
        return (["-af", f"aresample=resampler=soxr:precision=28:osr={target_rate}"],
                "soxr precision=28 (VHQ)")
    return (["-af", f"aresample=filter_size=64:cutoff=0.97:osr={target_rate}"],
            "swr filter_size=64 (soxr unavailable)")


def decode(path: str | Path, *, sample_rate: int, channels: int, target_rate: int | None = None,
           integer: bool = False, timeout: float = 1800) -> Decoded:
    """Decode to interleaved float32 (or int32 left-justified when ``integer``).

    ``sample_rate``/``channels`` are the probed native values; resampling happens only
    when ``target_rate`` differs from the native rate.
    """
    t = get_tools()
    args = [t.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", ffmpeg_input(path), "-map", "0:a:0"]
    resampled_from = None
    resampler = ""
    out_rate = sample_rate
    if target_rate and target_rate != sample_rate:
        if integer:
            raise ValueError("integer (bit-exact) decode cannot resample")
        f, resampler = _resample_filter(target_rate)
        args += f
        resampled_from = sample_rate
        out_rate = target_rate
    if integer:
        args += ["-f", "s32le", "-c:a", "pcm_s32le"]
        dtype = "<i4"
    else:
        args += ["-f", "f32le", "-c:a", "pcm_f32le"]
        dtype = "<f4"
    args += ["-ac", str(channels), "-"]
    proc = run(args, timeout=timeout)
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise DecodeError(err[-1500:] or f"ffmpeg exit {proc.returncode}")
    raw = np.frombuffer(proc.stdout, dtype=dtype)
    if raw.size == 0:
        raise DecodeError("decoded zero samples")
    if raw.size % channels:
        raise DecodeError("decoded sample count is not a multiple of the channel count")
    audio = raw.reshape(-1, channels)
    if not integer:
        audio = audio.astype(np.float32, copy=True)
    else:
        audio = audio.copy()
    warnings = [line for line in err.splitlines() if line.strip()][:50]
    return Decoded(audio=audio, sample_rate=out_rate, channels=channels, warnings=warnings,
                   resampled_from=resampled_from, resampler=resampler)
