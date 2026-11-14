import numpy as np
import pytest

from srm.analysis.loudness import integrated_lufs
from srm.analysis.truepeak import db, peak_envelope
from srm.dsp.chain import ChainConfigError, default_chain, run_chain, true_peak_limit, validate_chain
from srm.dsp.proposal import propose

FS = 48000


def music(seed=0, dur=6.0, level=0.9):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audio_fixtures"))
    from make_fixtures import synth_song

    return synth_song(FS, dur, 174, seed=seed, level=level)


def test_all_bypassed_is_identity():
    x = music()
    cfg = default_chain()
    cfg["limiter"]["enabled"] = False
    y = run_chain(x, FS, cfg).audio
    assert np.array_equal(y, x)


def test_limiter_ceiling_on_hostile_signals():
    t = np.arange(FS * 3) / FS
    isp = np.sin(2 * np.pi * FS / 4 * t + np.pi / 4) / np.sin(np.pi / 4)  # +3 dBTP
    clipped = np.clip(2.0 * np.sin(2 * np.pi * 220 * t), -1, 1)
    for sig in (np.stack([isp, isp], 1), np.stack([clipped, -clipped], 1), music(level=1.0) * 2.5):
        for ceiling in (-1.0, -2.0):
            y, info = true_peak_limit(sig, FS, ceiling)
            assert db(peak_envelope(y, FS).max()) <= ceiling + 1e-3
            assert y.shape == sig.shape


def test_limiter_leaves_quiet_regions_untouched():
    x = music(level=0.2)
    x[FS * 2] += 0.95  # single loud spike
    y, info = true_peak_limit(x, FS, -1.0, lookahead_ms=1.5, release_ms=50)
    far = np.r_[0: FS, FS * 3: x.shape[0]]
    assert np.array_equal(y[far], x[far])  # samples away from the event are bit-identical
    assert info["max_gr_db"] > 0


def test_zero_latency_alignment():
    x = music()
    cfg = default_chain()
    cfg["eq"] = {"enabled": True, "bands": [{"type": "peak", "freq_hz": 3000.0, "gain_db": -1.5, "q": 0.8}]}
    cfg["gain"] = {"enabled": True, "gain_db": 3.0}
    y = run_chain(x, FS, cfg).audio
    a, b = x[:, 0], y[:, 0]
    c = np.correlate(b[FS: FS + 4096], a[FS - 64: FS + 4096 + 64], mode="valid")
    assert int(np.argmax(c)) - 64 == 0
    assert y.shape == x.shape


def test_bounds_enforced():
    bad = default_chain()
    bad["eq"] = {"enabled": True, "bands": [{"type": "peak", "freq_hz": 3000.0, "gain_db": 9.0, "q": 1.0}]}
    with pytest.raises(ChainConfigError):
        validate_chain(bad)
    with pytest.raises(ChainConfigError):
        validate_chain({"compressor": {"ratio": 10.0}})
    with pytest.raises(ChainConfigError):
        validate_chain({"limiter": {"ceiling_dbtp": 0.5}})
    with pytest.raises(ChainConfigError):
        validate_chain({"shell": {"enabled": True}})


def test_compressor_gentle_and_deterministic():
    x = music()
    cfg = default_chain()
    cfg["compressor"] = {"enabled": True, "threshold_db": -20.0, "ratio": 1.5, "attack_ms": 30.0,
                         "release_ms": 250.0, "knee_db": 6.0}
    r1 = run_chain(x, FS, cfg)
    r2 = run_chain(x, FS, cfg)
    assert np.array_equal(r1.audio, r2.audio)
    gr = [s for s in r1.stages if s["stage"] == "compressor"][0]["info"]["max_gr_db"]
    assert 0 < gr < 8


def test_proposal_never_forces_minus14_and_caps_limiting():
    meas = {"loudness": {"integrated_lufs": -20.0, "lra_lu": 6.0}, "peaks": {"true_peak_dbtp": -0.5},
            "dynamics": {}, "spectral": {"band_energy_db_rel": {}}, "defects": {"candidates": []},
            "style": {"ranked": [{"style": "ambient_jazz"}], "mixed_or_uncertain": False}}
    assert propose(meas)["preset"] == "generic"  # LOW-confidence guess alone never picks the preset
    p = propose(meas, preset="ambient_jazz")  # user-confirmed style
    g = p["chain"]["gain"]
    # Ambient window starts at -18 LUFS; peak already -0.5 dBTP -> gain capped by max_limiter_gr (1 dB)
    assert p["preset"] == "ambient_jazz"
    assert g["enabled"] and g["gain_db"] <= 0.5 + 1e-9
    pres = propose(meas, preset="ambient_jazz", loudness_policy="preserve")
    assert not pres["chain"]["gain"]["enabled"]
    assert all("settings" in a and "risk" in a and "evidence" in a for a in p["actions"])


def test_eq_changes_only_intended_band():
    rng = np.random.default_rng(1)
    x = rng.standard_normal((FS * 4, 2)) * 0.05
    cfg = default_chain()
    cfg["limiter"]["enabled"] = False
    cfg["eq"] = {"enabled": True, "bands": [{"type": "peak", "freq_hz": 3500.0, "gain_db": -1.5, "q": 0.8}]}
    y = run_chain(x, FS, cfg).audio
    from scipy.signal import welch

    f, px = welch(x[:, 0], FS, nperseg=8192)
    _, py = welch(y[:, 0], FS, nperseg=8192)
    d = 10 * np.log10(py / px)
    assert abs(d[(f > 80) & (f < 300)].mean()) < 0.1
    assert -1.7 < d[(f > 3300) & (f < 3700)].mean() < -1.2
    assert abs(integrated_lufs(y, FS) - integrated_lufs(x, FS)) < 1.0


def test_limiter_file_start_at_full_scale():
    # Regression: box-average padding once let the first ~1.5 ms escape limiting.
    t = np.arange(FS) / FS
    s = 0.999 * np.sin(2 * np.pi * FS / 4 * t + np.pi / 4) / np.sin(np.pi / 4)
    x = np.stack([s, s], 1).astype(np.float32).astype(np.float64)
    y, info = true_peak_limit(x, FS, -1.0)
    head = peak_envelope(y, FS)[: int(0.02 * FS)]  # full-signal envelope (slicing adds edge overshoot)
    assert db(head.max()) <= -1.0
    assert info["max_gr_db"] < 6.0  # no runaway iterations


def test_low_confidence_style_never_picks_loud_preset():
    from srm.dsp.proposal import pick_preset

    meas = {"style": {"ranked": [{"style": "pop_electronic"}], "confidence": "LOW", "mixed_or_uncertain": False}}
    assert pick_preset(meas, None)[0] == "generic"
    assert pick_preset(meas, "pop_electronic")[0] == "pop_electronic"  # explicit user choice wins
