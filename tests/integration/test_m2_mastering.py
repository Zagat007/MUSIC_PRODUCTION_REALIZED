import numpy as np
import pytest
import soundfile as sf

from conftest import track_by_name
from srm.analysis.loudness import integrated_lufs
from srm.core.paths import sha256_file
from srm.ingest.probe import probe
from srm.service.engine import EngineError


@pytest.fixture(scope="module")
def eng(scanned):
    return scanned[0]


def test_safe_master_render_and_validate(eng):
    t = track_by_name(eng, "02 Rain Code.m4a")
    p = eng.suggest_mastering(t["id"])
    assert eng.version(t["id"], p["version_no"])["status"] == "PROPOSED"
    r = eng.render_version(t["id"], p["version_no"])
    assert r["policy_check"]["true_peak_within_ceiling"]["status"] == "PASS"
    assert r["policy_check"]["duration_preserved"]["status"] == "PASS"
    assert r["output_measurements"]["peaks"]["true_peak_dbtp"] <= -1.0 + 0.05
    pr = probe(r["output"]["path"])
    assert (pr.codec, pr.sample_rate, pr.bits_per_sample, pr.channels) == ("pcm_s24le", 48000, 24, 2)
    assert r["codec_preview"]["status"] == "OK"  # real MP3 encode analysed
    assert r["comparison"]["loudness.integrated_lufs"]["before"] is not None
    assert [s["stage"] for s in r["processing"]["stages"]][0] == "input"
    # immutability: re-rendering the same version is refused
    with pytest.raises(EngineError):
        eng.render_version(t["id"], p["version_no"])


def test_render_deterministic(eng):
    t = track_by_name(eng, "Песня №1")
    a = eng.suggest_mastering(t["id"], loudness_policy="target", target_lufs=-12.0, preset="liquid_dnb")
    b = eng.suggest_mastering(t["id"], loudness_policy="target", target_lufs=-12.0, preset="liquid_dnb")
    ra = eng.render_version(t["id"], a["version_no"], codec_preview=False)
    rb = eng.render_version(t["id"], b["version_no"], codec_preview=False)
    xa, _ = sf.read(ra["output"]["path"], dtype="int32")
    xb, _ = sf.read(rb["output"]["path"], dtype="int32")
    # Dither seed is per version -> identical except LSB dither; audio matches to within 2 LSB
    assert np.abs((xa >> 8) - (xb >> 8)).max() <= 2
    assert ra["config_hash"] == rb["config_hash"]


def test_hostile_source_ceiling(eng):
    t = track_by_name(eng, "intersample_over")
    p = eng.suggest_mastering(t["id"], loudness_policy="preserve")
    r = eng.render_version(t["id"], p["version_no"])
    assert r["output_measurements"]["peaks"]["true_peak_dbtp"] <= -1.0 + 0.05


def test_ab_preview_loudness_matched(eng):
    t = track_by_name(eng, "02 Rain Code.m4a")
    p = eng.suggest_mastering(t["id"], loudness_policy="target", target_lufs=-11.0, preset="liquid_dnb")
    eng.render_version(t["id"], p["version_no"])
    pv = eng.create_preview(t["id"], p["version_no"], duration_s=10)
    assert set(pv["clips"]) >= {"source_decoded", f"v{p['version_no']:03d}_safe_master", "encoded_mp3_preview"}
    levels = []
    for c in pv["clips"].values():
        x, fs = sf.read(c["path"])
        levels.append(integrated_lufs(x, fs))
    assert max(levels) - min(levels) < 0.3


def test_approval_flow_and_tamper_check(eng, tmp_path):
    t = track_by_name(eng, "Тихая вода")
    p = eng.suggest_mastering(t["id"])
    with pytest.raises(EngineError):
        eng.approve(t["id"], p["version_no"])  # not rendered yet
    r = eng.render_version(t["id"], p["version_no"], codec_preview=False)
    v = eng.approve(t["id"], p["version_no"], note="A-B listened on headphones")
    assert v["status"] == "APPROVED" and v["approvals"][0]["decision"] == "APPROVED"
    assert eng.approved_version(t["id"])["version_no"] == p["version_no"]
    assert sha256_file(r["output"]["path"]) == v["output_sha256"]


def test_album_cohesion(eng):
    for tr in eng.list_tracks("Album A"):
        eng.analyze_track(tr["id"])
    c = eng.album_cohesion("Album A")
    assert c["status"] == "OK" and len(c["tracks"]) == 3
    assert all("rel_loudness_lu" in r for r in c["tracks"])
