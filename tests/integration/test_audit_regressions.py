"""Regression tests for the defects found in the independent audit of f412904.

Each test reproduces one confirmed failure on small synthetic audio and pins the corrected behaviour.
"""
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

FS = 48000


def _tone(sec=3.0, freq=220.0, amp=0.3, channels=2):
    t = np.arange(int(sec * FS)) / FS
    x = amp * np.sin(2 * np.pi * freq * t) * np.minimum(1, t / 0.05) * np.minimum(1, (sec - t) / 0.05)
    return x[:, None] if channels == 1 else np.column_stack([x, 0.9 * x])


def make_project(tmp_path, n=2, root="music", album="01 - Album", project="project"):
    from srm.service.engine import Engine

    lib = tmp_path / root / album
    lib.mkdir(parents=True)
    for i in range(1, n + 1):
        sf.write(lib / f"{i:02d} - Song {i}.wav", _tone(freq=200.0 + 60 * i), FS, subtype="PCM_24")
    e = Engine(tmp_path / project)
    e.add_library_root(lib.parent)
    e.scan()
    return e, lib


def render(e, tid, **kw):
    v = e.suggest_mastering(tid, **kw)["version_no"]
    e.render_version(tid, v, codec_preview=kw.pop("codec_preview", True))
    return v


# ---------------------------------------------------------------- approvals / export choice
def test_latest_approval_wins_and_supersedes_the_older_one(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    v1 = render(e, tid)
    v2 = render(e, tid, loudness_policy="preserve")
    e.approve(tid, v2)
    e.approve(tid, v1)  # the user changed their mind: v1 is the latest decision
    statuses = {v["version_no"]: v["status"] for v in e.list_versions(tid)}
    assert statuses == {v1: "APPROVED", v2: "RENDERED"}
    assert e.approved_version(tid)["version_no"] == v1


def test_approved_version_uses_latest_decision_in_legacy_projects(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    v1, v2 = render(e, tid), render(e, tid, loudness_policy="preserve")
    e.approve(tid, v2)
    e.approve(tid, v1)
    e.db.execute("UPDATE versions SET status='APPROVED' WHERE track_id=?", (tid,))  # state written by 0.1.0
    assert e.approved_version(tid)["version_no"] == v1


# ---------------------------------------------------------------- render robustness
def test_damaged_cover_does_not_fail_the_render(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    t = e.list_tracks()[0]
    e.set_album_description(t["album_id"], "ночь город")
    cover = Path(e.generate_album_cover(t["album_id"])["cover"]["path"])
    cover.write_bytes(cover.read_bytes()[:-10] + b"0123456789")
    v = e.suggest_mastering(t["id"])["version_no"]
    report = e.render_version(t["id"], v)
    assert e.version(t["id"], v)["status"] == "RENDERED"
    assert report["codec_preview"]["status"] == "OK" and report["codec_preview"]["cover_warning"]


def test_version_cannot_be_rendered_twice(tmp_path):
    from srm.service.engine import EngineError

    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    v = e.suggest_mastering(tid)["version_no"]
    e.db.execute("UPDATE versions SET status='RENDERING' WHERE track_id=? AND version_no=?", (tid, v))
    with pytest.raises(EngineError):
        e.render_version(tid, v)


def test_source_changed_with_same_size_is_refused(tmp_path):
    from srm.service.engine import EngineError

    e, lib = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    p = next(lib.glob("*.wav"))
    data = bytearray(p.read_bytes())
    data[5000:5010] = b"\x01" * 10
    p.write_bytes(bytes(data))
    with pytest.raises(EngineError, match="content"):
        e.suggest_mastering(tid)  # needs an analysis -> must notice the changed bytes


def test_same_job_started_twice_renders_once(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    jid = e.create_job("render", [tid], ["render"], {"mode": "SAFE_MASTER", "codec_preview": False})
    threads = [threading.Thread(target=e.run_job, args=(jid,)) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [(v["version_no"], v["status"]) for v in e.list_versions(tid)] == [(1, "RENDERED")]
    assert e.job_status(jid)["status"] == "COMPLETED"


def test_job_item_claimed_by_another_runner_is_skipped(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    jid = e.create_job("render", [tid], ["render"], {"mode": "SAFE_MASTER", "codec_preview": False})
    real = e.db.query

    def query(sql, params=()):
        rows = real(sql, params)
        if "FROM job_items WHERE job_id=? AND status IN" in sql:  # another process takes the item right now
            e.db.execute("UPDATE job_items SET status='RUNNING', attempts=attempts+1 WHERE job_id=?", (jid,))
        return rows

    e.db.query = query
    try:
        e.run_job(jid)
    finally:
        e.db.query = real
    assert e.list_versions(tid) == []


def test_interrupted_render_is_marked_failed_on_next_start(tmp_path):
    from srm.service.engine import STALE_RENDER_S, Engine

    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    v = e.suggest_mastering(tid)["version_no"]
    e.db.execute("UPDATE versions SET status='RENDERING', created_at=? WHERE track_id=?",
                 (time.time() - STALE_RENDER_S - 60, tid))
    e2 = Engine(e.root, create=False)
    assert e2.version(tid, v)["status"] == "FAILED"


# ---------------------------------------------------------------- validator
@pytest.fixture()
def exported(tmp_path):
    e, _ = make_project(tmp_path, n=2)
    aid = e.list_tracks()[0]["album_id"]
    e.set_album_description(aid, "рассвет")
    e.generate_album_cover(aid)
    for t in e.list_tracks():
        render(e, t["id"])
    r = e.export_album(aid, allow_draft=True, confirm=True)
    assert r["technical"] == "PASS"
    return e, aid, Path(r["path"])


def _edit_manifest(pkg, fn):
    p = pkg / "processing_manifest.json"
    m = json.loads(p.read_text("utf-8"))
    fn(m)
    p.write_text(json.dumps(m), "utf-8")


def test_validator_reports_corrupt_mp3_instead_of_crashing(exported):
    from srm.release.validate import validate_package

    _, _, pkg = exported
    sorted((pkg / "MP3_PREVIEW").glob("*.mp3"))[0].write_bytes(b"not an mp3" * 50)
    v = validate_package(pkg)
    assert v["technical"] == "FAIL"
    assert v["tracks"][0]["checks"]["mp3_preview"]["status"] == "FAIL"


def test_validator_reports_missing_metadata_instead_of_crashing(exported):
    from srm.release.validate import validate_package

    _, _, pkg = exported
    (pkg / "album_metadata.json").unlink()
    v = validate_package(pkg)
    assert v["overall"] == "FAILED" and v["global_checks"]["package_readable"]["status"] == "FAIL"


def test_removed_mp3_with_edited_manifest_fails(exported):
    e, aid, pkg = exported
    for f in (pkg / "MP3_PREVIEW").glob("*.mp3"):
        f.unlink()
    _edit_manifest(pkg, lambda m: [t.__setitem__("mp3_preview_required", False) for t in m["tracks"]])
    v = e.validate_release(aid)
    assert v["technical"] == "FAIL"
    assert v["global_checks"]["manifest_matches_export_record"]["status"] == "FAIL"
    assert v["tracks"][0]["checks"]["mp3_preview"]["status"] == "FAIL"  # also caught without the DB record


def test_removed_cover_with_edited_manifest_fails(exported):
    e, aid, pkg = exported
    shutil.rmtree(pkg / "ARTWORK")
    _edit_manifest(pkg, lambda m: m.pop("artwork"))
    assert e.validate_release(aid)["technical"] == "FAIL"


def test_stray_files_are_reported(exported):
    from srm.release.validate import validate_package

    _, _, pkg = exported
    (pkg / "notes.txt").write_text("my notes", "utf-8")
    v = validate_package(pkg)
    assert v["technical"] == "PASS" and v["global_checks"]["no_unexpected_files"]["status"] == "WARN"
    shutil.copyfile(sorted((pkg / "MP3_PREVIEW").glob("*.mp3"))[0], pkg / "MP3_PREVIEW" / "99_stray.mp3")
    v = validate_package(pkg)
    assert v["technical"] == "FAIL" and v["global_checks"]["no_unexpected_files"]["status"] == "FAIL"


# ---------------------------------------------------------------- DSP / repair / proposal
def test_limiter_bound_applies_to_sources_that_are_already_hot():
    from srm.dsp.proposal import propose

    meas = {"loudness": {"integrated_lufs": -12.0, "lra_lu": 5}, "peaks": {"true_peak_dbtp": 1.5},
            "dynamics": {}, "spectral": {}, "style": {}, "defects": {"candidates": []}}
    p = propose(meas, ceiling_dbtp=-1.0)
    bound = p["preset_info"]["max_limiter_gr_db"]
    lim = next(a for a in p["actions"] if a["stage"] == "limiter")
    assert lim["evidence"]["est_peak_reduction_db"] <= bound + 1e-9
    assert p["chain"]["gain"] == {"enabled": True, "gain_db": -0.5}
    keep = propose(meas, ceiling_dbtp=-1.0, loudness_policy="preserve")
    assert keep["chain"]["gain"]["enabled"] is False  # explicit "preserve" is respected, with a warning
    assert "WARNING" in next(a for a in keep["actions"] if a["stage"] == "gain")["note"]


def test_mono_source_repairs_cover_both_output_channels(tmp_path):
    from srm.dsp.repair import apply_edit
    from srm.service.engine import Engine

    lib = tmp_path / "music" / "01 - Mono"
    lib.mkdir(parents=True)
    x = _tone(channels=1)
    x[FS + 100, 0] += 0.6
    sf.write(lib / "01 - Click.wav", x, FS, subtype="PCM_24")
    e = Engine(tmp_path / "project")
    e.add_library_root(lib.parent)
    e.scan()
    tid = e.list_tracks()[0]["id"]
    plan = e.propose_repairs(tid)
    clicks = [ed for ed in plan["edits"] if ed["type"] == "click_pop"]
    assert clicks and all(ed["channel"] is None for ed in plan["edits"])
    stereo, fs = e._source_float(e.track(tid))
    y = apply_edit(stereo, fs, dict(clicks[0]))
    assert np.array_equal(y[:, 0], y[:, 1]) and abs(y[FS + 100, 0]) < 0.5


# ---------------------------------------------------------------- core
def test_transaction_releases_lock_when_begin_fails(tmp_path):
    import sqlite3

    from srm.core.db import DB

    d = DB(tmp_path / "x.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        with d.transaction():
            with d.transaction():
                pass
    done = threading.Event()
    threading.Thread(target=lambda: (d.query("SELECT 1"), done.set()), daemon=True).start()
    assert done.wait(5), "another thread is locked out of the database"


def test_has_analysis_requires_current_analysis_version(tmp_path):
    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    e.analyze_track(tid)
    assert e.list_tracks()[0]["has_analysis"]
    e.db.execute("UPDATE analyses SET analysis_version='analysis-0.0'")
    assert not e.list_tracks()[0]["has_analysis"] and e.get_analysis(tid) is None


def test_numeric_flags_are_stored_as_booleans(tmp_path):
    from srm.release import metadata as M

    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    rel = e.set_track_metadata(tid, {"user_confirms_commercial_rights": 1, "downloaded_while_on_paid_plan": 1,
                                     "year": 2026})
    assert rel["user_confirms_commercial_rights"] is True and rel["year"] == 2026
    assert not [u for u in M.unresolved(M.track_record(e.track(tid), 1)) if "rights" in u]


def test_same_folder_name_under_two_roots_gives_two_albums(tmp_path):
    e, _ = make_project(tmp_path, n=1, root="rootA")
    lib_b = tmp_path / "rootB" / "01 - Album"
    lib_b.mkdir(parents=True)
    sf.write(lib_b / "01 - Other.wav", _tone(freq=500), FS, subtype="PCM_24")
    e.add_library_root(lib_b.parent)
    e.scan(str(lib_b.parent))
    albums = e.list_albums()
    assert len(albums) == 2 and all(a["tracks"] == 1 for a in albums)
    assert len({a["folder_hint"] for a in albums}) == 2


def test_renamed_album_keeps_receiving_files_from_its_folder(tmp_path):
    e, lib = make_project(tmp_path, n=1)
    aid = e.list_albums()[0]["id"]
    e.rename_album(aid, "My Release")
    sf.write(lib / "02 - New.wav", _tone(freq=700), FS, subtype="PCM_24")
    e.scan()
    albums = e.list_albums()
    assert len(albums) == 1 and albums[0]["tracks"] == 2


def test_edited_text_file_is_reread_on_rescan(tmp_path):
    e, lib = make_project(tmp_path, n=1)
    txt = lib / "01 - Song 1.txt"
    txt.write_text("Style: ambient jazz\n", "utf-8")
    e.scan()
    tid = e.list_tracks()[0]["id"]
    assert e.track(tid)["text"]["parsed"]["style_prompt"] == "ambient jazz"
    txt.write_text("Style: liquid drum and bass\n", "utf-8")
    e.scan()
    assert e.track(tid)["text"]["parsed"]["style_prompt"] == "liquid drum and bass"


def test_verify_sources_reports_missing_files(tmp_path, capsys):
    from srm.cli.main import cmd_verify_sources

    e, lib = make_project(tmp_path, n=2)
    next(lib.glob("01*.wav")).unlink()
    assert cmd_verify_sources(e, None) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "FAIL" and len(out["missing"]) == 1


# ---------------------------------------------------------------- MCP secrets
def test_capability_token_is_redacted_from_logs():
    pytest.importorskip("uvicorn")
    import uvicorn

    from srm.mcp_server.server import install_token_redaction

    token = "s3cr3t-token-value"
    flt = install_token_redaction(token)
    try:
        uvicorn.Config(lambda scope, receive, send: None, log_level="info")  # uvicorn configures logging here
        logger = logging.getLogger("uvicorn.access")
        assert flt in logger.filters, "uvicorn's logging setup dropped the redaction filter"
        rec = logger.makeRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                ("127.0.0.1:1", "POST", f"/s/{token}/mcp", "1.1", 200), None)
        assert logger.filter(rec)
        assert token not in rec.getMessage() and "<redacted>" in rec.getMessage()
    finally:
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "mcp"):
            logging.getLogger(name).removeFilter(flt)


# ---------------------------------------------------------------- GUI
def _wait(app, predicate, timeout=90):
    until = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        time.sleep(0.02)
        assert time.monotonic() < until, "GUI action timed out"
    app.processEvents()


@pytest.fixture()
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_project_switch_drops_the_prepared_audition(tmp_path, qt_app):
    from srm.gui.studio import StudioWindow

    ea, _ = make_project(tmp_path, n=1, root="musicA", project="projectA")
    eb, _ = make_project(tmp_path, n=1, root="musicB", project="projectB")
    tid_b = eb.list_tracks()[0]["id"]
    vb = render(eb, tid_b)  # same track id and version number as in project A, never auditioned
    w = StudioWindow(str(ea.root))
    w.show()
    w.track_table.selectRow(0)
    qt_app.processEvents()
    w.prepare_listening()
    _wait(qt_app, lambda: w._preview_identity is not None)
    assert w._preview_identity == (tid_b, vb)
    w.open_project(str(eb.root))
    qt_app.processEvents()
    assert w._preview_identity is None and not w.accept_button.isEnabled()
    assert w.player.combo.count() == 0 and not w.player.clips
    w.accept_listening()
    v = w.engine.version(tid_b, vb)
    assert v["status"] == "RENDERED" and v["approvals"] == []
    w.close()


def test_closing_during_work_waits_for_the_worker(tmp_path, qt_app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from srm.gui.studio import StudioWindow

    e, _ = make_project(tmp_path, n=1)
    tid = e.list_tracks()[0]["id"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: QMessageBox.Yes)
    w = StudioWindow(str(e.root))
    w.show()
    w.track_table.selectRow(0)
    qt_app.processEvents()
    w.prepare_listening()
    _wait(qt_app, lambda: w._busy and any(v["status"] == "RENDERING" for v in w.engine.list_versions(tid)))
    w.close()
    assert w.isVisible(), "the window must stay until the worker has finished"
    _wait(qt_app, lambda: not w.isVisible())
    assert [v["status"] for v in e.list_versions(tid)] == ["RENDERED"]


def test_second_job_is_refused_while_one_is_running(tmp_path, qt_app):
    from srm.gui.studio import StudioWindow

    e, _ = make_project(tmp_path, n=2)
    w = StudioWindow(str(e.root))
    w.show()
    qt_app.processEvents()
    w.prepare_album()
    first = w.current_job
    assert w._job_running
    w.analyze_selected_album()
    w.resume_job()
    assert w.current_job == first and len(w.engine.db.query("SELECT id FROM jobs")) == 1
    _wait(qt_app, lambda: not w._job_running)
    assert not w._album_job_active
    assert all(len(w.engine.list_versions(t["id"])) == 1 for t in w.engine.list_tracks())
    w.close()
