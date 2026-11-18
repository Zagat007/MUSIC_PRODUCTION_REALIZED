"""One-click release: engine on a synthetic song and the window flow (offscreen)."""

import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from srm.analysis.loudness import integrated_lufs
from srm.analysis.truepeak import measure_peaks
from srm.auto import engine as E


def _song(path, seconds=40.0, fs=48000):
    t = np.arange(int(seconds * fs)) / fs
    rng = np.random.default_rng(1)
    beat = (np.sin(2 * np.pi * 55 * t) * (np.mod(t, 0.5) < 0.25)) * 0.5
    lead = 0.2 * np.sin(2 * np.pi * 440 * t + 2 * np.sin(2 * np.pi * 5 * t))
    ring = 0.03 * np.sin(2 * np.pi * 3150 * t)  # static "metallic" resonance
    hiss = rng.normal(0, 0.01, (t.size, 2))
    x = np.stack([beat + lead + ring, beat + 0.9 * lead + ring], axis=1) + hiss
    sf.write(path, (x * 0.4).astype(np.float32), fs, subtype="PCM_24")
    return path


def test_release_engine(tmp_path):
    src = _song(tmp_path / "01 — Тест.wav")
    a = E.analyze_track(src)
    r = E.make_release(src, analysis=a, style="deep_house")
    out = tmp_path / "РЕЛИЗ" / "01 — Тест.wav"
    assert r["output"] == str(out) and out.is_file()
    info = sf.info(str(out))
    assert info.samplerate == 44100 and info.subtype == "PCM_24" and info.channels == 2
    y, fs = sf.read(str(out))
    assert abs(y.shape[0] / fs - 40.0) < 0.01  # same length
    assert measure_peaks(y, fs)["true_peak_dbtp"] <= -0.95
    assert abs(integrated_lufs(y, fs) - E.PROFILES["deep_house"]["lufs"]) < 1.0
    assert any("металлические" in x for x in r["actions"])
    # the source is never touched and a second run does not overwrite
    r2 = E.make_release(src, analysis=a)
    assert r2["output"].endswith("01 — Тест (2).wav")
    rep = E.write_report([r, r2], tmp_path / "РЕЛИЗ")
    assert "Громкость" in rep.read_text("utf-8")
    assert E.list_audio([tmp_path]) == [src]  # the release folder is not taken as input
    assert r["fixes_total"] >= 1 and r["fixes"]["resonances"] >= 1
    seen = []
    r3 = E.make_release(src, tmp_path / "mp3", analysis=a, fmt="both", on_fixes=seen.append)
    assert [Path(o).suffix for o in r3["outputs"]] == [".wav", ".mp3"]
    assert all(Path(o).is_file() for o in r3["outputs"]) and sf.info(r3["outputs"][1]).samplerate == 44100
    assert seen and seen[-1] == r3["fixes"]
    r4 = E.make_release(src, tmp_path / "mp3", analysis=a, fmt="mp3")
    assert r4["outputs"][0].endswith("01 — Тест (2).mp3") and len(list((tmp_path / "mp3").iterdir())) == 3


def test_album_style_vote():
    def an(guess, conf="НИЗКАЯ", declared=()):
        return {"det": {"style": guess if conf != "НИЗКАЯ" else "generic", "guess": guess, "confidence": conf,
                        "declared": list(declared)}}
    assert E.album_style([an("liquid_dnb"), an("liquid_dnb"), an("deep_house")])["style"] == "liquid_dnb"
    assert E.album_style([an("dark_jazz"), an("triphop_rnb"), an("deep_house")])["style"] == "generic"
    assert E.album_style([an("dark_jazz"), an("whale_jazz", "ВЫСОКАЯ", ["whale_jazz"])])["style"] == "whale_jazz"


def test_genres_order_and_prompt():
    from srm.auto.genres import GENRES, genre_from_prompt
    assert len(GENRES) == 10 and list(E.PROFILES)[-1] == "whale_jazz"  # Whale Jazz always last
    assert genre_from_prompt("Liquid drum and bass, 174 bpm, female vocal") == "liquid_dnb"
    assert genre_from_prompt("noir romantic jazz, liquid dnb, saxophone") == "noir_jazz_dnb"
    assert genre_from_prompt("dark cinematic noir jazz, upright bass") == "dark_jazz"
    assert genre_from_prompt("russian gypsy romance, violin, drum & bass") == "romani_dnb"
    assert genre_from_prompt("cinematic trip-hop, dark alternative R&B, low male vocal") == "triphop_rnb"
    assert genre_from_prompt("melodic deep house, atmospheric pads") == "deep_house"
    assert genre_from_prompt("dark trance, deep ritual trance") == "dark_trance"
    assert genre_from_prompt("dark cinematic melodic dnb, orchestral strings") == "cinematic_dnb"
    assert genre_from_prompt("ethnic deep house, mongolian throat singing, morin khuur") == "ethnic_house"
    assert genre_from_prompt("experimental cinematic jazz, whale jazz") == "whale_jazz"
    assert genre_from_prompt("lyrics only, no style words") is None


@pytest.mark.parametrize("genre", ["liquid_dnb", "noir_jazz_dnb", "dark_jazz", "romani_dnb", "triphop_rnb",
                                   "deep_house", "dark_trance", "cinematic_dnb", "ethnic_house", "whale_jazz"])
def test_every_genre_recipe(tmp_path, genre):
    src = _song(tmp_path / "song.wav", 20)
    a = E.analyze_track(src)
    r = E.make_release(src, analysis=a, style=genre)
    y, fs = sf.read(r["output"])
    assert np.isfinite(y).all() and measure_peaks(y, fs)["true_peak_dbtp"] <= -0.95
    assert integrated_lufs(y, fs) <= E.PROFILES[genre]["lufs"] + 0.6
    assert r["style_label"] == E.PROFILES[genre]["label"]


def test_window_flow(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from srm.auto.gui import COL_FIXES, COL_PROGRESS, COL_STATUS, AutoWindow

    _song(tmp_path / "01 — Один.wav", 25)
    _song(tmp_path / "02 — Два.wav", 25)
    os.environ["SRM_SETTINGS_INI"] = str(tmp_path / "settings.ini")
    app = QApplication.instance() or QApplication([])
    w = AutoWindow([str(tmp_path)])
    w.set_dest(str(tmp_path))  # a folder chosen by the user
    out = tmp_path / "out"
    out.mkdir()
    w.set_dest(str(out))
    w.fmt.setCurrentIndex(w.fmt.findData("both"))
    w.show()

    def wait(pred, timeout=240):
        until = time.monotonic() + timeout
        while not pred():
            app.processEvents()
            time.sleep(0.02)
            assert time.monotonic() < until
    wait(lambda: w.thread is None and all(t["analysis"] for t in w.tracks))
    assert w.table.rowCount() == 2 and w.go.isEnabled()
    assert w.table.cellWidget(0, COL_PROGRESS).text() == "100%"
    w.start_release()
    seen = []
    w.job.progress.connect(lambda r, f, t: seen.append(f))
    wait(lambda: w.thread is None)
    assert all(t["result"] for t in w.tracks) and (out / "ОТЧЁТ.txt").is_file()
    assert sorted(f.suffix for f in out.iterdir()) == [".mp3", ".mp3", ".txt", ".wav", ".wav"]
    assert w.table.item(0, COL_FIXES).text() not in ("", "—") and w.fix_total.text().startswith("Убрано")
    assert w.table.item(1, COL_STATUS).text() == "✓ Готово" and w.total.text().startswith("100%")
    assert seen and max(seen) <= 1.0
    w.close()
