"""ffprobe-based container/codec identification. The extension is never trusted."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from srm.core.tools import ffmpeg_input, get_tools, run

LOSSLESS_CODECS = {"alac", "flac", "wavpack", "tta", "ape", "mlp", "truehd"}
LOSSY_CODECS = {"aac", "mp3", "mp2", "vorbis", "opus", "ac3", "eac3", "wmav2", "wmav1", "aac_latm"}


@dataclass
class ProbeResult:
    ok: bool
    path: str
    container: str = ""
    codec: str = ""
    codec_long: str = ""
    codec_profile: str = ""
    family: str = "unknown"  # lossless_pcm | lossless_compressed | lossy | unknown
    is_lossy: bool | None = None
    bit_rate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str = ""
    duration_s: float | None = None
    bits_per_sample: int | None = None  # meaningful only for lossless sources
    sample_fmt: str = ""
    audio_streams: int = 0
    tags: dict = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def classify_codec(codec: str) -> tuple[str, bool | None]:
    c = (codec or "").lower()
    if c.startswith("pcm_"):
        return "lossless_pcm", False
    if c in LOSSLESS_CODECS:
        return "lossless_compressed", False
    if c in LOSSY_CODECS:
        return "lossy", True
    return "unknown", None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe(path: str | Path, timeout: float = 60) -> ProbeResult:
    t = get_tools()
    p = Path(path)
    proc = run(
        [t.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         ffmpeg_input(p)],
        timeout=timeout,
    )
    res = ProbeResult(ok=False, path=str(p))
    if proc.returncode != 0:
        res.error = proc.stderr.decode("utf-8", "replace").strip()[-1000:] or f"ffprobe exit {proc.returncode}"
        return res
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        res.error = f"ffprobe JSON error: {exc}"
        return res
    fmt = data.get("format", {})
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    res.container = fmt.get("format_name", "")
    res.audio_streams = len(streams)
    res.tags = {str(k): str(v) for k, v in (fmt.get("tags") or {}).items()}
    if not streams:
        res.error = "no audio stream"
        return res
    if len(streams) > 1:
        res.warnings.append(f"{len(streams)} audio streams; only the first is used")
    s = streams[0]
    res.codec = s.get("codec_name", "")
    res.codec_long = s.get("codec_long_name", "")
    res.codec_profile = s.get("profile", "") or ""
    res.family, res.is_lossy = classify_codec(res.codec)
    res.bit_rate = _int(s.get("bit_rate")) or _int(fmt.get("bit_rate"))
    res.sample_rate = _int(s.get("sample_rate"))
    res.channels = _int(s.get("channels"))
    res.channel_layout = s.get("channel_layout", "") or ""
    res.duration_s = _float(s.get("duration")) or _float(fmt.get("duration"))
    res.sample_fmt = s.get("sample_fmt", "") or ""
    if not res.is_lossy:
        bits = _int(s.get("bits_per_raw_sample")) or _int(s.get("bits_per_sample"))
        res.bits_per_sample = bits if bits else None
    res.ok = True
    ext = p.suffix.lower()
    if ext == ".wav" and not res.codec.startswith("pcm_"):
        res.warnings.append(f".wav extension but codec is {res.codec}")
    if ext in (".m4a", ".mp4") and res.codec not in ("aac", "alac"):
        res.warnings.append(f".m4a extension but codec is {res.codec}")
    if res.is_lossy:
        res.warnings.append(
            f"LOSSY SOURCE ({res.codec}). Converting to WAV/FLAC is transcoding: it does not restore "
            "information removed by the encoder. Not eligible as Hi-Res / Apple Digital Masters source."
        )
    return res
