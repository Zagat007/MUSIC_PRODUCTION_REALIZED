"""Logic of the blind version test: grouping, technical check, schedule, rating, report, apply.

Nothing here touches the GUI. Audio files are only read; the single place that renames or moves
anything is ``execute_apply`` and it runs only with an explicit plan confirmed by the user.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import math
import os
import random
import re
import shutil
import time
import unicodedata
from pathlib import Path

import numpy as np

from srm.analysis.defects import detect_clipping
from srm.analysis.loudness import measure_loudness
from srm.analysis.metrics import level_dynamics, silence_metrics, waveform_overview
from srm.analysis.truepeak import measure_peaks
from srm.core.paths import atomic_write_text, safe_name, sha256_file
from srm.ingest.decode import decode
from srm.ingest.probe import probe

AUDIO_EXT = {".wav", ".wave", ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".aif", ".aiff"}
MIN_DURATION_S = 120.0
DEFERRED_DIR = "_ОТЛОЖЕНО_ВЕРСИИ"
REPORT_TXT, REPORT_CSV, REPORT_JSON = "РЕЗУЛЬТАТ_СРАВНЕНИЯ.txt", "РЕЗУЛЬТАТ_СРАВНЕНИЯ.csv", "result.json"
ROUND_ROBIN_MAX = 5  # up to five versions every pair is compared; above that the two-stage mode is the default
FINALISTS = 4

CRITERIA = [
    ("intro", "Сила вступления"), ("vocal", "Качество вокала"), ("diction", "Естественность произношения"),
    ("emotion", "Эмоциональность"), ("chorus", "Припев"), ("hook", "Запоминаемость"),
    ("arrangement", "Аранжировка"), ("structure", "Структура"), ("clarity", "Чистота звука"),
    ("artifacts", "Отсутствие артефактов"), ("ending", "Качество финала"), ("replay", "Желание переслушать"),
]
CRITERIA_NAMES = dict(CRITERIA)
DEFAULT_SETTINGS = {
    "weights": {k: 1.0 for k, _ in CRITERIA},
    "pair_share": 0.5,       # share of pairwise results in the final score (the rest are the 1-10 ratings)
    "tech_weight": 0.25,     # final-score points removed per technical penalty point
    "close_margin": 3.0,     # leaders closer than this need a final A/B
    "min_duration_s": MIN_DURATION_S,
}

NOT_STARTED, IN_PROGRESS, NEED_FINAL, WINNER, APPLIED = (
    "НЕ НАЧАТО", "В ПРОЦЕССЕ", "НУЖЕН ФИНАЛ", "ПОБЕДИТЕЛЬ ОПРЕДЕЛЁН", "РЕЗУЛЬТАТ ПРИМЕНЁН")
ONE_VERSION = "ОДНА ВЕРСИЯ"
OK, REJECTED_SHORT, REJECTED_CORRUPT, DUPLICATE = (
    "ДОПУЩЕНА", "НЕ ДОПУЩЕНА — МЕНЬШЕ 2 МИНУТ", "НЕ ДОПУЩЕНА — ФАЙЛ ПОВРЕЖДЁН", "ДУБЛЬ")
MODES = {"round_robin": "Полный круг", "tournament": "Быстрый турнир", "selected": "Сравнение выбранных"}


class VersusError(RuntimeError):
    pass


# ------------------------------------------------------------------ grouping
_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*[—–\-_.)]+\s*(?=\S)")
_VARIANT = re.compile(
    r"(\s*[\(\[]\s*\d+\s*[\)\]]"
    r"|[\s_\-]+(?:v|ver|version|take|alt|mix|вариант|версия|копия|copy)[\s_\-]*\d*"
    r"|[\s_\-]+\d{1,2})$", re.I)


def natural_key(name: str):
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name)]


def song_title(stem: str) -> str:
    """'03 - Песня (2)' / 'Песня 2' / 'Песня v3' -> 'Песня' (the part shared by versions of one song)."""
    s = unicodedata.normalize("NFC", stem).strip()
    s = _TRACK_NO.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _VARIANT.sub("", s).strip()
    return s


def looks_like_album(files: list[Path]) -> bool:
    """True for a track list ('01 — A', '02 — B', ...): numbered files with different numbers and titles."""
    numbers = [int(re.match(r"\s*(\d+)", f.stem).group(1)) for f in files if _TRACK_NO.match(f.stem)]
    return len(numbers) >= 2 and len(numbers) * 2 >= len(files) and len(set(numbers)) == len(numbers)


def count_audio(folder: str | Path) -> int:
    folder = Path(folder)
    return sum(1 for f in folder.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXT) if folder.is_dir() else 0


def discover_groups(root: str | Path, max_depth: int = 5, singles: bool = False) -> list[dict]:
    """Find groups of two or more versions of one song under ``root``.

    Files in one folder whose names differ only by a version suffix form a group. A folder the
    user picked directly is treated as one group even when its files have unrelated names -
    unless it is an album track list (different songs, one file each), which has nothing to compare.
    With ``singles`` the songs that have only one file are listed too (one file each), so an album
    shows all its tracks; they cannot be compared until a second version appears.
    """
    root = Path(root)
    if not root.is_dir():
        raise VersusError(f"Папка не найдена: {root}")
    groups = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = sorted((d for d in dirnames if not d.startswith(("_", "."))), key=natural_key) \
            if depth < max_depth else []
        files = sorted((here / f for f in filenames if Path(f).suffix.lower() in AUDIO_EXT),
                       key=lambda p: natural_key(p.name))
        if len(files) < (1 if singles else 2):
            continue
        clusters: dict[str, list[Path]] = {}
        titles: dict[str, str] = {}
        for f in files:
            title = song_title(f.stem) or here.name
            key = title.casefold()
            clusters.setdefault(key, []).append(f)
            titles.setdefault(key, title)
        found = [(titles[k], v) for k, v in clusters.items() if len(v) >= 2]
        if not found and here == root and len(files) >= 2 and not looks_like_album(files):
            found = [(here.name, files)]
        elif singles:
            found = [(titles[k], v) for k, v in clusters.items()]
        for title, members in found:
            groups.append({"name": title, "folder": str(here), "files": [str(m) for m in members],
                           "key": f"{os.path.normcase(str(here))}|{title.casefold()}"})
    return groups


def labels_for(n: int) -> list[str]:
    """A, B, ..., Z, AA, AB, ... - as many as there are versions."""
    out, size = [], 1
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while len(out) < n:
        out += ["".join(t) for t in itertools.product(alphabet, repeat=size)]
        size += 1
    return out[:n]


# ------------------------------------------------------------------ technical check
def analyze_version(path: str | Path) -> dict:
    """Measure one file. Read-only: the file is probed, hashed and decoded to memory."""
    p = Path(path)
    st = p.stat()
    info: dict = {"path": str(p), "name": p.name, "size": st.st_size, "mtime": st.st_mtime, "sha256": sha256_file(p),
                  "error": None, "tech": {}, "waveform": []}
    pr = probe(p)
    if not pr.ok:
        info["error"] = pr.error or "файл не распознан как аудио"
        return info
    channels = min(pr.channels or 2, 2)
    tech = {
        "container": pr.container, "codec": pr.codec, "is_lossy": pr.is_lossy, "sample_rate": pr.sample_rate,
        "channels": pr.channels, "bits_per_sample": pr.bits_per_sample, "bit_rate": pr.bit_rate,
        "container_duration_s": pr.duration_s,
    }
    info["tech"] = tech
    try:
        dec = decode(p, sample_rate=pr.sample_rate, channels=channels)
    except Exception as exc:
        info["error"] = f"не удалось декодировать: {str(exc)[-200:]}"
        return info
    x, fs = dec.audio, dec.sample_rate
    loud = measure_loudness(x, fs)
    peaks = measure_peaks(x, fs)
    dyn = level_dynamics(x, fs, loud.integrated_lufs, peaks["true_peak_dbtp"])
    sil = silence_metrics(x, fs)
    clips = detect_clipping(x, fs)
    dur = dec.duration_s
    mismatch = bool(pr.duration_s and abs(dur - pr.duration_s) > max(0.5, 0.01 * pr.duration_s))
    tech.update({
        "duration_s": round(dur, 3),
        "integrated_lufs": loud.summary()["integrated_lufs"],
        "lra_lu": loud.summary()["lra_lu"],
        "true_peak_dbtp": peaks["true_peak_dbtp"],
        "sample_peak_dbfs": peaks["sample_peak_dbfs"],
        "plr_db": dyn.get("plr_db"),
        "crest_db": dyn.get("crest_factor_db"),
        "clipping_events": len(clips),
        "clipping_longest_run": max((c["evidence"]["longest_run_samples"] for c in clips), default=0),
        "samples_at_full_scale": peaks["samples_at_or_above_0dbfs"],
        "entirely_silent": bool(sil.get("entirely_silent")),
        "leading_silence_s": sil.get("leading_silence_s"),
        "trailing_silence_s": sil.get("trailing_silence_s"),
        "abrupt_start": bool(sil.get("abrupt_start")),
        "abrupt_end": bool(sil.get("abrupt_end")),
        "decode_warnings": len(dec.warnings),
        "duration_mismatch": mismatch,
    })
    info["waveform"] = waveform_overview(x, 1200)["peak"]
    return info


def technical_penalties(tech: dict) -> list[dict]:
    """Penalty points with plain-language reasons. Loudness itself is never rewarded or punished."""
    out = []

    def add(code, points, text):
        out.append({"code": code, "points": points, "text": text})

    run = tech.get("clipping_longest_run") or 0
    events = tech.get("clipping_events") or 0
    if run >= 12 or events >= 20:
        add("clipping_severe", 25, "сильный клиппинг (срезанные пики)")
    elif run >= 6 or events >= 5:
        add("clipping_moderate", 10, "заметный клиппинг")
    elif events:
        add("clipping_minor", 3, "единичные срезанные пики")
    tp = tech.get("true_peak_dbtp")
    if tp is not None and tp > 1.0:
        add("true_peak_high", 10, f"пики выше нуля ({tp:+.1f} dBTP) — искажения при кодировании")
    elif tp is not None and tp > 0.0:
        add("true_peak_over", 5, f"пики чуть выше нуля ({tp:+.1f} dBTP)")
    if tech.get("abrupt_end"):
        add("abrupt_end", 15, "резкий обрыв в конце")
    if tech.get("abrupt_start"):
        add("abrupt_start", 3, "резкое начало без атаки")
    if (tech.get("leading_silence_s") or 0) > 2.0:
        add("leading_silence", 3, f"тишина в начале {tech['leading_silence_s']:.1f} с")
    if (tech.get("trailing_silence_s") or 0) > 5.0:
        add("trailing_silence", 3, f"тишина в конце {tech['trailing_silence_s']:.1f} с")
    plr = tech.get("plr_db")
    if plr is not None and plr < 6.0:
        add("dynamics_flat", 10, f"очень сжатая динамика (PLR {plr:.1f} dB)")
    elif plr is not None and plr < 8.0:
        add("dynamics_low", 5, f"сжатая динамика (PLR {plr:.1f} dB)")
    if tech.get("is_lossy"):
        br = tech.get("bit_rate") or 0
        if br and br < 128_000:
            add("source_lossy", 10, f"сжатый источник низкого битрейта ({br // 1000} кбит/с)")
        elif br and br < 192_000:
            add("source_lossy", 6, f"сжатый источник ({br // 1000} кбит/с)")
        elif br and br < 256_000:
            add("source_lossy", 3, f"сжатый источник ({br // 1000} кбит/с)")
        else:
            add("source_lossy", 1, "сжатый источник (с потерями)")
    if (tech.get("sample_rate") or 44100) < 44100:
        add("low_sample_rate", 5, f"низкая частота дискретизации ({tech['sample_rate']} Гц)")
    if tech.get("channels") == 1:
        add("mono", 3, "монофонический файл")
    if tech.get("decode_warnings"):
        add("decode_warnings", 10, "декодер сообщил об ошибках в файле")
    if tech.get("duration_mismatch"):
        add("duration_mismatch", 10, "длительность не совпадает с заявленной в файле")
    return out


def admission(info: dict, min_duration_s: float = MIN_DURATION_S) -> str:
    if info.get("error") or info["tech"].get("entirely_silent"):
        return REJECTED_CORRUPT
    if (info["tech"].get("duration_s") or 0) < min_duration_s:
        return REJECTED_SHORT
    return OK


# ------------------------------------------------------------------ session
def new_session(group: dict, analyses: list[dict], *, settings: dict | None = None, seed: int | None = None) -> dict:
    """Build a fresh test state: admit versions, hide names behind random letters, schedule the pairs."""
    settings = settings or DEFAULT_SETTINGS
    seed = random.SystemRandom().randrange(2**31) if seed is None else seed
    versions, by_sha = [], {}
    for i, info in enumerate(analyses, start=1):
        v = {"id": i, "path": info["path"], "name": info["name"], "size": info["size"], "sha256": info["sha256"],
             "error": info.get("error"), "tech": info.get("tech", {}), "waveform": info.get("waveform", []),
             "duplicate_of": None}
        v["status"] = admission(info, settings.get("min_duration_s", MIN_DURATION_S))
        if not info["sha256"]:
            pass  # unreadable file: nothing to compare by
        elif info["sha256"] in by_sha:
            v["status"], v["duplicate_of"] = DUPLICATE, by_sha[info["sha256"]]
        else:
            by_sha[info["sha256"]] = i
        v["penalties"] = technical_penalties(v["tech"]) if v["status"] in (OK, REJECTED_SHORT) else []
        v["tech_score"] = 0 if v["error"] else max(0, 100 - sum(p["points"] for p in v["penalties"]))
        versions.append(v)
    admitted = [v["id"] for v in versions if v["status"] == OK]
    rng = random.Random(seed)
    shuffled = admitted[:]
    rng.shuffle(shuffled)
    labels = dict(zip(labels_for(len(shuffled)), shuffled))
    state = {
        "schema": 1, "group": {k: group[k] for k in ("name", "folder", "key")}, "created_at": time.time(),
        "seed": seed, "versions": versions, "labels": labels, "mode": None, "selected": None,
        "stage": "round", "pairs": [], "ratings": {}, "marks": {"chorus_s": None, "loop": None},
        "override": None, "applied": None, "final_confirmed": False,
    }
    set_mode(state, "round_robin" if len(labels) <= ROUND_ROBIN_MAX else "tournament")
    return state


def version_of(state: dict, label: str) -> dict:
    vid = state["labels"][label]
    return next(v for v in state["versions"] if v["id"] == vid)


def _round_robin(labels: list[str], rng: random.Random, stage: str, start_id: int) -> list[dict]:
    pairs = [list(p) for p in itertools.combinations(labels, 2)]
    rng.shuffle(pairs)
    out = []
    for i, p in enumerate(pairs):
        rng.shuffle(p)  # which one is offered first is random too
        out.append({"id": start_id + i, "a": p[0], "b": p[1], "stage": stage, "result": None, "decided_at": None})
    return out


def set_mode(state: dict, mode: str, selected: list[str] | None = None) -> None:
    """(Re)build the schedule. Only allowed while no comparison has been decided."""
    if mode not in MODES:
        raise VersusError(f"неизвестный режим {mode}")
    if any(p["result"] for p in state["pairs"]):
        raise VersusError("Режим нельзя менять после начала сравнения. Начните тест заново.")
    labels = sorted(state["labels"], key=lambda s: (len(s), s))
    rng = random.Random(state["seed"] + 1)
    state["mode"], state["selected"], state["pairs"] = mode, None, []
    if mode == "selected":
        chosen = [x for x in labels if x in set(selected or [])]
        if len(chosen) < 2:
            raise VersusError("Выберите не меньше двух вариантов.")
        state["selected"], state["stage"] = chosen, "round"
        state["pairs"] = _round_robin(chosen, rng, "round", 1)
    elif mode == "tournament" and len(labels) > FINALISTS:
        state["stage"] = "prelim"  # stage 1: every version gets a rating; stage 2: round robin of the best
    else:
        state["stage"] = "round"
        state["pairs"] = _round_robin(labels, rng, "round", 1)


def active_labels(state: dict) -> list[str]:
    labels = sorted(state["labels"], key=lambda s: (len(s), s))
    return state["selected"] or labels


def weighted_rating(ratings: dict, weights: dict) -> float | None:
    """Weighted mean of the 1-10 marks that were actually given (unrated criteria are ignored)."""
    num = den = 0.0
    for key, _ in CRITERIA:
        val = ratings.get(key)
        w = float(weights.get(key, 1.0))
        if val and w > 0:
            num += w * float(val)
            den += w
    return num / den if den else None


def set_rating(state: dict, label: str, criterion: str, value: int | None) -> None:
    if criterion not in CRITERIA_NAMES:
        raise VersusError(f"неизвестный критерий {criterion}")
    if value is not None and not 1 <= int(value) <= 10:
        raise VersusError("оценка должна быть от 1 до 10")
    card = state["ratings"].setdefault(label, {})
    if value is None:
        card.pop(criterion, None)
    else:
        card[criterion] = int(value)


def prelim_scores(state: dict, settings: dict) -> dict[str, float | None]:
    out = {}
    for label in active_labels(state):
        r = weighted_rating(state["ratings"].get(label, {}), settings["weights"])
        pen = sum(p["points"] for p in version_of(state, label)["penalties"])
        out[label] = None if r is None else 100 * (r - 1) / 9 - settings["tech_weight"] * pen
    return out


def advance_tournament(state: dict, settings: dict) -> list[str]:
    """Stage 1 -> stage 2: the best-rated versions (ratings minus technical penalties) meet in a full round."""
    if state["stage"] != "prelim":
        raise VersusError("Предварительный этап уже завершён.")
    scores = prelim_scores(state, settings)
    missing = [k for k, v in scores.items() if v is None]
    if missing:
        raise VersusError("Оцените каждую версию хотя бы по одному критерию: " + ", ".join(missing))
    order = sorted(scores, key=lambda k: (-scores[k], k))
    finalists = sorted(order[:FINALISTS], key=lambda s: (len(s), s))
    state["finalists"] = finalists
    state["prelim_scores"] = {k: round(v, 2) for k, v in scores.items()}
    state["stage"] = "round"
    state["pairs"] = _round_robin(finalists, random.Random(state["seed"] + 2), "round", 1)
    return finalists


def next_pair(state: dict) -> dict | None:
    return next((p for p in state["pairs"] if p["result"] is None), None)


def record_pair(state: dict, pair_id: int, result: str) -> None:
    """``result``: the winning label, or 'draw'."""
    pair = next((p for p in state["pairs"] if p["id"] == pair_id), None)
    if pair is None:
        raise VersusError("сравнение не найдено")
    if result not in (pair["a"], pair["b"], "draw"):
        raise VersusError("результат должен быть одним из двух вариантов или «равны»")
    pair["result"], pair["decided_at"] = result, time.time()
    state["final_confirmed"] = False


# ------------------------------------------------------------------ rating
def standings(state: dict, settings: dict) -> list[dict]:
    """One row per blind label, best first. The final score never looks at loudness."""
    rows = {}
    contenders = set(state.get("finalists") or active_labels(state))
    for label in active_labels(state):
        v = version_of(state, label)
        rows[label] = {"label": label, "version_id": v["id"], "wins": 0, "draws": 0, "losses": 0, "played": 0,
                       "contender": label in contenders,
                       "rating": weighted_rating(state["ratings"].get(label, {}), settings["weights"]),
                       "penalty": sum(p["points"] for p in v["penalties"])}
    for p in state["pairs"]:
        if p["result"] is None or p["stage"] != "round":
            continue
        a, b = rows[p["a"]], rows[p["b"]]
        a["played"] += 1
        b["played"] += 1
        if p["result"] == "draw":
            a["draws"] += 1
            b["draws"] += 1
        else:
            win, lose = (a, b) if p["result"] == p["a"] else (b, a)
            win["wins"] += 1
            lose["losses"] += 1
    share = float(settings["pair_share"])
    for r in rows.values():
        pair_rate = (r["wins"] + 0.5 * r["draws"]) / r["played"] if r["played"] else None
        rating_norm = None if r["rating"] is None else (r["rating"] - 1) / 9
        if pair_rate is not None and rating_norm is not None:
            comp = share * pair_rate + (1 - share) * rating_norm
        elif pair_rate is not None:
            comp = pair_rate
        elif rating_norm is not None:
            comp = rating_norm
        else:
            comp = 0.0
        r["pair_rate"] = pair_rate
        r["score"] = round(max(0.0, 100 * comp - float(settings["tech_weight"]) * r["penalty"]), 2)
        r["rating"] = None if r["rating"] is None else round(r["rating"], 2)
    ordered = sorted(rows.values(), key=lambda r: (not r["contender"], -r["score"], -r["wins"], r["penalty"], r["label"]))
    for place, r in enumerate(ordered, start=1):
        r["place"] = place
    return ordered


def _head_to_head(state: dict, x: str, y: str) -> str | None:
    for p in state["pairs"]:
        if p["stage"] == "round" and {p["a"], p["b"]} == {x, y} and p["result"]:
            return p["result"]
    return None


def technical_winner(state: dict) -> list[int]:
    """Version ids with the best technical score among admitted versions (several on a tie)."""
    ok = [v for v in state["versions"] if v["status"] == OK]
    if not ok:
        return []
    best = max(v["tech_score"] for v in ok)
    return [v["id"] for v in ok if v["tech_score"] == best]


def evaluate(state: dict, settings: dict) -> dict:
    """Current verdict. Adds the final A/B pair when the leaders are too close to call."""
    labels = active_labels(state)
    res: dict = {"status": NOT_STARTED, "winner": None, "second": None, "confidence": None, "margin": None,
                 "needs_final": False, "standings": [], "technical_winner": technical_winner(state),
                 "override": state.get("override"), "note": None}
    if not labels:
        res["note"] = "Нет допущенных версий: сравнивать нечего."
        return res
    if len(labels) == 1:
        res.update(status=WINNER, winner=labels[0], confidence=100, standings=standings(state, settings),
                   note="Допущена только одна версия — она и выбрана.")
        return _finish(state, res)
    table = standings(state, settings)
    res["standings"] = table
    started = any(p["result"] for p in state["pairs"]) or any(state["ratings"].values())
    if state["stage"] == "prelim" or next_pair(state) and not any(p["stage"] == "final" for p in state["pairs"]):
        res["status"] = IN_PROGRESS if started else NOT_STARTED
        return res
    first, second = table[0], table[1]
    margin = first["score"] - second["score"]
    h2h = _head_to_head(state, first["label"], second["label"])
    contradict = h2h == second["label"]
    final = next((p for p in state["pairs"] if p["stage"] == "final"), None)
    close = margin < float(settings["close_margin"]) or (contradict and margin < 2 * float(settings["close_margin"]))
    confidence = 50 + 49 * (1 - math.exp(-margin / 8.0)) - (10 if contradict else 0)
    winner, runner = first["label"], second["label"]
    if final and {final["a"], final["b"]} != {winner, runner} and final["result"] is None:
        state["pairs"].remove(final)  # the leaders changed (ratings were edited): the old final is void
        final = None
    if close and final is None:
        state["pairs"].append({"id": max((p["id"] for p in state["pairs"]), default=0) + 1, "a": first["label"],
                               "b": second["label"], "stage": "final", "result": None, "decided_at": None})
        final = state["pairs"][-1]
    if final is not None:
        if final["result"] in (final["a"], final["b"]):
            winner = final["result"]
            runner = final["b"] if winner == final["a"] else final["a"]
            confidence = max(60.0, min(75.0, confidence + 10))
            res["note"] = "Лидеры набрали почти одинаково; победителя определило финальное A/B-сравнение."
        elif not state.get("override"):
            res.update(status=NEED_FINAL, needs_final=True, margin=round(margin, 2), second=runner, winner=None,
                       note="ТРЕБУЕТСЯ ФИНАЛЬНОЕ A/B-СРАВНЕНИЕ" if final["result"] is None else
                       "Финальное сравнение не выявило лучшего — выберите победителя вручную.")
            return res
    res.update(status=WINNER, winner=winner, second=runner, margin=round(margin, 2),
               confidence=int(round(max(50.0, min(99.0, confidence)))))
    return _finish(state, res)


def _finish(state: dict, res: dict) -> dict:
    ov = state.get("override")
    if ov and ov["label"] != res["winner"]:
        res["auto_winner"] = res["winner"]
        res["second"] = res["winner"] or res.get("second")
        res["winner"] = ov["label"]
    if state.get("applied"):
        res["status"] = APPLIED
    res["explanation"] = explain(state, res)
    return res


def set_override(state: dict, label: str, reason: str, auto_winner: str | None) -> None:
    """The user's own choice of winner. Always recorded and shown in the report."""
    if label not in state["labels"]:
        raise VersusError("нет такого варианта")
    state["override"] = None if label == auto_winner else {
        "label": label, "previous_winner": auto_winner, "reason": (reason or "").strip() or "без комментария",
        "at": time.time()}


def explain(state: dict, res: dict) -> list[str]:
    """Plain-language reasons for the result, with real file names (used only after the test)."""
    if not res.get("winner"):
        return []
    rows = {r["label"]: r for r in res.get("standings", [])}
    w = rows.get(res["winner"])
    v = version_of(state, res["winner"])
    lines = []
    if w and w["played"]:
        lines.append(f"В попарных сравнениях: побед {w['wins']}, ничьих {w['draws']}, поражений {w['losses']}.")
    card = state["ratings"].get(res["winner"], {})
    if card:
        best = sorted(card.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Сильные стороны по вашим оценкам: " + ", ".join(
            f"{CRITERIA_NAMES[k].lower()} ({val})" for k, val in best) + ".")
        if w and w["rating"] is not None:
            lines.append(f"Средняя взвешенная оценка: {w['rating']:.1f} из 10.")
    if v["penalties"]:
        lines.append("Технические замечания (учтены штрафом): " + "; ".join(p["text"] for p in v["penalties"]) + ".")
    else:
        lines.append("Технических замечаний нет.")
    if res.get("second") and res["second"] in rows and w:
        s = rows[res["second"]]
        lines.append(f"Отрыв от второго места: {w['score'] - s['score']:+.1f} балла "
                     f"({w['score']:.1f} против {s['score']:.1f}).")
    lines.append("Громкость на результат не влияла: при прослушивании все версии были выровнены.")
    if res.get("note"):
        lines.append(res["note"])
    ov = state.get("override")
    if ov:
        prev = version_of(state, ov["previous_winner"])["name"] if ov.get("previous_winner") else "—"
        lines.append(f"Победитель выбран вручную (расчёт предлагал: {prev}). Причина: {ov['reason']}.")
    return lines


# ------------------------------------------------------------------ playback levels
def playback_gains_db(state: dict) -> dict[str, float]:
    """Loudness matching for listening only: every version is turned DOWN to the quietest one, so
    nothing can clip and a louder version never sounds 'better'. Files are not touched."""
    lufs = {lab: version_of(state, lab)["tech"].get("integrated_lufs") for lab in state["labels"]}
    known = [v for v in lufs.values() if v is not None]
    if not known:
        return {lab: 0.0 for lab in lufs}
    target = min(known)
    return {lab: (0.0 if v is None else round(target - v, 2)) for lab, v in lufs.items()}


def load_playback(path: str | Path, rate: int = 48000) -> np.ndarray:
    """Decode to int16 stereo at ``rate`` in memory (for instant switching). Read-only."""
    pr = probe(path)
    if not pr.ok:
        raise VersusError(f"файл не читается: {Path(path).name}")
    dec = decode(path, sample_rate=pr.sample_rate, channels=min(pr.channels or 2, 2),
                 target_rate=rate if pr.sample_rate != rate else None)
    x = dec.audio
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)


# ------------------------------------------------------------------ report
def _fmt(v, nd=1, unit=""):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def build_report(state: dict, settings: dict) -> dict:
    res = evaluate(state, settings)
    rows = {r["label"]: r for r in res.get("standings", [])}
    label_of = {vid: lab for lab, vid in state["labels"].items()}
    versions = []
    for v in state["versions"]:
        lab = label_of.get(v["id"])
        r = rows.get(lab, {})
        versions.append({
            "version": v["id"], "blind_label": lab, "file": v["name"], "path": v["path"], "sha256": v["sha256"],
            "status": v["status"], "duplicate_of": v["duplicate_of"], "error": v["error"], "technical": v["tech"],
            "technical_score": v["tech_score"], "penalties": v["penalties"],
            "ratings": state["ratings"].get(lab, {}) if lab else {},
            "weighted_rating": r.get("rating"), "wins": r.get("wins"), "draws": r.get("draws"),
            "losses": r.get("losses"), "final_score": r.get("score"), "place": r.get("place"),
        })

    def vid(label):
        return state["labels"][label] if label else None

    return {
        "song": state["group"]["name"], "folder": state["group"]["folder"], "created_at": state["created_at"],
        "reported_at": time.time(), "mode": MODES.get(state["mode"], state["mode"]), "status": res["status"],
        "winner_version": vid(res.get("winner")), "second_version": vid(res.get("second")),
        "confidence_percent": res.get("confidence"), "margin_points": res.get("margin"),
        "technical_winner_versions": res.get("technical_winner"), "explanation": res.get("explanation", []),
        "blind_mapping": {lab: {"version": v, "file": version_of(state, lab)["name"]}
                          for lab, v in state["labels"].items()},
        "pairs": [{"stage": p["stage"], "a": p["a"], "b": p["b"], "result": p["result"],
                   "a_version": vid(p["a"]), "b_version": vid(p["b"]),
                   "winner_version": None if p["result"] in (None, "draw") else vid(p["result"])}
                  for p in state["pairs"]],
        "manual_override": state.get("override"), "applied": state.get("applied"),
        "criteria_weights": settings["weights"], "settings": {k: settings[k] for k in (
            "pair_share", "tech_weight", "close_margin", "min_duration_s")},
        "versions": versions,
    }


def report_text(rep: dict) -> str:
    by_id = {v["version"]: v for v in rep["versions"]}

    def name(vid):
        return "—" if vid is None else f"ВЕРСИЯ {vid} ({by_id[vid]['file']})"

    out = [f"РЕЗУЛЬТАТ СРАВНЕНИЯ: {rep['song']}", f"Папка: {rep['folder']}",
           f"Дата: {time.strftime('%d.%m.%Y %H:%M', time.localtime(rep['reported_at']))}",
           f"Режим: {rep['mode']}", f"Статус: {rep['status']}", "",
           f"ПОБЕДИТЕЛЬ: {name(rep['winner_version'])}", f"ВТОРОЕ МЕСТО: {name(rep['second_version'])}",
           f"УВЕРЕННОСТЬ: {'—' if rep['confidence_percent'] is None else str(rep['confidence_percent']) + '%'}",
           "ТЕХНИЧЕСКИЙ ПОБЕДИТЕЛЬ: " + (", ".join(name(v) for v in rep["technical_winner_versions"]) or "—"), ""]
    if rep["manual_override"]:
        ov = rep["manual_override"]
        out += ["РУЧНОЕ ИЗМЕНЕНИЕ ПОБЕДИТЕЛЯ: да", f"  Причина: {ov['reason']}",
                f"  Когда: {time.strftime('%d.%m.%Y %H:%M', time.localtime(ov['at']))}", ""]
    out += ["ПОЧЕМУ ТАК:"] + [f"  - {line}" for line in rep["explanation"]] + ["", "ВАРИАНТЫ:"]
    for v in rep["versions"]:
        t = v["technical"]
        out.append(f"  ВЕРСИЯ {v['version']} — {v['file']}  [{v['status']}]"
                   + (f"  обозначение в тесте: {v['blind_label']}" if v["blind_label"] else ""))
        out.append(f"    Путь: {v['path']}")
        out.append(f"    SHA-256: {v['sha256']}")
        if v["error"]:
            out.append(f"    Ошибка: {v['error']}")
        if v["duplicate_of"]:
            out.append(f"    Точная копия версии {v['duplicate_of']}")
        if t.get("duration_s") is not None:
            m, s = divmod(int(t["duration_s"]), 60)
            quality = f"{t['bits_per_sample']} бит" if t.get("bits_per_sample") else (
                f"{t['bit_rate'] // 1000} кбит/с" if t.get("bit_rate") else "—")
            out.append(f"    Длительность {m}:{s:02d} · {t.get('codec')} · {t.get('sample_rate')} Гц · {quality}")
            out.append(f"    Громкость {_fmt(t.get('integrated_lufs'))} LUFS · пик {_fmt(t.get('true_peak_dbtp'))} dBTP"
                       f" · диапазон громкости {_fmt(t.get('lra_lu'))} LU · PLR {_fmt(t.get('plr_db'))} dB")
            out.append(f"    Техническая оценка: {v['technical_score']} из 100"
                       + ("; штрафы: " + "; ".join(f"{p['text']} (−{p['points']})" for p in v["penalties"])
                          if v["penalties"] else "; замечаний нет"))
        if v["place"]:
            out.append(f"    Место {v['place']} · итог {_fmt(v['final_score'])} · побед {v['wins']}, ничьих "
                       f"{v['draws']}, поражений {v['losses']} · средняя оценка {_fmt(v['weighted_rating'])}")
        if v["ratings"]:
            out.append("    Оценки: " + ", ".join(f"{CRITERIA_NAMES[k]} {val}" for k, val in v["ratings"].items()))
    out += ["", "СООТВЕТСТВИЕ ОБОЗНАЧЕНИЙ (случайное):"]
    out += [f"  {lab} = ВЕРСИЯ {m['version']} ({m['file']})" for lab, m in rep["blind_mapping"].items()]
    out += ["", "ПОПАРНЫЕ СРАВНЕНИЯ:"]
    for p in rep["pairs"]:
        verdict = "не проведено" if p["result"] is None else ("равны" if p["result"] == "draw" else f"лучше {p['result']}")
        out.append(f"  {'ФИНАЛ: ' if p['stage'] == 'final' else ''}{p['a']} против {p['b']} "
                   f"(версии {p['a_version']} и {p['b_version']}): {verdict}")
    if rep["applied"]:
        out += ["", "ПРИМЕНЕНО:"] + [f"  {op['src']}  ->  {op['dst']}" for op in rep["applied"]["operations"]]
        if not rep["applied"]["operations"]:
            out.append("  файлы оставлены на месте, сохранён только отчёт")
    return "\n".join(out) + "\n"


def report_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    cols = ["Версия", "Обозначение", "Файл", "Статус", "Место", "Итог", "Побед", "Ничьих", "Поражений",
            "Средняя оценка", "Техническая оценка", "Штрафы", "Длительность, с", "Кодек", "Частота, Гц", "Бит",
            "Битрейт", "LUFS", "True peak, dBTP", "LRA, LU", "PLR, dB", "SHA-256", "Путь"]
    cols += [name for _, name in CRITERIA]
    w.writerow(cols)
    for v in rep["versions"]:
        t = v["technical"]
        row = [v["version"], v["blind_label"] or "", v["file"], v["status"], v["place"] or "", v["final_score"],
               v["wins"], v["draws"], v["losses"], v["weighted_rating"], v["technical_score"],
               "; ".join(f"{p['text']} (-{p['points']})" for p in v["penalties"]), t.get("duration_s"),
               t.get("codec"), t.get("sample_rate"), t.get("bits_per_sample"), t.get("bit_rate"),
               t.get("integrated_lufs"), t.get("true_peak_dbtp"), t.get("lra_lu"), t.get("plr_db"), v["sha256"],
               v["path"]]
        row += [v["ratings"].get(k, "") for k, _ in CRITERIA]
        w.writerow(["" if c is None else c for c in row])
    return "﻿" + buf.getvalue()  # BOM: Excel opens Cyrillic correctly


def write_reports(state: dict, settings: dict, folder: str | Path | None = None) -> dict:
    """Write TXT, CSV and JSON next to the versions (new files only; audio is not touched)."""
    folder = Path(folder or state["group"]["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    rep = build_report(state, settings)
    paths = {"txt": folder / REPORT_TXT, "csv": folder / REPORT_CSV, "json": folder / REPORT_JSON}
    atomic_write_text(paths["txt"], report_text(rep), overwrite_generated=True)
    atomic_write_text(paths["csv"], report_csv(rep), overwrite_generated=True)
    atomic_write_text(paths["json"], json.dumps(rep, ensure_ascii=False, indent=1), overwrite_generated=True)
    return {k: str(v) for k, v in paths.items()}


# ------------------------------------------------------------------ apply
def winner_filename(title: str, ext: str, number: int | None) -> str:
    base = safe_name(title, 120)
    return (f"{int(number):02d} {base}" if number is not None else base) + ext.lower()


def plan_apply(state: dict, winner_label: str, *, rename: bool = False, number: int | None = None,
               title: str | None = None, album_folder: str | Path | None = None,
               defer_losers: bool = False) -> list[dict]:
    """List of moves the chosen options imply. Nothing is done here; an empty list means 'report only'."""
    win = version_of(state, winner_label)
    src = Path(win["path"])
    ops = []
    dst_dir = Path(album_folder) if album_folder else src.parent
    dst_name = winner_filename(title or state["group"]["name"], src.suffix, number) if rename else src.name
    dst = dst_dir / dst_name
    if os.path.normcase(str(dst)) != os.path.normcase(str(src)) or dst.name != src.name:
        ops.append({"kind": "winner", "src": str(src), "dst": str(dst)})
    if defer_losers:
        hold = Path(state["group"]["folder"]) / DEFERRED_DIR
        for v in state["versions"]:
            if v["id"] != win["id"]:
                ops.append({"kind": "deferred", "src": v["path"], "dst": str(hold / v["name"])})
    return ops


def check_apply(state: dict, ops: list[dict]) -> list[str]:
    """Problems that make the plan unsafe (missing or changed source, occupied target)."""
    problems = []
    sha = {v["path"]: v["sha256"] for v in state["versions"]}
    targets = set()
    for op in ops:
        src, dst = Path(op["src"]), Path(op["dst"])
        if not src.is_file():
            problems.append(f"Файл не найден: {src}")
        elif sha.get(op["src"]) and sha256_file(src) != sha[op["src"]]:
            problems.append(f"Файл изменился после проверки: {src.name}")
        same = os.path.normcase(str(src)) == os.path.normcase(str(dst))
        if (dst.exists() and not same) or os.path.normcase(str(dst)) in targets:
            problems.append(f"Уже существует: {dst}")
        targets.add(os.path.normcase(str(dst)))
    return problems


def execute_apply(state: dict, ops: list[dict], settings: dict, *, confirmed: bool = False) -> dict:
    """Carry out a confirmed plan. Never overwrites and never deletes; a failure rolls the moves back."""
    if not confirmed:
        raise VersusError("Применение результата требует явного подтверждения.")
    problems = check_apply(state, ops)
    if problems:
        raise VersusError("Нельзя применить результат:\n" + "\n".join(problems))
    done = []
    try:
        for op in ops:
            dst = Path(op["dst"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.normcase(op["src"]) == os.path.normcase(op["dst"]):
                os.rename(op["src"], op["dst"])  # case-only rename
            else:
                shutil.move(op["src"], op["dst"])
            done.append(op)
    except Exception as exc:
        for op in reversed(done):
            try:
                shutil.move(op["dst"], op["src"])
            except OSError:
                pass
        raise VersusError(f"Не удалось переместить файлы, изменения отменены: {exc}") from exc
    moved = {op["src"]: op["dst"] for op in ops}
    for v in state["versions"]:
        if v["path"] in moved:
            v["original_path"], v["path"] = v["path"], moved[v["path"]]
            v["name"] = Path(v["path"]).name
    state["applied"] = {"at": time.time(), "operations": ops}
    state["applied"]["reports"] = write_reports(state, settings)
    return state["applied"]
