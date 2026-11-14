"""Generate synthetic, copyright-free test fixtures.

Usage: python tests/audio_fixtures/make_fixtures.py <out_dir> [--many N]

Synthetic signals verify measurement/IO correctness only; they prove nothing about
musical mastering quality.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def _ffmpeg():
    import shutil

    return shutil.which("ffmpeg")


def synth_song(fs=48000, dur=24.0, bpm=174.0, seed=1, level=0.5, bass=True):
    rng = np.random.default_rng(seed)
    n = int(fs * dur)
    t = np.arange(n) / fs
    out = np.zeros((n, 2))
    beat = 60.0 / bpm
    # kick on 1 and "and of 3" (dnb-ish), snare on 2 and 4
    def env(length, tau):
        k = np.arange(int(length * fs)) / fs
        return np.exp(-k / tau)
    kick_len = 0.25
    ke = env(kick_len, 0.06)
    kk = np.arange(len(ke)) / fs
    kick = np.sin(2 * np.pi * (50 + 120 * np.exp(-kk / 0.02)) * kk) * ke
    sn_e = env(0.18, 0.04)
    snare = (rng.standard_normal(len(sn_e)) * 0.6 + np.sin(2 * np.pi * 190 * np.arange(len(sn_e)) / fs) * 0.4) * sn_e
    hat_e = env(0.05, 0.01)
    hat = np.diff(np.concatenate([[0], rng.standard_normal(len(hat_e))])) * hat_e * 0.25
    bar = 4 * beat
    nb = int(dur / bar) + 1
    for b in range(nb):
        t0 = b * bar
        for pos, smp, g in ((0, kick, 0.9), (2.5 * beat, kick, 0.8), (beat, snare, 0.7), (3 * beat, snare, 0.7)):
            i = int((t0 + pos) * fs)
            if i < n:
                m = min(len(smp), n - i)
                out[i:i + m] += (smp[:m] * g)[:, None]
        for h in range(8):
            i = int((t0 + h * beat / 2) * fs)
            if i < n:
                m = min(len(hat), n - i)
                out[i:i + m, 0] += hat[:m] * 0.8
                out[i:i + m, 1] += hat[:m] * 0.5
    if bass:
        f = 55.0 * (2 ** (np.floor(t / bar) % 4 * 3 / 12))
        ph = 2 * np.pi * np.cumsum(f) / fs
        out += (0.35 * np.sin(ph) * (0.6 + 0.4 * np.sin(2 * np.pi * t / beat) ** 2))[:, None]
    pad = sum(0.06 * np.sin(2 * np.pi * fr * t + p) for fr, p in ((220, 0), (277.2, 1), (329.6, 2), (440, 0.5)))
    out[:, 0] += pad * (1 + 0.2 * np.sin(2 * np.pi * 0.1 * t))
    out[:, 1] += pad * (1 - 0.2 * np.sin(2 * np.pi * 0.1 * t))
    fade = int(0.05 * fs)
    out[:fade] *= np.linspace(0, 1, fade)[:, None]
    out[-fade * 10:] *= np.linspace(1, 0, fade * 10)[:, None]
    out *= level / np.abs(out).max()
    return out


def ambient(fs=44100, dur=20.0, seed=2):
    t = np.arange(int(fs * dur)) / fs
    rng = np.random.default_rng(seed)
    swell = 0.15 + 0.85 * np.sin(np.pi * t / dur) ** 2
    sig = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) / (i + 1) for i, f in enumerate((196, 246.9, 293.7, 392)))
    x = np.stack([sig * swell, np.roll(sig, 12) * swell], axis=1)
    return 0.3 * x / np.abs(x).max()


def write(path: Path, x, fs, subtype="PCM_24"):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x, fs, subtype=subtype)


def enc(src: Path, dst: Path, args: list[str]):
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_ffmpeg(), "-nostdin", "-v", "error", "-y", "-i", "file:" + str(src), *args, "file:" + str(dst)],
                   check=True)


def make_all(out: Path) -> dict:
    out = Path(out)
    lib = out / "library"
    tmp = out / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    fs = 48000
    files = {}
    # Album A: DnB-ish, Suno-like AAC m4a + prompt txt
    a = lib / "Album A"
    song = synth_song(fs, 24, 174, seed=1)
    write(tmp / "song1.wav", song, fs)
    enc(tmp / "song1.wav", a / "01 Liquid Night.m4a", ["-c:a", "aac", "-b:a", "192k"])
    (a / "01 Liquid Night.txt").write_text(
        "Title: Liquid Night\nStyle: liquid drum and bass, warm pads, female vocal\nModel: v4.5\n\n"
        "[Verse]\nCity lights are fading slow\n[Chorus]\nHold on\n", encoding="utf-8")
    song2 = synth_song(fs, 20, 172, seed=3, level=0.9)
    write(tmp / "song2.wav", song2, fs)
    enc(tmp / "song2.wav", a / "02 Rain Code.m4a", ["-c:a", "aac", "-b:a", "256k"])
    enc(tmp / "song2.wav", a / "02 Rain Code (1).m4a", ["-c:a", "aac", "-b:a", "128k"])  # alternate take
    # Album Б (Cyrillic) - ALAC m4a + cp1251 prompt, FLAC, ambient WAV 16-bit 44.1k
    b = lib / "Альбом Б"
    enc(tmp / "song1.wav", b / "Песня №1 — тест.m4a", ["-c:a", "alac", "-sample_fmt", "s32p"])
    (b / "Песня №1 — тест.txt").write_bytes("Стиль: эмбиент, джаз\nТекст:\nНочь и тишина\n".encode("cp1251"))
    amb = ambient()
    write(b / "Тихая вода.wav", amb, 44100, "PCM_16")
    enc(tmp / "song2.wav", b / "Flac Track.flac", ["-c:a", "flac", "-sample_fmt", "s32"])
    # Edge cases
    e = lib / "Edge Cases"
    write(e / "silence.wav", np.zeros((fs * 3, 2)), fs)
    t = np.arange(fs * 4) / fs
    s = 0.5 * np.sin(2 * np.pi * 440 * t)
    write(e / "dc_offset.wav", np.stack([s + 0.05, s + 0.05], 1), fs)
    write(e / "phase_inverted.wav", np.stack([s, -s], 1), fs)
    cl = np.clip(1.6 * np.sin(2 * np.pi * 220 * t), -1, 1) * 0.999
    write(e / "clipped_sine.wav", np.stack([cl, cl], 1), fs, "PCM_16")
    # Intersample over: fs/4 sine at 45 deg phase; samples at +-1.0, true peak ~ +3 dBTP
    isp = np.sin(2 * np.pi * (fs / 4) * t + np.pi / 4) / np.sin(np.pi / 4) * 0.999
    write(e / "intersample_over.wav", np.stack([isp, isp], 1), fs, "FLOAT")
    clicks = song.copy() * 0.5
    for pos in (3.0, 7.5, 12.25):
        i = int(pos * fs)
        clicks[i] += 0.6
        clicks[i + 1] -= 0.5
    write(e / "clicks.wav", clicks, fs)
    write(e / "mono.wav", song[:, :1], fs)
    write(tmp / "hi.wav", synth_song(96000, 8, 128, seed=5), 96000)
    enc(tmp / "hi.wav", e / "hires_96k.flac", ["-c:a", "flac"])
    enc(tmp / "song1.wav", e / "preview.mp3", ["-c:a", "libmp3lame", "-b:a", "320k"])
    (e / "corrupt.wav").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(64)))
    full = (a / "01 Liquid Night.m4a").read_bytes()
    (e / "truncated.m4a").write_bytes(full[: len(full) // 3])
    (e / "lonely_prompt.md").write_text("# Notes\nNo matching audio here.", encoding="utf-8")
    for p in lib.rglob("*"):
        if p.is_file():
            files[str(p.relative_to(lib))] = p.stat().st_size
    return {"library": str(lib), "files": files}


def make_many(out: Path, n: int, seconds: float = 6.0) -> Path:
    lib = Path(out) / "batch_library"
    tmp = Path(out) / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    base = synth_song(48000, seconds, 140, seed=9, level=0.6)
    write(tmp / "base.wav", base, 48000)
    enc(tmp / "base.wav", tmp / "base.m4a", ["-c:a", "aac", "-b:a", "160k"])
    data = (tmp / "base.m4a").read_bytes()
    for i in range(n):
        album = lib / f"Batch {i // 25 + 1:02d}"
        album.mkdir(parents=True, exist_ok=True)
        # identical audio bytes are fine for throughput; vary a few by re-encoding gain
        p = album / f"{i + 1:03d} Track {i + 1}.m4a"
        if i % 10 == 0:
            g = synth_song(48000, seconds, 120 + i % 50, seed=i, level=0.4 + (i % 5) / 10)
            write(tmp / f"g{i}.wav", g, 48000)
            enc(tmp / f"g{i}.wav", p, ["-c:a", "aac", "-b:a", "160k"])
        else:
            p.write_bytes(data)
    return lib


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--many", type=int, default=0)
    a = ap.parse_args()
    if a.many:
        print(make_many(Path(a.out), a.many))
    else:
        r = make_all(Path(a.out))
        print(r["library"], len(r["files"]), "files")
    sys.exit(0)
