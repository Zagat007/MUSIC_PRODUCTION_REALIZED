import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from conftest import ROOT, track_by_name
from srm.core.paths import sha256_file
from srm.ingest.probe import probe


def test_scan_summary(scanned):
    e, s = scanned
    assert s["audio_files"] == 17 and s["probe_failed"] == 2 and s["errors"] == []
    names = {a["name"] for a in e.list_albums()}
    assert {"Album A", "Альбом Б", "Edge Cases"} <= names


def test_codec_identification(scanned):
    e, _ = scanned
    aac = track_by_name(e, "01 Liquid Night")
    alac = track_by_name(e, "Песня №1")
    assert aac["codec"] == "aac" and aac["is_lossy"] == 1
    assert alac["codec"] == "alac" and alac["is_lossy"] == 0
    assert any("LOSSY SOURCE" in w for w in e.track(aac["id"])["probe"]["warnings"])
    assert track_by_name(e, "preview.mp3")["is_lossy"] == 1


def test_text_matches_and_duplicates(scanned):
    e, _ = scanned
    assert track_by_name(e, "01 Liquid Night")["text_match_status"] == "MATCHED_EXACT"
    t = e.track(track_by_name(e, "Песня №1")["id"])
    assert t["text"]["encoding"] == "cp1251" and "эмбиент" in t["text"]["parsed"]["style_prompt"]
    a, b = track_by_name(e, "02 Rain Code.m4a"), track_by_name(e, "02 Rain Code (1)")
    assert a["dup_group"] and a["dup_group"] == b["dup_group"]


def test_analysis_job_resume_and_failures(scanned):
    e, _ = scanned
    ids = [t["id"] for t in e.list_tracks()]
    jid = e.create_job("analyze", ids, ["analyze"])
    st = e.run_job(jid, stop_after=5)  # controlled interruption
    assert st["status"] == "PAUSED" and st["done"] + st["failed"] == 5
    st = e.run_job(jid)  # resume
    assert st["status"] == "COMPLETED_WITH_ERRORS"
    assert st["done"] == 15 and st["failed"] == 2  # corrupt.wav + truncated.m4a fail cleanly
    failed_paths = {e.track(f["track_id"])["rel_path"] for f in st["failures"]}
    assert {str(Path("Edge Cases/corrupt.wav")), str(Path("Edge Cases/truncated.m4a"))} == failed_paths
    # idempotency: a second run does not redo finished items
    st2 = e.run_job(jid)
    assert st2["done"] == 15


def test_analysis_content(scanned):
    e, _ = scanned
    sil = e.analyze_track(track_by_name(e, "silence.wav")["id"])
    assert sil["qc"]["technical_status"] == "FAILED"
    clk = e.analyze_track(track_by_name(e, "clicks.wav")["id"])
    times = sorted({round(c["start_s"], 1) for c in clk["measurements"]["defects"]["candidates"] if c["type"] == "click_pop"})
    # Clicks at 3.0 / 7.5 s are in the clear; the one at 12.25 s lies on a hi-hat burst and is
    # deliberately not flagged (masked-click miss is documented in LIMITATIONS.md). No false positives.
    assert {3.0, 7.5} <= set(times) and set(times) <= {3.0, 7.5, 12.2, 12.3}
    song = e.analyze_track(track_by_name(e, "Песня №1")["id"])
    assert not [c for c in song["measurements"]["defects"]["candidates"] if c["type"] == "click_pop"]
    ph = e.analyze_track(track_by_name(e, "phase_inverted")["id"])
    assert any(c["type"] == "phase_mono_cancellation" for c in ph["measurements"]["defects"]["candidates"])
    isp = e.analyze_track(track_by_name(e, "intersample_over")["id"])
    assert isp["measurements"]["peaks"]["true_peak_dbtp"] > 2.5
    assert isp["measurements"]["peaks"]["sample_peak_dbfs"] < 0.01
    dc = e.analyze_track(track_by_name(e, "dc_offset")["id"])
    assert any(c["type"] == "dc_offset" for c in dc["measurements"]["defects"]["candidates"])
    cl = e.analyze_track(track_by_name(e, "clipped_sine")["id"])
    assert any(c["type"] == "hard_clipping" for c in cl["measurements"]["defects"]["candidates"])
    liq = e.analyze_track(track_by_name(e, "01 Liquid Night")["id"])
    st = liq["measurements"]["style"]
    assert st["confidence"] == "LOW" and "liquid_dnb" in st["prompt_declared_styles"]
    assert Path(e._report_dir(e.track(track_by_name(e, "Песня №1")["id"])) / "source_analysis.html").exists()


def _probe_out(path):
    pr = probe(path)
    return pr.codec, pr.sample_rate, pr.bits_per_sample, pr.channels, pr.duration_s


def test_convert_alac_bit_exact(scanned):
    e, _ = scanned
    t = track_by_name(e, "Песня №1")
    r = e.convert_track(t["id"])
    assert r["processing"]["plan"]["bit_exact"] and r["processing"]["bit_exact_verified"] is True
    codec, sr, bits, ch, dur = _probe_out(r["output"]["path"])
    assert (codec, sr, bits, ch) == ("pcm_s24le", 48000, 24, 2)
    assert r["source_unchanged"]


def test_convert_aac_transcode_warns(scanned):
    e, _ = scanned
    t = track_by_name(e, "01 Liquid Night")
    r = e.convert_track(t["id"])
    p = r["processing"]
    assert not p["plan"]["bit_exact"] and p["dither"] == "tpdf"
    assert any("not restoration" in n for n in p["plan"]["notes"])
    codec, sr, bits, ch, dur = _probe_out(r["output"]["path"])
    assert (codec, sr, bits, ch) == ("pcm_s24le", 48000, 24, 2)
    assert abs(dur - t["duration_s"]) < 0.05
    # Real bytes: header must agree with soundfile's independent parse
    info = sf.info(r["output"]["path"])
    assert info.subtype == "PCM_24" and info.samplerate == 48000 and info.channels == 2
    assert abs(r["comparison"]["loudness.integrated_lufs"]["delta"]) < 0.05


def test_convert_resample_and_mono(scanned):
    e, _ = scanned
    hi = e.convert_track(track_by_name(e, "hires_96k")["id"])
    assert hi["processing"]["plan"]["resample"] and "soxr" in hi["processing"]["resampler"]
    assert _probe_out(hi["output"]["path"])[1] == 48000
    mono = e.convert_track(track_by_name(e, "mono.wav")["id"])
    assert mono["processing"]["plan"]["mono_to_dual_mono"]
    x, _ = sf.read(mono["output"]["path"])
    assert x.shape[1] == 2 and np.array_equal(x[:, 0], x[:, 1])
    v = e.list_versions(track_by_name(e, "mono.wav")["id"])
    assert v[0]["version_no"] == 1 and v[0]["status"] == "RENDERED"
    again = e.convert_track(track_by_name(e, "mono.wav")["id"])  # new immutable version, no overwrite
    assert again["version_no"] == 2 and again["output"]["path"] != mono["output"]["path"]


def test_isp_overs_policy(scanned):
    e, _ = scanned
    r = e.convert_track(track_by_name(e, "intersample_over")["id"])
    assert r["processing"]["quantize"]["clipped_samples"] == 0


def test_cli_end_to_end(tmp_path, fixture_lib):
    proj = tmp_path / "cli_proj"
    py = sys.executable

    def srm(*args):
        p = subprocess.run([py, "-m", "srm.cli.main", *args], cwd=ROOT, capture_output=True, timeout=900)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    rc, out, err = srm("scan", str(fixture_lib / "Альбом Б"), "--project", str(proj))
    assert rc == 0, err
    assert json.loads(out)["audio_files"] == 3
    rc, out, err = srm("analyze", "--project", str(proj))
    assert rc == 0, err
    rc, out, err = srm("convert", "--project", str(proj), "--album", "Unsorted")
    assert rc == 0, err
    assert json.loads(out)["done"] == 6
    rc, out, err = srm("tracks", "--project", str(proj))
    assert rc == 0 and "Тихая вода" in out
    rc, out, err = srm("verify-sources", "--project", str(proj))
    assert rc == 0 and json.loads(out)["status"] == "PASS"


def test_sources_untouched(scanned, source_hashes):
    # Runs last in this module: no input byte changed after scan/analysis/convert/failures
    for p, h in source_hashes.items():
        assert sha256_file(p) == h, p
