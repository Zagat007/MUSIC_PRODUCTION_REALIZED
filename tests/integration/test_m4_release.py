import json
from pathlib import Path

import pytest

from srm.release.metadata import MetadataError
from srm.service.engine import Engine, EngineError


@pytest.fixture(scope="module")
def eng(tmp_path_factory, fixture_lib):
    e = Engine(tmp_path_factory.mktemp("m4") / "proj")
    e.add_library_root(fixture_lib)
    e.scan()
    return e


def _tid(e, frag):
    return [t for t in e.list_tracks() if frag in t["rel_path"]][0]["id"]


def test_album_assignment_never_moves_files(eng, fixture_lib):
    a = eng.create_album("Сборник 2026")
    t1, t2 = _tid(eng, "Песня №1"), _tid(eng, "01 Liquid Night")
    src = eng.track(t1)["path"]
    eng.assign_track(t1, "Сборник 2026")
    eng.assign_track(t2, "Сборник 2026", position=1)
    tracks = eng.list_tracks("Сборник 2026")
    assert [t["id"] for t in tracks] == [t2, t1] and Path(src).exists()
    eng.reorder_album("Сборник 2026", [t1, t2])
    assert [t["id"] for t in eng.list_tracks(a["id"])] == [t1, t2]
    with pytest.raises(ValueError):
        eng.reorder_album("Сборник 2026", [t1])


def test_text_match_decision_survives_rescan(eng):
    t = _tid(eng, "01 Liquid Night")
    eng.decide_text_match(t, accept=False)
    eng.scan()
    assert eng.track(t)["text_match_status"] == "REJECTED"
    eng.decide_text_match(t, accept=True, text_path=str(Path(eng.track(t)["path"]).with_suffix(".txt")))
    eng.scan()
    assert eng.track(t)["text_match_status"] == "CONFIRMED"


def test_metadata_validation(eng):
    t = _tid(eng, "Песня №1")
    with pytest.raises(MetadataError):
        eng.set_track_metadata(t, {"isrc": "12345"})
    with pytest.raises(MetadataError):
        eng.set_track_metadata(t, {"made_up_field": 1})
    m = eng.set_track_metadata(t, {"isrc": "ru-a1b-26-00001", "artist": "Тест Артист"})
    assert m["isrc"] == "RUA1B2600001"


def test_export_gates_and_draft(eng):
    album = "Сборник 2026"
    for tid in [t["id"] for t in eng.list_tracks(album)]:
        eng.analyze_track(tid)
        p = eng.suggest_mastering(tid)
        eng.render_version(tid, p["version_no"], codec_preview=False)
    with pytest.raises(EngineError):
        eng.export_album(album)  # no confirmation
    with pytest.raises(EngineError):
        eng.export_album(album, confirm=True)  # unapproved renders, no --allow-draft
    r = eng.export_album(album, confirm=True, allow_draft=True, include_flac=True)
    assert r["status"] == "DRAFT_NOT_RELEASE_READY" and r["technical"] == "PASS"
    pkg = Path(r["path"])
    assert pkg.name == "v001" and not list(pkg.parent.glob(".building_*"))
    for f in ("album_metadata.csv", "album_metadata.json", "rights_ai_disclosure_checklist.json",
              "release_validation.json", "album_qa.html", "processing_manifest.json", "cover_art_checklist.json",
              "UNRESOLVED_WARNINGS.txt"):
        assert (pkg / f).exists(), f
    wavs = sorted(p.name for p in (pkg / "WAV_MASTER").glob("*.wav"))
    assert wavs[0].startswith("01_Песня №1") and wavs[1].startswith("02_Liquid Night")  # album order honoured; number parsed from filename
    assert len(list((pkg / "MP3_PREVIEW").glob("*.mp3"))) == 2
    assert len(list((pkg / "QA").glob("*.html"))) == 2
    meta = json.loads((pkg / "album_metadata.json").read_text("utf-8"))
    rec = meta["tracks"][0]
    assert rec["songwriters"] == "UNKNOWN" and rec["ai_generated_vocals"] == "UNKNOWN"
    assert rec["isrc"] == "RUA1B2600001" and meta["album"]["upc"] == "UNKNOWN"
    unresolved = (pkg / "UNRESOLVED_WARNINGS.txt").read_text("utf-8")
    assert "UNAPPROVED" in unresolved and "lossy" in unresolved and "listening sign-off" in unresolved
    v = r["validation"]
    t0 = v["tracks"][0]["checks"]
    for k in ("codec", "bit_depth", "sample_rate", "channels", "checksum_matches_manifest", "duration_vs_source",
              "true_peak_vs_policy", "flac_lossless_vs_wav", "loudness_matches_render"):
        assert t0[k]["status"] == "PASS", (k, t0[k])
    assert t0["version_approved"]["status"] == "PENDING"


def test_full_gates_recorded(eng):
    album = "Сборник 2026"
    for t in eng.list_tracks(album):
        vno = eng.list_versions(t["id"])[-1]["version_no"]
        eng.approve(t["id"], vno, note="approved after A-B")
        eng.listening_signoff(t["id"], vno, note="listened on monitors")
        eng.set_track_metadata(t["id"], {"artist": "Тест Артист", "songwriters": "User Name",
                                         "ai_generated_vocals": True, "ai_generated_music": True,
                                         "ai_generated_lyrics": False, "downloaded_while_on_paid_plan": True,
                                         "user_confirms_commercial_rights": True})
    eng.set_album_metadata(album, {"album_title": album, "album_artist": "Тест Артист"})
    r = eng.export_album(album, confirm=True)
    v = r["validation"]
    # Lossy sources still leave an unresolved note -> honest DRAFT, never auto-"approved"
    assert v["technical"] == "PASS" and v["human_gates"] == "RECORDED"
    assert r["status"] == "DRAFT_NOT_RELEASE_READY"
    assert any("lossy" in u for u in r["unresolved"])
    assert Path(r["path"]).name == "v002"


def test_validator_detects_tampering(eng):
    from srm.release.validate import validate_package

    pkg = Path(eng.validate_release("Сборник 2026")["package"])
    wav = sorted((pkg / "WAV_MASTER").glob("*.wav"))[0]
    data = bytearray(wav.read_bytes())
    data[-100] ^= 0xFF
    wav.write_bytes(bytes(data))  # tamper with a generated output (not a source)
    v = validate_package(pkg)
    assert v["technical"] == "FAIL"
    assert v["tracks"][0]["checks"]["checksum_matches_manifest"]["status"] == "FAIL"


def test_compat_wav_profile(eng):
    album = "Сборник 2026"
    r = eng.export_album(album, confirm=True, profile="compat_wav", include_mp3=False)
    t0 = r["validation"]["tracks"][0]["checks"]
    assert t0["codec"]["value"] == "pcm_s16le" and t0["sample_rate"]["value"] == 44100
    assert t0["true_peak_vs_policy"]["status"] in ("PASS", "FAIL")  # measured, not assumed
