import numpy as np
import pytest
import soundfile as sf

from conftest import track_by_name
from srm.analysis.defects import detect_clicks
from srm.dsp.repair import apply_edit, plan_from_candidates, regression_check
from srm.service.engine import Engine, EngineError


@pytest.fixture(scope="module")
def eng(scanned):
    return scanned[0]


def test_click_plan_auto_needs_confirmation(eng):
    t = track_by_name(eng, "clicks.wav")
    plan = eng.propose_repairs(t["id"])
    clicks = [e for e in plan["edits"] if e["type"] == "click_pop"]
    assert clicks and all(e["regression"]["passed"] for e in clicks)
    # auto-eligible edits are NOT applied until the user confirms the auto set
    assert all(e["decision"] == "AUTO_ACCEPTED_PENDING_CONFIRMATION" for e in clicks)
    assert any(n["status"] == "NEEDS_SOURCE_REGENERATION_OR_STEMS" for n in plan["not_repairable"])
    assert eng.approved_repair_plan(t["id"]) is None


def test_restore_requires_approved_plan(eng):
    t = track_by_name(eng, "Тихая вода")
    with pytest.raises(EngineError):
        eng.suggest_mastering(t["id"], mode="RESTORE_AND_MASTER")


def test_restore_and_master_end_to_end(eng):
    t = track_by_name(eng, "clicks.wav")
    plan = eng.propose_repairs(t["id"])
    pv = eng.preview_repair(t["id"], plan["plan_id"], plan["edits"][0]["edit_id"])
    orig, fs = sf.read(pv["files"]["original"])
    diff, _ = sf.read(pv["files"]["difference_x4"])
    nz = np.where(np.abs(diff).max(axis=1) > 0)[0]
    assert nz.size and (nz.max() - nz.min()) / fs < 0.003  # only the repaired region differs
    p2 = eng.decide_repairs(t["id"], plan["plan_id"], accept_auto=True)
    assert p2["status"] == "APPROVED"
    prop = eng.suggest_mastering(t["id"], mode="RESTORE_AND_MASTER", loudness_policy="preserve")
    r = eng.render_version(t["id"], prop["version_no"], codec_preview=False)
    log = r["processing"]["repair_log"]
    assert log and all(x["applied"] for x in log)
    y, fs = sf.read(r["output"]["path"])
    left = [c for c in detect_clicks(y, fs) if any(abs(c["start_s"] - s) < 0.01 for s in (3.0, 7.5))]
    assert not left
    src = eng.list_versions(t["id"])
    assert any(v["mode"] == "RESTORE_AND_MASTER" for v in src)


def _music_with_clips():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audio_fixtures"))
    from make_fixtures import synth_song

    x = synth_song(48000, 8, 120, seed=7, level=0.5)
    t = np.arange(x.shape[0]) / 48000
    for pos in (2.0, 4.0, 6.0):  # three short loud 1 kHz bursts that get clipped
        m = (t >= pos) & (t < pos + 0.004)
        x[m] += (0.9 * np.sin(2 * np.pi * 1000 * (t[m] - pos)))[:, None]
    return np.clip(x, -0.8, 0.8), 48000


def test_declip_candidate_needs_approval():
    from srm.analysis.defects import detect_clipping

    x, fs = _music_with_clips()
    cands = [c for c in detect_clipping(x, fs)]
    assert cands
    plan = plan_from_candidates(cands, [])
    eds = [e for e in plan["edits"] if e["method"] == "lsar_declip_candidate"]
    assert eds and not any(e["auto_eligible"] for e in eds)
    ed = dict(eds[0])
    y = apply_edit(x, fs, ed)
    chk = regression_check(x, y, fs, ed)
    assert chk["exceeds_clip_level_consistently"] and chk["outside_region_bit_identical"]


def test_widespread_clipping_not_offered(eng):
    t = track_by_name(eng, "clipped_sine")
    plan = eng.propose_repairs(t["id"])
    assert not [e for e in plan["edits"] if e["type"] == "hard_clipping"]
    assert any(n["type"] == "hard_clipping" and n["status"] == "NEEDS_SOURCE_REGENERATION_OR_STEMS"
               for n in plan["not_repairable"])


def test_cannot_approve_failed_edit(eng):
    t = track_by_name(eng, "clicks.wav")
    plan = eng.propose_repairs(t["id"])
    with eng.db.transaction():
        pass
    # Forge a failing regression result in a copy of the plan and ensure approval is refused
    import json

    body = json.loads(eng.db.one("SELECT plan_json FROM repair_plans WHERE id=?", (plan["plan_id"],))["plan_json"])
    body["edits"][0]["regression"]["passed"] = False
    eng.db.execute("UPDATE repair_plans SET plan_json=? WHERE id=?", (json.dumps(body), plan["plan_id"]))
    with pytest.raises(EngineError):
        eng.decide_repairs(t["id"], plan["plan_id"], approve=[body["edits"][0]["edit_id"]])


def test_stem_import_validation(tmp_path):
    from srm.service.engine import Engine as E

    lib = tmp_path / "lib"
    (lib / "Song").mkdir(parents=True)
    rng = np.random.default_rng(0)
    a = rng.standard_normal((48000 * 3, 2)) * 0.05
    b = rng.standard_normal((48000 * 3, 2)) * 0.05
    sf.write(lib / "Song" / "song.wav", a + b, 48000, subtype="PCM_24")
    stems = lib / "Song" / "stems"
    stems.mkdir()
    sf.write(stems / "vocals.wav", a, 48000, subtype="PCM_24")
    sf.write(stems / "music.wav", b, 48000, subtype="PCM_24")
    e = E(tmp_path / "proj")
    e.add_library_root(lib)
    e.scan()
    tid = [t for t in e.list_tracks() if t["rel_path"].endswith("song.wav")][0]["id"]
    info = e.import_stems(tid, stems)
    assert info["verdict"] == "CONSISTENT" and info["sum_vs_mix_residual_db"] < -60
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    sf.write(outside / "x.wav", a, 48000)
    with pytest.raises(EngineError):
        e.import_stems(tid, outside)
    assert e.separation_capability()["status"] in ("UNAVAILABLE", "INSTALLED_NOT_ENABLED")
