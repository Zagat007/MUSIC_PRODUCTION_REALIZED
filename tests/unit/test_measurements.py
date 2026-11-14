import subprocess

import numpy as np
import pyloudnorm
import pytest
import soundfile as sf

from srm.analysis.loudness import measure_loudness
from srm.analysis.tempo import estimate_tempo
from srm.analysis.truepeak import measure_peaks
from srm.core.tools import get_tools


def stereo_sine(freq, amp_db, fs=48000, dur=10.0):
    t = np.arange(int(fs * dur)) / fs
    s = 10 ** (amp_db / 20) * np.sin(2 * np.pi * freq * t)
    return np.stack([s, s], 1)


@pytest.mark.parametrize("fs", [44100, 48000, 96000])
def test_reference_sine_minus23(fs):
    # EBU Tech 3341: 1 kHz stereo sine at -23 dBFS reads -23.0 LUFS (+-0.1)
    r = measure_loudness(stereo_sine(997, -23, fs), fs)
    assert abs(r.integrated_lufs - (-23.0)) < 0.1


def test_lra_alternating_levels():
    # EBU Tech 3342 case 1 style: 20 s at -20 then 20 s at -30 LUFS -> LRA ~10 LU
    fs = 48000
    x = np.concatenate([stereo_sine(1000, -20, fs, 20), stereo_sine(1000, -30, fs, 20)])
    r = measure_loudness(x, fs)
    assert abs(r.lra_lu - 10.0) < 0.2


def test_against_pyloudnorm():
    rng = np.random.default_rng(3)
    fs = 48000
    x = rng.standard_normal((fs * 12, 2)) * 0.1
    x[: fs * 4] *= 0.2
    ours = measure_loudness(x, fs).integrated_lufs
    ref = pyloudnorm.Meter(fs).integrated_loudness(x)
    assert abs(ours - ref) < 0.1


def test_true_peak_analytic():
    fs = 48000
    t = np.arange(fs * 2) / fs
    w = np.hanning(len(t))
    y = np.sin(2 * np.pi * fs / 4 * t + np.pi / 4) * 0.5 * w  # samples never hit the crest
    m = measure_peaks(np.stack([y, y], 1), fs)
    assert abs(m["true_peak_dbtp"] - 20 * np.log10(0.5)) < 0.02
    assert m["sample_peak_dbfs"] < m["true_peak_dbtp"] - 2.5


def _ffmpeg_ebur128(path):
    t = get_tools()
    p = subprocess.run([t.ffmpeg, "-nostdin", "-hide_banner", "-i", "file:" + str(path), "-af",
                        "ebur128=peak=true", "-f", "null", "-"], capture_output=True)
    txt = p.stderr.decode("utf-8", "replace")
    summ = txt[txt.rfind("Summary:"):]
    import re

    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summ).group(1))
    lra = float(re.search(r"LRA:\s+(-?[\d.]+) LU", summ).group(1))
    pk = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summ).group(1))
    return i, lra, pk


@pytest.mark.parametrize("name", ["Album A/01 Liquid Night.m4a", "Альбом Б/Flac Track.flac", "Альбом Б/Тихая вода.wav"])
def test_cross_check_ffmpeg(fixture_lib, name):
    from srm.ingest.decode import decode
    from srm.ingest.probe import probe

    path = fixture_lib / name
    pr = probe(path)
    d = decode(path, sample_rate=pr.sample_rate, channels=pr.channels)
    ours = measure_loudness(d.audio, d.sample_rate)
    tp = measure_peaks(d.audio, d.sample_rate)["true_peak_dbtp"]
    i, lra, pk = _ffmpeg_ebur128(path)
    assert abs(ours.integrated_lufs - i) <= 0.15
    assert abs(ours.lra_lu - lra) <= 1.0  # LRA implementations differ in short-term hop
    assert abs(tp - pk) <= 0.3


def test_tempo_half_double_ambiguity(fixture_lib):
    x, fs = sf.read(str(fixture_lib / "Альбом Б" / "Flac Track.flac"))
    r = estimate_tempo(x, fs)
    bpms = [c["bpm"] for c in r["candidates"]]
    # Fixture is 172 BPM: either 172 or 86 must be primary, and both must be listed
    assert any(abs(b - 172) < 3 for b in bpms) and any(abs(b - 86) < 2 for b in bpms)
