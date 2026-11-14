from pathlib import Path

import numpy as np
import pytest

from srm.core.paths import PathSafetyError, atomic_write_text, ensure_within, next_version_dir, safe_name
from srm.dsp.quantize import quantize
from srm.ingest.textmatch import match_texts, norm_title, parse_prompt_text, read_text


def test_safe_name_unicode_and_reserved():
    assert safe_name("Песня №1 — тест") == "Песня №1 — тест"
    assert safe_name('a<b>c:"d"/e\\f|g?h*') == "a_b_c__d__e_f_g_h_"
    assert safe_name("CON") == "_CON"
    assert safe_name("  trailing. ") == "trailing"
    assert safe_name("") == "untitled"


def test_atomic_write_never_overwrites(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "a")
    with pytest.raises(FileExistsError):
        atomic_write_text(p, "b")
    assert p.read_text() == "a"
    assert not [f for f in tmp_path.iterdir() if f.name.startswith(".srm_tmp_")]


def test_version_dirs(tmp_path):
    assert next_version_dir(tmp_path).name == "v001"
    assert next_version_dir(tmp_path).name == "v002"


def test_ensure_within(tmp_path):
    ensure_within(tmp_path / "a" / "b", tmp_path)
    with pytest.raises(PathSafetyError):
        ensure_within(tmp_path / ".." / "escape", tmp_path)


def test_quantize_tpdf_once_and_bounds():
    x = np.zeros((1000, 2))
    q, info = quantize(x, 16, dither="tpdf", seed=1)
    vals = q >> 16
    assert set(np.unique(vals)).issubset({-1, 0, 1})
    q2, _ = quantize(x, 16, dither="tpdf", seed=1)
    assert np.array_equal(q, q2)  # deterministic
    q3, info3 = quantize(np.full((10, 1), 1.5), 24, dither="none")
    assert info3["clipped_samples"] == 10


def test_text_parsing_and_encodings(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("Стиль: эмбиент\nТекст:\nНочь\n".encode("cp1251"))
    txt, enc, warns = read_text(p)
    assert enc == "cp1251" and "эмбиент" in txt and warns
    parsed = parse_prompt_text(txt)
    assert parsed["style_prompt"] == "эмбиент" and parsed["lyrics"] == "Ночь"
    parsed2 = parse_prompt_text("Title: X\nStyle: liquid dnb\nModel: v4.5\n[Verse]\nla la")
    assert parsed2["model_version"] == "v4.5" and parsed2["lyrics"].startswith("[Verse]")


def test_text_matching(tmp_path):
    d = tmp_path
    a1, a2, a3 = d / "Song One.m4a", d / "Song One (2).m4a", d / "Other.m4a"
    t1, t2 = d / "Song One.txt", d / "Song One lyrics.md"
    m = match_texts([a1, a2, a3], [t1])
    assert m[a1]["status"] == "MATCHED_EXACT"
    assert m[a2]["status"] == "MATCHED_VARIANT"
    assert m[a3]["status"] == "NONE"
    m2 = match_texts([a1], [t1, t2])  # two candidates normalise identically -> ambiguous, human decides
    assert m2[a1]["status"] == "AMBIGUOUS"
    assert norm_title("Rain Code (1).m4a") == norm_title("rain_code_v2.m4a") == "rain code"
    m3 = match_texts([d / "Midnight Walk.m4a"], [d / "Midnight Walks.txt"])
    assert m3[d / "Midnight Walk.m4a"]["status"] == "NEEDS_CONFIRMATION"
    assert Path(m3[d / "Midnight Walk.m4a"]["text"]).name == "Midnight Walks.txt"


def test_track_number_parsing():
    from srm.service.engine import split_track_number

    assert split_track_number("01 — CONFESSION AT DAWN") == (1, "CONFESSION AT DAWN")
    assert split_track_number("07_Track") == (7, "Track")
    assert split_track_number("2024 Remix") == (None, "2024 Remix")
    assert split_track_number("Тихая вода") == (None, "Тихая вода")


def test_audio_ext_inside_text_name(tmp_path):
    a, b = tmp_path / "Song.m4a", tmp_path / "Song (1).m4a"
    t1, t2 = tmp_path / "Song.m4a.txt", tmp_path / "Song.m4a (1).txt"
    m = match_texts([a, b], [t1, t2])
    assert m[a]["text"] == t1 and m[b]["text"] == t2


def test_scan_excludes_and_numbering(tmp_path):
    import numpy as np
    import soundfile as sf

    from srm.service.engine import Engine

    lib = tmp_path / "lib"
    alb = lib / "03 — MY ALBUM"
    (lib / "_ОТЛОЖЕНО").mkdir(parents=True)
    alb.mkdir(parents=True)
    x = np.zeros((4800, 2))
    x[100:200] = 0.1
    for name in ("02 — SECOND.wav", "01 — FIRST.wav"):
        sf.write(alb / name, x, 48000)
    sf.write(lib / "_ОТЛОЖЕНО" / "take.wav", x, 48000)
    e = Engine(tmp_path / "proj")
    e.add_library_root(lib, exclude=["_*"])
    s = e.scan()
    assert s["audio_files"] == 2 and s["excluded_dirs"] == ["_ОТЛОЖЕНО"]
    tr = e.list_tracks("03 — MY ALBUM")
    assert [(t["track_no"], t["title"]) for t in tr] == [(1, "FIRST"), (2, "SECOND")]
    assert e.list_albums()[0]["metadata"]["album_title"] == "MY ALBUM"
    assert e.scan()["excluded_dirs"] == ["_ОТЛОЖЕНО"]  # exclusion persists with the root
