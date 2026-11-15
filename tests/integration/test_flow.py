"""Album pipeline (stages + AUTO) and the step-by-step main window."""
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from srm.core.paths import sha256_file

FS = 48000


def tone(sec=4.0, freq=220.0, amp=0.25):
    t = np.arange(int(sec * FS)) / FS
    x = amp * np.sin(2 * np.pi * freq * t) * np.minimum(1, t / 0.05) * np.minimum(1, (sec - t) / 0.05)
    return np.column_stack([x, 0.9 * x])


def make_library(tmp_path) -> Path:
    """Two real albums, one service folder and one postponed folder that must be ignored."""
    root = tmp_path / "НА РЕЛИЗ"
    a = root / "01 — ПЕРВЫЙ СВЕТ"
    a.mkdir(parents=True)
    sf.write(a / "01 — Заря.wav", tone(freq=220), FS, subtype="PCM_24")
    sf.write(a / "02 — Ночь.wav", tone(freq=330), FS, subtype="PCM_24")
    clipped = np.clip(tone(freq=330, amp=0.9) * 1.6, -0.98, 0.98)  # a visibly damaged second version
    sf.write(a / "02 — Ночь (2).wav", clipped, FS, subtype="PCM_24")
    click = tone(freq=440)
    click[FS + 100, :] += 0.6  # one isolated click
    sf.write(a / "03 — Щелчок.wav", click, FS, subtype="PCM_24")
    (a / "КОНЦЕПЦИЯ.txt").write_text("АЛЬБОМ: ПЕРВЫЙ СВЕТ\nСТИЛЬ: ambient jazz\n\nКОНЦЕПЦИЯ\nРок-н-ролл тут ни при чём.\n", "utf-8")
    Image.new("RGB", (800, 600), (180, 140, 60)).save(a / "COVER.png")
    b = root / "02 — ВТОРОЙ"
    b.mkdir()
    sf.write(b / "01 — Один.wav", tone(freq=260), FS, subtype="PCM_16")
    sf.write(b / "02 — Два.wav", tone(freq=290), FS, subtype="PCM_16")
    for skipped in ("_ВХОДЯЩИЕ/01 — ЧЕРНОВИК", "renders/00001_x/v001"):
        d = root / skipped
        d.mkdir(parents=True)
        sf.write(d / "x.wav", tone(sec=2), FS, subtype="PCM_16")
    return root


@pytest.fixture()
def engine(tmp_path):
    from srm.service.engine import Engine
    from srm.service.pipeline import SERVICE_DIRS

    root = make_library(tmp_path)
    e = Engine(tmp_path / "project")
    e.name_root_album_by_folder = True
    e.add_library_root(root, exclude=SERVICE_DIRS)
    e.scan()
    return e, root


def test_folder_with_albums_opens_all_and_skips_service_folders(engine):
    e, _ = engine
    albums = e.albums_overview()
    assert [(a["title"], a["tracks"]) for a in albums] == [("01 — ПЕРВЫЙ СВЕТ", 4), ("02 — ВТОРОЙ", 2)]


def test_album_folder_opens_as_one_album(tmp_path):
    from srm.service.engine import Engine
    from srm.service.pipeline import SERVICE_DIRS

    root = make_library(tmp_path)
    e = Engine(tmp_path / "p2")
    e.name_root_album_by_folder = True
    e.add_library_root(root / "02 — ВТОРОЙ", exclude=SERVICE_DIRS)
    e.scan()
    assert [(a["title"], a["tracks"]) for a in e.albums_overview()] == [("02 — ВТОРОЙ", 2)]


def test_style_comes_from_style_line_album_name_or_choice(engine):
    from srm.service.pipeline import style_from_text

    e, _ = engine
    first, second = (a["id"] for a in e.albums_overview())
    assert e.album_style(first) == {"preset": "ambient_jazz", "source": "из файла КОНЦЕПЦИЯ.txt"}
    assert e.album_style(second)["preset"] is None  # nothing declared -> neutral preset later
    e.set_album_style(second, "rock")
    assert e.album_style(second) == {"preset": "rock", "source": "выбран вами"}
    e.set_album_style(second, None)
    assert e.album_style(second)["preset"] is None
    assert style_from_text("liquid drum and bass") == "liquid_dnb"
    assert style_from_text("rapid popular wrap") is None      # whole words only
    assert style_from_text("NEW HOUSE JAZZ") is None           # two styles named: undecided


def test_auto_runs_every_stage_and_builds_the_release(engine):
    e, root = engine
    before = {p: sha256_file(p) for p in root.rglob("*.wav")}
    aid = e.albums_overview()[0]["id"]
    seen = []
    results = e.run_auto(aid, progress=lambda *a: seen.append(a[0]))
    assert [r["stage"] for r in results] == ["analyze", "versions", "repair", "master", "release"]
    assert not any(r["failed"] for r in results), [r["failed"] for r in results]
    st = e.pipeline_status(aid)
    by_file = {t["file"]: t for t in st["tracks"]}
    # versions: the clipped duplicate is kept out of the release, the clean one stays; nothing is moved
    assert by_file["02 — Ночь (2).wav"]["excluded"] and not by_file["02 — Ночь.wav"]["excluded"]
    assert (root / "01 — ПЕРВЫЙ СВЕТ" / "02 — Ночь (2).wav").is_file()
    # repair + master ran for the three release tracks only
    active = [t for t in st["tracks"] if not t["excluded"]]
    assert len(active) == 3 and all(t["analyzed"] and t["repair"] is not None and t["mastered"] for t in active)
    assert by_file["02 — Ночь (2).wav"]["mastered"] is False
    assert by_file["03 — Щелчок.wav"]["repair"]["applied"] >= 1
    # the declared style picked the preset
    v = e.version(by_file["01 — Заря.wav"]["id"], 1)
    assert v["config"]["proposal"]["preset"] == "ambient_jazz" and by_file["01 — Заря.wav"]["style"] == "ambient_jazz"
    # release: three WAV + three MP3, cover taken from COVER.png, technically valid, still a draft
    rel = results[-1]["release"]
    pkg = Path(rel["path"])
    assert rel["technical"] == "PASS" and rel["status"] == "DRAFT_NOT_RELEASE_READY"
    assert len(list((pkg / "WAV_MASTER").glob("*.wav"))) == 3 and len(list((pkg / "MP3_PREVIEW").glob("*.mp3"))) == 3
    assert (pkg / "ARTWORK" / "cover.jpg").is_file() and any("COVER.png" in m for m in results[-1]["messages"])
    assert st["stages"]["release"]["done"] == 1 and st["stages"]["versions"] == {"done": 1, "total": 1}
    # sources untouched, the other album untouched
    assert {p: sha256_file(p) for p in root.rglob("*.wav")} == before
    assert e.pipeline_status(e.albums_overview()[1]["id"])["stages"]["analyze"]["done"] == 0
    # a second run repeats nothing but the package
    again = e.run_auto(aid)
    assert [(r["stage"], r["done"]) for r in again[:4] if r["stage"] != "versions"] == [
        ("analyze", 0), ("repair", 0), ("master", 0)]
    # after the user's own approval the package is final
    assert e.approve_album(aid) == 3
    final = e.run_stage(aid, "release")["release"]
    assert final["status"] != "DRAFT_NOT_RELEASE_READY" or final["unresolved"] > 0


def test_repair_leaves_rhythm_alone(tmp_path):
    from srm.service.engine import Engine

    lib = tmp_path / "music" / "01 — Бит"
    lib.mkdir(parents=True)
    x = tone(sec=6.0)
    for k in range(30):  # thirty sharp hits: rhythm, not damage
        x[FS // 2 + k * 8000, :] += 0.6
    sf.write(lib / "01 — Барабаны.wav", x, FS, subtype="PCM_24")
    e = Engine(tmp_path / "project")
    e.add_library_root(lib.parent)
    e.scan()
    tid = e.list_tracks()[0]["id"]
    out = e.auto_repair_track(tid)
    assert out["applied"] == 0
    assert e.approved_repair_plan(tid)["edits"] == []
    assert e.auto_repair_track(tid)["skipped"] is True  # already decided for this file


def test_manual_exclusion_and_export_skip(engine):
    e, _ = engine
    aid = e.albums_overview()[1]["id"]
    t1, t2 = (t["id"] for t in e.list_tracks(aid))
    e.set_track_excluded(t2, True)
    e.run_stage(aid, "analyze")
    res = e.run_stage(aid, "master")
    assert res["done"] == 1 and not res["failed"]
    rel = e.run_stage(aid, "release")["release"]
    man = json.loads((Path(rel["path"]) / "processing_manifest.json").read_text("utf-8"))
    assert len(man["tracks"]) == 1
    e.set_track_excluded(t2, False)
    assert len(e.release_tracks(aid)) == 2


def test_stage_failure_does_not_stop_other_tracks(engine):
    e, root = engine
    aid = e.albums_overview()[1]["id"]
    (root / "02 — ВТОРОЙ" / "02 — Два.wav").unlink()
    res = e.run_stage(aid, "analyze")
    assert res["done"] == 1 and len(res["failed"]) == 1 and res["failed"][0]["title"]
    auto = e.run_auto(aid)
    assert auto[-1]["stage"] == "release" and auto[-1].get("blocked")  # no release from a broken album


# ---------------------------------------------------------------- interface
def _wait(app, predicate, timeout=180):
    until = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        time.sleep(0.02)
        assert time.monotonic() < until, "GUI action timed out"
    app.processEvents()


def test_main_window_open_folder_and_auto(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from srm.gui.flow import FlowWindow, human_error

    root = make_library(tmp_path)
    app = QApplication.instance() or QApplication([])
    w = FlowWindow(base_dir=tmp_path / "appdata", autoload=False)
    w.show()
    assert w.pages.currentIndex() == 0 and not w.btn_auto.isEnabled()
    w.open_folder(str(root))
    _wait(app, lambda: not w._running and w.album_list.count() > 0)
    assert w.album_list.count() == 2 and w.albums_title.text() == "АЛЬБОМЫ · 2"
    assert w.album_title.text() == "01 — ПЕРВЫЙ СВЕТ" and w.table.rowCount() == 4
    assert "Эмбиент" in w.style_note.text() and w.btn_auto.isEnabled()
    assert all(c.button.isEnabled() for c in w.cards.values())
    w.run_stage("analyze")
    assert not w.btn_auto.isEnabled() and w.btn_stop.isVisible()  # one job at a time
    _wait(app, lambda: not w._running)
    assert "проверено: 4 из 4" in w.cards["analyze"].state.text()
    w.auto_album()
    _wait(app, lambda: not w._running)
    assert "собран" in w.cards["release"].state.text() and w.cards["master"].state.text() == "готово: 3 из 3"
    assert "не идёт в релиз" in " ".join(w.table.item(r, 7).text() for r in range(4))
    assert "АВТО завершено: пакет релиза собран" in w.log.toPlainText()
    assert w.btn_ready.isVisible() and "релиз собран" in w.album_list.item(0).text()
    # opening one album folder of the same library reuses the project and shows only that album
    w.open_folder(str(root / "02 — ВТОРОЙ"))
    _wait(app, lambda: not w._running)
    assert w.album_list.count() == 1 and w.album_title.text() == "02 — ВТОРОЙ"
    w.open_folder(str(root))
    _wait(app, lambda: not w._running)
    assert w.album_list.count() == 2 and "релиз собран" in w.album_list.item(0).text()  # earlier work kept
    assert "FFmpeg" in human_error("FFmpeg/ffprobe not found") and "Обновить папку" in human_error(
        "source changed on disk since scan (content)")
    w.close()
