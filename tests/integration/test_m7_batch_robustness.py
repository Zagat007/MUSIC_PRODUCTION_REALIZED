"""M7: 100-file batch with controlled interruption/resume, and graceful-failure cases."""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from srm.core.paths import sha256_file
from srm.service.engine import Engine, EngineError


@pytest.mark.slow
def test_batch_100_interrupt_resume(tmp_path):
    sys.path.insert(0, str(ROOT / "tests" / "audio_fixtures"))
    from make_fixtures import make_many

    lib = make_many(tmp_path, 100, seconds=6.0)
    before = {str(p): sha256_file(p) for p in lib.rglob("*") if p.is_file()}
    assert len(before) == 100
    e = Engine(tmp_path / "proj")
    e.add_library_root(lib)
    s = e.scan()
    assert s["audio_files"] == 100 and s["errors"] == [] and s["lossy"] == 100
    assert len(e.list_albums()) == 4
    ids = [t["id"] for t in e.list_tracks()]
    jid = e.create_job("analyze", ids, ["analyze"], idempotency_key="batch-100")
    st = e.run_job(jid, workers=4, stop_after=37)  # controlled interruption
    assert st["status"] == "PAUSED" and st["done"] == 37
    # simulate a crash: one item left RUNNING
    e.db.execute("UPDATE job_items SET status='RUNNING' WHERE id=(SELECT id FROM job_items WHERE job_id=? AND "
                 "status='PENDING' LIMIT 1)", (jid,))
    # a fresh Engine instance (as after restart) resumes the same job; idempotency key returns same job
    e2 = Engine(tmp_path / "proj")
    assert e2.create_job("analyze", ids, ["analyze"], idempotency_key="batch-100") == jid
    st = e2.run_job(jid, workers=4)
    assert st["status"] == "COMPLETED" and st["done"] == 100 and st["failed"] == 0
    # no duplicate analyses (minimal repeated DSP)
    n = e2.db.one("SELECT COUNT(*) c FROM analyses WHERE kind='source'")["c"]
    assert n == 100
    # rescan is incremental: nothing rehashed
    s2 = e2.scan()
    assert s2["unchanged"] == 100 and s2["new"] == 0
    after = {str(p): sha256_file(p) for p in lib.rglob("*") if p.is_file()}
    assert before == after


def test_cancel_marks_job_cancelled(tmp_path, fixture_lib):
    e = Engine(tmp_path / "p")
    e.add_library_root(fixture_lib / "Album A")
    e.scan()
    ids = [t["id"] for t in e.list_tracks()]
    jid = e.create_job("analyze", ids, ["analyze"])
    e._cancel_flags[jid] = __import__("threading").Event()
    orig = e._run_stage

    def stage_then_cancel(stage, tid, params):
        r = orig(stage, tid, params)
        e.cancel_job(jid)
        return r

    e._run_stage = stage_then_cancel
    # run_job clears the flag at start, so cancel happens after the first item
    st = e.run_job(jid, workers=1)
    assert st["status"] == "CANCELLED" and st["done"] >= 1 and st["pending"] >= 1
    e._run_stage = orig
    st = e.run_job(jid, workers=1)
    assert st["status"] == "COMPLETED"


def test_missing_ffmpeg_is_reported(tmp_path, fixture_lib):
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent)  # python only, no ffmpeg
    env.pop("SRM_FFMPEG", None)
    env.pop("SRM_FFPROBE", None)
    proj = tmp_path / "p"
    p = subprocess.run([sys.executable, "-m", "srm.cli.main", "scan", str(fixture_lib / "Album A"), "--project",
                        str(proj)], cwd=ROOT, env=env, capture_output=True, timeout=300)
    err = p.stderr.decode("utf-8", "replace")
    out = p.stdout.decode("utf-8", "replace")
    # scan continues per file but each probe fails with a clear ToolMissingError message
    assert "FFmpeg/ffprobe not found" in (err + out)
    p2 = subprocess.run([sys.executable, "-m", "srm.cli.main", "health", "--project", str(proj)], cwd=ROOT, env=env,
                        capture_output=True, timeout=120)
    assert '"status": "UNAVAILABLE"' in p2.stdout.decode("utf-8")


def test_read_only_sources_and_bad_encoding(tmp_path):
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "ro_lib" / "Альбом"
    lib.mkdir(parents=True)
    x = 0.3 * np.sin(2 * np.pi * 440 * np.arange(48000 * 3) / 48000)
    wav = lib / "Трек.wav"
    sf.write(wav, np.stack([x, x], 1), 48000, subtype="PCM_24")
    bad = lib / "Трек.txt"
    bad.write_bytes(b"Style: \xff\xfe\x00 broken \x81\x8d bytes\n")
    os.chmod(wav, stat.S_IREAD)
    os.chmod(bad, stat.S_IREAD)
    try:
        e = Engine(tmp_path / "proj")
        e.add_library_root(tmp_path / "ro_lib")
        s = e.scan()
        assert s["errors"] == []
        t = e.list_tracks()[0]
        a = e.analyze_track(t["id"])
        assert a["qc"]["technical_status"] in ("TECHNICAL_PASS", "NEEDS_LISTENING_REVIEW")
        r = e.convert_track(t["id"])
        assert r["processing"]["bit_exact_verified"] is True
        tr = e.track(t["id"])
        assert tr["text"] is not None and tr["text"]["encoding"] in ("cp1251", "utf-8-replace")
    finally:
        os.chmod(wav, stat.S_IREAD | stat.S_IWRITE)
        os.chmod(bad, stat.S_IREAD | stat.S_IWRITE)


def test_low_disk_space_refuses_render(tmp_path, fixture_lib, monkeypatch):
    import srm.service.engine as eng_mod

    e = Engine(tmp_path / "p")
    e.add_library_root(fixture_lib / "Album A")
    e.scan()
    tid = e.list_tracks()[0]["id"]
    monkeypatch.setattr(eng_mod, "free_disk_bytes", lambda p: 10 * 1024 * 1024)
    with pytest.raises(EngineError, match="low disk space"):
        e.convert_track(tid)
    assert e.list_versions(tid) == []  # nothing half-written was registered


def test_core_has_no_network_code():
    """Offline guarantee by audit: only the optional MCP server may import network modules."""
    pat = re.compile(r"^\s*(import|from)\s+(socket|urllib|http\b|httpx|requests|aiohttp|ftplib|smtplib)", re.M)
    offenders = []
    for p in (ROOT / "srm").rglob("*.py"):
        if "mcp_server" in p.parts:
            continue
        if pat.search(p.read_text("utf-8")):
            offenders.append(str(p))
    assert offenders == []
