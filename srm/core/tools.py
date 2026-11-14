"""Discovery and safe invocation of external tools (FFmpeg / ffprobe).

Rules:
* Commands are always fixed argument lists (never shell=True, never string interpolation).
* Input paths are passed with the ``file:`` protocol prefix so names such as ``http:x.m4a``
  or names starting with ``-`` cannot be interpreted as options or network protocols.
* Nothing here performs network access.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class ToolMissingError(RuntimeError):
    """Raised when FFmpeg/ffprobe cannot be located."""


@dataclass(frozen=True)
class ToolInfo:
    ffmpeg: str
    ffprobe: str
    version_line: str
    has_soxr: bool
    has_libmp3lame: bool
    has_flac: bool
    license_hint: str  # "GPL" / "LGPL" / "unknown" as reported by the build configuration


def _candidate(name: str) -> str | None:
    env = os.environ.get(f"SRM_{name.upper()}")
    if env and Path(env).is_file():
        return env
    local = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / f"{name}.exe"
    if local.is_file():
        return str(local)
    return shutil.which(name)


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args: list[str], *, timeout: float | None = 600, input_bytes: bytes | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    """Run a fixed argument list. No shell, no window on Windows, captured output."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise TypeError("args must be a list[str]")
    proc = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        args,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{Path(args[0]).name} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    return proc


@lru_cache(maxsize=1)
def get_tools() -> ToolInfo:
    ffmpeg = _candidate("ffmpeg")
    ffprobe = _candidate("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolMissingError(
            "FFmpeg/ffprobe not found. Install a free FFmpeg build (e.g. `winget install Gyan.FFmpeg`) "
            "or set SRM_FFMPEG / SRM_FFPROBE to the executables."
        )
    out = run([ffmpeg, "-hide_banner", "-version"], timeout=30).stdout.decode("utf-8", "replace")
    first = out.splitlines()[0] if out else "unknown"
    conf = out
    lic = "GPL" if "--enable-gpl" in conf else ("LGPL" if "configuration:" in conf else "unknown")
    enc = run([ffmpeg, "-hide_banner", "-encoders"], timeout=30).stdout.decode("utf-8", "replace")
    return ToolInfo(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        version_line=first,
        has_soxr="--enable-libsoxr" in conf,
        has_libmp3lame=bool(re.search(r"\blibmp3lame\b", enc)),
        has_flac=bool(re.search(r"\bflac\b", enc)),
        license_hint=lic,
    )


def ffmpeg_input(path: str | os.PathLike) -> str:
    """Return a protocol-safe FFmpeg input/output path for a local file."""
    return "file:" + str(Path(path).resolve())


def tools_status() -> dict:
    try:
        t = get_tools()
        return {
            "status": "AVAILABLE",
            "ffmpeg": t.ffmpeg,
            "ffprobe": t.ffprobe,
            "version": t.version_line,
            "soxr_resampler": t.has_soxr,
            "mp3_encoder_libmp3lame": t.has_libmp3lame,
            "flac_encoder": t.has_flac,
            "build_license": t.license_hint,
        }
    except ToolMissingError as exc:
        return {"status": "UNAVAILABLE", "error": str(exc)}
