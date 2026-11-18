"""Blind comparison of song versions: the twelve required checks plus the rating rules."""
import copy
import csv
import io
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from srm.core.paths import sha256_file
from srm.core.tools import ffmpeg_input, get_tools, run
from srm.versus import core as C
from srm.versus.player import FADE_FRAMES, RATE, MixerCore
from srm.versus.store import Store

FS = 48000
SHORT = {**copy.deepcopy(C.DEFAULT_SETTINGS), "min_duration_s": 2.0}  # test files are a few seconds long


def tone(sec=4.0, freq=220.0, amp=0.3):
    t = np.arange(int(sec * FS)) / FS
    x = amp * np.sin(2 * np.pi * freq * t) * np.minimum(1, t / 0.05) * np.minimum(1, (sec - t) / 0.05)
    return np.column_stack([x, 0.9 * x])


def write(path: Path, audio, subtype="PCM_16"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".wav":
        sf.write(path, audio, FS, subtype=subtype)
        return path
    tmp = path.with_name(path.stem + "_src.wav")
    sf.write(tmp, audio, FS, subtype="PCM_16")
    codec = {".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], ".flac": ["-c:a", "flac"],
             ".m4a": ["-c:a", "aac", "-b:a", "192k"], ".ogg": ["-c:a", "libvorbis", "-q:a", "5"]}[path.suffix.lower()]
    p = run([get_tools().ffmpeg, "-nostdin", "-v", "error", "-y", "-i", ffmpeg_input(tmp), *codec, ffmpeg_input(path)])
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    tmp.unlink()
    return path


def song(tmp_path, names, title="НАЗВАНИЕ ПЕСНИ", **kw):
    folder = tmp_path / title
    files = [write(folder / n, tone(freq=200 + 70 * i, **kw)) for i, n in enumerate(names)]
    return folder, files


def session(folder, settings=SHORT, seed=7):
    groups = C.discover_groups(folder)
    assert len(groups) == 1
    analyses = [C.analyze_version(f) for f in groups[0]["files"]]
    return C.new_session(groups[0], analyses, settings=settings, seed=seed)


def play_all(state, pick):
    """Decide every scheduled pair with ``pick(pair) -> label | 'draw'``."""
    while (p := C.next_pair(state)) is not None and p["stage"] == "round":
        C.record_pair(state, p["id"], pick(p))


def label_of(state, file_name):
    vid = next(v["id"] for v in state["versions"] if v["name"] == file_name)
    return next(lab for lab, i in state["labels"].items() if i == vid)


# ---------------------------------------------------------------- 1-3: two, three, five versions
def test_01_two_wav_single_ab_comparison(tmp_path):
    folder, _ = song(tmp_path, ["НАЗВАНИЕ ПЕСНИ 1.wav", "НАЗВАНИЕ ПЕСНИ 2.wav"])
    st = session(folder)
    assert sorted(st["labels"]) == ["A", "B"] and len(st["pairs"]) == 1 and st["mode"] == "round_robin"
    best = label_of(st, "НАЗВАНИЕ ПЕСНИ 2.wav")
    play_all(st, lambda p: best)
    res = C.evaluate(st, SHORT)
    assert res["status"] == C.WINNER and res["winner"] == best and res["confidence"] >= 90
    assert C.version_of(st, res["winner"])["name"] == "НАЗВАНИЕ ПЕСНИ 2.wav"


def test_02_three_mp3_full_round_of_three(tmp_path):
    folder, _ = song(tmp_path, ["Песня 1.mp3", "Песня 2.mp3", "Песня 3.mp3"], title="Песня")
    st = session(folder)
    assert sorted(st["labels"]) == ["A", "B", "C"]
    assert sorted(tuple(sorted((p["a"], p["b"]))) for p in st["pairs"]) == [("A", "B"), ("A", "C"), ("B", "C")]
    assert all(v["tech"]["codec"] == "mp3" and v["tech"]["is_lossy"] for v in st["versions"])
    order = {label_of(st, f"Песня {i}.mp3"): i for i in (1, 2, 3)}  # version 3 beats 2 beats 1
    play_all(st, lambda p: max((p["a"], p["b"]), key=order.get))
    res = C.evaluate(st, SHORT)
    assert C.version_of(st, res["winner"])["id"] == 3 and C.version_of(st, res["second"])["id"] == 2


def test_03_five_files_of_different_formats(tmp_path):
    names = ["Трек 1.wav", "Трек 2.mp3", "Трек 3.flac", "Трек 4.m4a", "Трек 5.ogg"]
    folder, _ = song(tmp_path, names, title="Трек")
    st = session(folder)
    assert sorted(st["labels"]) == ["A", "B", "C", "D", "E"] and len(st["pairs"]) == 10
    assert {v["tech"]["codec"] for v in st["versions"]} == {"pcm_s16le", "mp3", "flac", "aac", "vorbis"}
    assert all(v["status"] == C.OK and v["tech"]["integrated_lufs"] is not None for v in st["versions"])
    play_all(st, lambda p: min(p["a"], p["b"]))
    res = C.evaluate(st, SHORT)
    assert res["status"] == C.WINNER and res["winner"] == "A"
    assert [r["wins"] for r in res["standings"]] == [4, 3, 2, 1, 0]


def test_more_than_five_versions_use_two_stage_tournament(tmp_path):
    folder, _ = song(tmp_path, [f"Гимн {i}.wav" for i in range(1, 8)], title="Гимн", sec=2.5)
    st = session(folder)
    assert len(st["labels"]) == 7 and st["mode"] == "tournament" and st["stage"] == "prelim" and not st["pairs"]
    with pytest.raises(C.VersusError):
        C.advance_tournament(st, SHORT)  # every version needs at least one mark first
    for i, lab in enumerate(sorted(st["labels"])):
        C.set_rating(st, lab, "replay", 10 - i)
    finalists = C.advance_tournament(st, SHORT)
    assert finalists == ["A", "B", "C", "D"] and len(st["pairs"]) == 6
    play_all(st, lambda p: min(p["a"], p["b"]))
    res = C.evaluate(st, SHORT)
    assert res["winner"] == "A" and [r["label"] for r in res["standings"][:4]] == ["A", "B", "C", "D"]
    C.set_mode(session(folder), "round_robin")  # the full round stays available: 21 pairs
    full = session(folder)
    C.set_mode(full, "round_robin")
    assert len(full["pairs"]) == 21
    C.set_mode(full, "selected", ["A", "C", "F"])
    assert len(full["pairs"]) == 3 and C.active_labels(full) == ["A", "C", "F"]


# ---------------------------------------------------------------- 4-5: duplicates, short songs
def test_04_exact_duplicates_are_detected_by_sha256(tmp_path):
    folder, files = song(tmp_path, ["Дубль 1.wav", "Дубль 2.wav"], title="Дубль")
    (folder / "Дубль 3.wav").write_bytes(files[0].read_bytes())
    st = session(folder)
    dup = st["versions"][2]
    assert dup["status"] == C.DUPLICATE and dup["duplicate_of"] == 1 and dup["sha256"] == st["versions"][0]["sha256"]
    assert sorted(st["labels"]) == ["A", "B"] and dup["id"] not in st["labels"].values()


def test_05_song_shorter_than_two_minutes_is_not_admitted(tmp_path):
    folder = tmp_path / "Короткая"
    write(folder / "Короткая 1.wav", tone(sec=121.0))
    write(folder / "Короткая 2.wav", tone(sec=121.0, freq=330))
    write(folder / "Короткая 3.wav", tone(sec=30.0, freq=440))
    st = session(folder, settings=C.DEFAULT_SETTINGS)
    assert [v["status"] for v in st["versions"]] == [C.OK, C.OK, "НЕ ДОПУЩЕНА — МЕНЬШЕ 2 МИНУТ"]
    assert len(st["labels"]) == 2 and len(st["pairs"]) == 1


def test_corrupt_file_is_reported_not_fatal(tmp_path):
    folder, _ = song(tmp_path, ["Бит 1.wav", "Бит 2.wav"], title="Бит")
    (folder / "Бит 3.mp3").write_bytes(b"this is not audio" * 100)
    st = session(folder)
    assert st["versions"][2]["status"] == C.REJECTED_CORRUPT and st["versions"][2]["error"]
    assert len(st["labels"]) == 2


# ---------------------------------------------------------------- 6: resume
def test_06_unfinished_test_is_restored(tmp_path):
    folder, _ = song(tmp_path, ["Река 1.wav", "Река 2.wav", "Река 3.wav"], title="Река")
    st = session(folder)
    store = Store(tmp_path / "state" / "versus.sqlite")
    sid = store.create(st)
    first = C.next_pair(st)
    C.record_pair(st, first["id"], first["a"])
    C.set_rating(st, first["a"], "vocal", 9)
    st["marks"]["chorus_s"] = 1.5
    store.save(sid, st, status=C.evaluate(st, SHORT)["status"])
    store.close()

    store2 = Store(tmp_path / "state" / "versus.sqlite")  # the program was closed and opened again
    summary = store2.active(st["group"]["key"])
    assert summary["id"] == sid and summary["status"] == C.IN_PROGRESS
    back = store2.load(sid)
    assert back["labels"] == st["labels"] and back["ratings"][first["a"]]["vocal"] == 9
    assert back["marks"]["chorus_s"] == 1.5
    nxt = C.next_pair(back)
    assert nxt["id"] != first["id"] and len([p for p in back["pairs"] if p["result"]]) == 1
    play_all(back, lambda p: p["a"])
    assert C.evaluate(back, SHORT)["status"] in (C.WINNER, C.NEED_FINAL)


# ---------------------------------------------------------------- 7-8: playback
def test_07_switching_keeps_the_same_time_position():
    core = MixerCore()
    n = RATE * 3
    a = np.full((n, 2), 1000, np.int16)
    b = np.full((n, 2), 5000, np.int16)
    core.set_tracks({"A": a, "B": b})
    core.playing = True
    assert (core.render(4800) == 1000).all() and core.pos == 4800
    core.switch("B")
    assert core.pos == 4800  # switching does not move the playhead
    out = core.render(4800)
    assert core.pos == 9600 and (out[FADE_FRAMES:] == 5000).all()
    assert out[0, 0] < out[FADE_FRAMES - 1, 0] <= 5000  # short crossfade instead of a click
    core.seek(RATE)
    core.switch("A")
    assert core.pos == RATE
    core.set_loop(RATE, RATE + 9600)
    core.render(9600 + 100)
    assert core.pos == RATE + 100  # the looped region repeats
    core.set_loop(None, None)
    core.seek(n - 100)
    tail = core.render(1000)
    assert not core.playing and (tail[100:] == 0).all()  # stops cleanly at the end


def test_08_loudness_matching_only_during_playback(tmp_path):
    folder = tmp_path / "Громкость"
    loud = write(folder / "Громкость 1.wav", tone(amp=0.6))
    quiet = write(folder / "Громкость 2.wav", tone(amp=0.1))
    before = {p: sha256_file(p) for p in (loud, quiet)}
    st = session(folder)
    gains = C.playback_gains_db(st)
    lab_loud, lab_quiet = label_of(st, loud.name), label_of(st, quiet.name)
    assert gains[lab_quiet] == 0.0 and -16.5 < gains[lab_loud] < -14.5  # only ever turned down
    tracks = {lab: C.load_playback(C.version_of(st, lab)["path"]) for lab in st["labels"]}
    pristine = {k: v.copy() for k, v in tracks.items()}
    core = MixerCore()
    core.set_tracks(tracks, gains)
    core.playing = True
    core.seek(RATE)
    core.switch(lab_loud)
    core._fade_from = None
    rms_loud = np.sqrt(np.mean(core.render(RATE).astype(float) ** 2))
    core.seek(RATE)
    core.switch(lab_quiet)
    core._fade_from = None
    rms_quiet = np.sqrt(np.mean(core.render(RATE).astype(float) ** 2))
    assert abs(20 * np.log10(rms_loud / rms_quiet)) < 0.5  # both sound equally loud
    assert all(np.array_equal(tracks[k], pristine[k]) for k in tracks)  # buffers untouched
    assert {p: sha256_file(p) for p in (loud, quiet)} == before  # originals untouched
    # and a louder version gains nothing in the rating
    C.record_pair(st, st["pairs"][0]["id"], "draw")
    rows = {r["label"]: r for r in C.standings(st, SHORT)}
    assert rows[lab_loud]["score"] == rows[lab_quiet]["score"]


# ---------------------------------------------------------------- rating rules
def test_near_tie_requires_final_ab_and_override_is_recorded(tmp_path):
    folder, _ = song(tmp_path, ["Спор 1.wav", "Спор 2.wav"], title="Спор")
    st = session(folder)
    C.record_pair(st, st["pairs"][0]["id"], "draw")
    res = C.evaluate(st, SHORT)
    assert res["status"] == C.NEED_FINAL and res["note"] == "ТРЕБУЕТСЯ ФИНАЛЬНОЕ A/B-СРАВНЕНИЕ"
    final = C.next_pair(st)
    assert final["stage"] == "final"
    C.record_pair(st, final["id"], "B")
    res = C.evaluate(st, SHORT)
    assert res["status"] == C.WINNER and res["winner"] == "B" and 60 <= res["confidence"] <= 75
    C.set_override(st, "A", "нравится тембр", res["winner"])
    res = C.evaluate(st, SHORT)
    assert res["winner"] == "A" and res["second"] == "B" and res["auto_winner"] == "B"
    rep = C.build_report(st, SHORT)
    assert rep["manual_override"]["reason"] == "нравится тембр"
    assert "РУЧНОЕ ИЗМЕНЕНИЕ ПОБЕДИТЕЛЯ: да" in C.report_text(rep)


def test_ratings_weights_and_technical_penalties_shape_the_result(tmp_path):
    folder, _ = song(tmp_path, ["Вес 1.wav", "Вес 2.wav"], title="Вес")
    st = session(folder)
    C.record_pair(st, st["pairs"][0]["id"], "draw")
    C.set_rating(st, "A", "vocal", 9)
    C.set_rating(st, "A", "ending", 3)
    C.set_rating(st, "B", "vocal", 3)
    C.set_rating(st, "B", "ending", 9)
    vocal = {**SHORT, "weights": {**SHORT["weights"], "vocal": 5.0, "ending": 0.5}}
    ending = {**SHORT, "weights": {**SHORT["weights"], "vocal": 0.5, "ending": 5.0}}
    assert C.standings(st, vocal)[0]["label"] == "A" and C.standings(st, ending)[0]["label"] == "B"
    st["ratings"] = {"A": {"vocal": 7}, "B": {"vocal": 6}}
    assert C.standings(st, SHORT)[0]["label"] == "A"
    C.version_of(st, "A")["penalties"] = [{"code": "clipping_severe", "points": 25, "text": "сильный клиппинг"},
                                         {"code": "abrupt_end", "points": 15, "text": "резкий обрыв в конце"}]
    assert C.standings(st, SHORT)[0]["label"] == "B"  # technical defects outweigh a small lead
    with pytest.raises(C.VersusError):
        C.set_rating(st, "A", "vocal", 11)


def test_technical_penalties_do_not_reward_loudness():
    base = {"true_peak_dbtp": -1.0, "plr_db": 12.0, "is_lossy": False, "sample_rate": 48000, "channels": 2}
    assert C.technical_penalties({**base, "integrated_lufs": -8.0}) == C.technical_penalties(
        {**base, "integrated_lufs": -20.0}) == []
    codes = {p["code"] for p in C.technical_penalties(
        {**base, "clipping_longest_run": 20, "abrupt_end": True, "true_peak_dbtp": 1.4, "plr_db": 5.0})}
    assert codes == {"clipping_severe", "abrupt_end", "true_peak_high", "dynamics_flat"}


# ---------------------------------------------------------------- 9: reports
def test_09_txt_csv_and_json_reports(tmp_path):
    folder, _ = song(tmp_path, ["Отчёт 1.wav", "Отчёт 2.wav", "Отчёт 3.wav"], title="Отчёт")
    st = session(folder)
    play_all(st, lambda p: min(p["a"], p["b"]))
    C.set_rating(st, "A", "chorus", 8)
    paths = C.write_reports(st, SHORT)
    assert {Path(p).name for p in paths.values()} == {"РЕЗУЛЬТАТ_СРАВНЕНИЯ.txt", "РЕЗУЛЬТАТ_СРАВНЕНИЯ.csv",
                                                      "result.json"}
    rep = json.loads(Path(paths["json"]).read_text("utf-8"))
    win = C.version_of(st, "A")
    assert rep["winner_version"] == win["id"] and rep["status"] == C.WINNER and rep["confidence_percent"]
    assert {v["sha256"] for v in rep["versions"]} == {v["sha256"] for v in st["versions"]}
    assert rep["blind_mapping"]["A"]["file"] == win["name"] and len(rep["pairs"]) == 3
    assert all(Path(v["path"]).is_file() and v["technical"]["integrated_lufs"] is not None for v in rep["versions"])
    text = Path(paths["txt"]).read_text("utf-8")
    assert f"ПОБЕДИТЕЛЬ: ВЕРСИЯ {win['id']}" in text and "УВЕРЕННОСТЬ:" in text and "ВТОРОЕ МЕСТО:" in text
    rows = list(csv.reader(io.StringIO(Path(paths["csv"]).read_text("utf-8-sig")), delimiter=";"))
    assert rows[0][:3] == ["Версия", "Обозначение", "Файл"] and len(rows) == 4
    assert rows[0].index("Припев") and "8" in rows[1 + win["id"] - 1]


# ---------------------------------------------------------------- 10-12: apply
def test_10_cancelled_apply_changes_nothing(tmp_path):
    folder, files = song(tmp_path, ["Отмена 1.wav", "Отмена 2.wav"], title="Отмена")
    st = session(folder)
    play_all(st, lambda p: "A")
    before = sorted(p.name for p in folder.iterdir())
    hashes = {p: sha256_file(p) for p in files}
    ops = C.plan_apply(st, "A", rename=True, number=3, defer_losers=True)
    assert len(ops) == 2 and C.check_apply(st, ops) == []  # planning and checking are read-only
    with pytest.raises(C.VersusError, match="подтверждения"):
        C.execute_apply(st, ops, SHORT)
    assert sorted(p.name for p in folder.iterdir()) == before and st["applied"] is None
    assert {p: sha256_file(p) for p in files} == hashes


def test_11_winner_moves_only_after_confirmation(tmp_path):
    folder, _ = song(tmp_path, ["Победа 1.wav", "Победа 2.wav", "Победа 3.wav"], title="Победа")
    album = tmp_path / "Альбом"
    album.mkdir()
    st = session(folder)
    win_label = label_of(st, "Победа 2.wav")
    play_all(st, lambda p: win_label if win_label in (p["a"], p["b"]) else p["a"])
    sha = C.version_of(st, win_label)["sha256"]
    ops = C.plan_apply(st, win_label, rename=True, number=7, album_folder=album, defer_losers=True)
    applied = C.execute_apply(st, ops, SHORT, confirmed=True)
    target = album / "07 Победа.wav"
    assert target.is_file() and sha256_file(target) == sha  # renamed NN TITLE, content untouched
    assert sorted(p.name for p in (folder / "_ОТЛОЖЕНО_ВЕРСИИ").iterdir()) == ["Победа 1.wav", "Победа 3.wav"]
    assert not list(folder.glob("Победа *.wav"))  # nothing deleted: every file was moved, not removed
    assert (folder / "result.json").is_file() and len(applied["operations"]) == 3
    assert C.evaluate(st, SHORT)["status"] == C.APPLIED
    # an occupied target is refused before anything moves
    folder2, _ = song(tmp_path, ["Занято 1.wav", "Занято 2.wav"], title="Занято")
    st2 = session(folder2)
    (album / "07 Занято.wav").write_bytes(b"x")
    ops2 = C.plan_apply(st2, "A", rename=True, number=7, album_folder=album)
    assert C.check_apply(st2, ops2)
    with pytest.raises(C.VersusError):
        C.execute_apply(st2, ops2, SHORT, confirmed=True)
    assert len(list(folder2.glob("*.wav"))) == 2


def test_12_russian_paths_and_names(tmp_path):
    root = tmp_path / "Музыка — НА РЕЛИЗ"
    folder, files = song(root, ["03 — Ёлка и Щёлочь (1).mp3", "03 — Ёлка и Щёлочь (2).mp3"], title="Ёлка и Щёлочь")
    groups = C.discover_groups(root)
    assert len(groups) == 1 and groups[0]["name"] == "Ёлка и Щёлочь" and len(groups[0]["files"]) == 2
    st = session(folder)
    play_all(st, lambda p: "B")
    ops = C.plan_apply(st, "B", rename=True, number=3, defer_losers=True)
    C.execute_apply(st, ops, SHORT, confirmed=True)
    assert (folder / "03 Ёлка и Щёлочь.mp3").is_file()
    assert len(list((folder / "_ОТЛОЖЕНО_ВЕРСИИ").iterdir())) == 1
    text = (folder / "РЕЗУЛЬТАТ_СРАВНЕНИЯ.txt").read_text("utf-8")
    assert "Ёлка и Щёлочь" in text and "Музыка — НА РЕЛИЗ" in text
    assert C.discover_groups(root) == []  # deferred versions are not offered again


def test_grouping_rules(tmp_path):
    album = tmp_path / "Альбом"
    for name in ["01 - Заря.wav", "01 - Заря (1).wav", "02 - Ночь.wav", "02 - Ночь v2.wav", "02 - Ночь v3.wav",
                 "03 - Одна.wav"]:
        (album / name).parent.mkdir(parents=True, exist_ok=True)
        (album / name).write_bytes(b"0")
    groups = {g["name"]: len(g["files"]) for g in C.discover_groups(tmp_path)}
    assert groups == {"Заря": 2, "Ночь": 3}
    loose = tmp_path / "Разное"
    for name in ["demo_final.wav", "mix_loud.wav", "take_three.wav"]:
        (loose / name).parent.mkdir(parents=True, exist_ok=True)
        (loose / name).write_bytes(b"0")
    assert [(g["name"], len(g["files"])) for g in C.discover_groups(loose)] == [("Разное", 3)]
    assert C.labels_for(28)[-3:] == ["Z", "AA", "AB"]
    # an album track list (different songs, one file each) is not one song with many versions
    release = tmp_path / "01 — AFTER THE LAST LIGHT"
    for name in ["01 — CONFESSION AT DAWN.mp3", "02 — DAWN WITHOUT MERCY.mp3", "03 — NO MORNING.m4a",
                 "09 — GOLDEN GRAVITY.mp3", "10 — GOLDEN SILENCE.mp3", "12 — SILVER LINE.wav"]:
        (release / name).parent.mkdir(parents=True, exist_ok=True)
        (release / name).write_bytes(b"0")
    assert C.discover_groups(release) == []
    assert C.count_audio(release) == 6
    tracks = C.discover_groups(release, singles=True)  # the interface lists every track of the album
    assert [g["name"] for g in tracks][:2] == ["CONFESSION AT DAWN", "DAWN WITHOUT MERCY"]
    assert len(tracks) == 6 and all(len(g["files"]) == 1 for g in tracks)
    mixed = {g["name"]: len(g["files"]) for g in C.discover_groups(album, singles=True)}
    assert mixed == {"Заря": 2, "Ночь": 3, "Одна": 1}
    assert [(g["name"], len(g["files"])) for g in C.discover_groups(loose, singles=True)] == [("Разное", 3)]


# ---------------------------------------------------------------- interface
def _wait(app, predicate, timeout=90):
    until = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        time.sleep(0.02)
        assert time.monotonic() < until, "GUI action timed out"
    app.processEvents()


def test_interface_blind_flow_and_confirmed_apply(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from srm.versus.gui import VersusWindow

    folder, files = song(tmp_path, ["Окно 1.wav", "Окно 2.wav", "Окно 3.wav"], title="Окно")
    app = QApplication.instance() or QApplication([])
    w = VersusWindow(store_path=tmp_path / "db" / "versus.sqlite", folder=str(folder), audio=False)
    w.settings["min_duration_s"] = 2.0
    w.show()
    assert w.pages.currentIndex() == 1 and w.groups_table.item(0, 2).text() == C.NOT_STARTED
    w.open_selected_group()
    _wait(app, lambda: w.state is not None and w.pages.currentIndex() == 2)
    assert w.tech_table.rowCount() == 3 and "A/B/C" in w.tech_summary.text()
    w.start_listening()
    _wait(app, lambda: w._loaded_for == w.session_id and bool(w.version_buttons))
    shown = " ".join([w.listen_head.text(), w.listen_note.text(), *(b.text() for b in w.version_buttons.values())])
    assert "Окно 1" not in shown and ".wav" not in shown  # real names stay hidden during the test
    assert sorted(w.version_buttons) == sorted([w._pair["a"], w._pair["b"]])
    w.switch_to(w._pair["b"])
    assert w.core.active == w._pair["b"]
    w.rating_table.cellWidget(0, 0).setValue(8)
    assert w.state["ratings"][w._shown[0]]["intro"] == 8
    w.refresh_table()
    assert w.standings_table.item(0, 2).text() == "скрыто до конца теста"
    # closing and reopening continues from the same comparison
    sid, pair_id = w.session_id, w._pair["id"]
    w.verdict(w._pair["a"])
    w.close()
    w = VersusWindow(store_path=tmp_path / "db" / "versus.sqlite", folder=str(folder), audio=False)
    w.settings["min_duration_s"] = 2.0
    w.show()
    assert w.groups_table.item(0, 2).text() == C.IN_PROGRESS
    w.open_selected_group()
    assert w.session_id == sid
    w.start_listening()
    _wait(app, lambda: w._loaded_for == w.session_id and bool(w.version_buttons))
    assert w._pair["id"] != pair_id
    while w.pages.currentIndex() == 3 and w._pair:
        w.verdict("A" if "A" in (w._pair["a"], w._pair["b"]) else w._pair["a"])
    assert w.pages.currentIndex() == 5 and w.result_head.text().startswith("ПОБЕДИТЕЛЬ: ВЕРСИЯ")
    assert "УВЕРЕННОСТЬ:" in w.result_text.toPlainText() and "Окно" in w.result_text.toPlainText()
    w.go(6)
    w.chk_defer.setChecked(True)
    before = sorted(p.name for p in folder.iterdir())
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    w.do_apply()
    assert sorted(p.name for p in folder.iterdir()) == before  # "Отмена": nothing moved, no report written
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w.do_apply()
    assert len(list((folder / "_ОТЛОЖЕНО_ВЕРСИИ").iterdir())) == 2 and (folder / "result.json").is_file()
    assert w.store.active(w.state["group"]["key"])["status"] == C.APPLIED
    w.go(7)
    assert w.history_table.rowCount() == 1 and w.history_table.item(0, 2).text() == C.APPLIED
    w.close()
