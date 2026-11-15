"""Album pipeline: the release workflow as ordered stages that can run one by one or all at once.

    analyze -> versions -> repair -> master -> release

Every stage works on the whole album, skips what is already done, never stops on one bad track
and reports in plain language. Source files are only read. Tracks that lose a version
comparison are *excluded from the release*, never moved or deleted.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from srm.analysis.genre import PROMPT_KEYWORDS
from srm.dsp.presets import PRESETS

STAGES = [
    ("analyze", "Анализ"),
    ("versions", "Версии"),
    ("repair", "Ремонт"),
    ("master", "Мастеринг"),
    ("release", "Релиз"),
]
STAGE_NAMES = dict(STAGES)
MAX_AUTO_CLICKS = 8        # more "clicks" than this in one track is rhythm, not damage: leave it alone
MIN_RELEASE_S = 120.0
COVER_NAMES = ("cover", "folder", "front", "обложка")
COVER_EXT = (".png", ".jpg", ".jpeg", ".webp")
SERVICE_DIRS = ["_*", "renders", "previews", "releases", "reports", "logs", "artwork"]

STYLE_LABELS = {
    "generic": "Универсальный", "liquid_dnb": "Drum & Bass", "ambient_jazz": "Эмбиент / джаз",
    "acoustic_ballad": "Акустика / баллада", "pop_electronic": "Поп / электроника",
    "hybrid_ethnic": "Этно / гибрид", "rock": "Рок", "hiphop": "Хип-хоп / трэп",
}
_STYLE_LINE = re.compile(r"(?im)^\s*(?:стиль|жанр|style|genre|styles?\s+of\s+music)\s*[:：]\s*(.+)$")
# Explicit version markers only. A bare number at the end ("Part 2", "Hotel 2") is NOT treated as a
# version inside an album: two different songs must never be merged automatically.
_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*[—–\-_.)]+\s*(?=\S)")
_VERSION_MARK = re.compile(
    r"(\s*[\(\[]\s*\d+\s*[\)\]]|[\s_\-]+(?:v|ver|version|take|alt|вариант|версия|копия|copy)[\s_\-]*\d*)$", re.I)


def style_from_text(text: str | None) -> str | None:
    """Preset named by a text, or None. Whole words only; several different styles -> undecided."""
    if not text:
        return None
    low = text.casefold()
    hits = {}
    for style, words in PROMPT_KEYWORDS.items():
        n = sum(len(re.findall(rf"(?<!\w){re.escape(w.casefold())}(?!\w)", low)) for w in words)
        if n:
            hits[style] = n
    if not hits:
        return None
    best = max(hits.values())
    top = [s for s, n in hits.items() if n == best]
    return top[0] if len(top) == 1 else None


def version_key(file_name: str) -> str:
    """'03 - Song (2).mp3' and '03 - Song.mp3' -> 'song'. Used to find versions of one song in an album."""
    s = _TRACK_NO.sub("", Path(file_name).stem.strip())
    prev = None
    while prev != s:
        prev = s
        s = _VERSION_MARK.sub("", s).strip()
    return s.casefold()


class PipelineMixin:
    name_root_album_by_folder = False

    # ------------------------------------------------------------------ style
    def _album_row(self, album) -> dict:
        aid = self.album_id(album)
        row = self.db.one("SELECT * FROM albums WHERE id=?", (aid,))
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        return d

    def album_style(self, album) -> dict:
        """Style of an album and where it came from: the user's choice, a 'СТИЛЬ:' line in the album text
        files, or the album name. Free prose is not used - a casual word must not change the loudness."""
        a = self._album_row(album)
        manual = a["metadata"].get("style_preset")
        if manual in PRESETS:
            return {"preset": manual, "source": "выбран вами"}
        folder = Path(a["folder_hint"]) if a["folder_hint"] else None
        if folder and folder.is_dir():
            from srm.ingest.textmatch import read_text

            for f in sorted(folder.glob("*.txt")) + sorted(folder.glob("*.md")):
                try:
                    if f.stat().st_size > 200_000:
                        continue
                    m = _STYLE_LINE.search(read_text(f)[0])
                except OSError:
                    continue
                preset = style_from_text(m.group(1)) if m else None
                if preset:
                    return {"preset": preset, "source": f"из файла {f.name}"}
        preset = style_from_text(Path(a["name"]).name)
        if preset:
            return {"preset": preset, "source": "по названию альбома"}
        return {"preset": None, "source": "не указан"}

    def set_album_style(self, album, preset: str | None) -> None:
        if preset is not None and preset not in PRESETS:
            raise ValueError(f"unknown preset {preset}")
        aid = self.album_id(album)
        self._mutate_meta("albums", aid, lambda m: m.__setitem__("style_preset", preset) if preset
                          else m.pop("style_preset", None))
        self.db.audit("album_style", {"album_id": aid, "preset": preset})

    def resolve_style(self, t: dict, album_style: dict | None = None) -> tuple[str | None, str]:
        """(preset, where it came from) for one track: manual track style, its own prompt file, the album."""
        if t.get("style_override") in PRESETS:
            return t["style_override"], "задан для трека"
        parsed = (t.get("text") or {}).get("parsed") or {}
        preset = style_from_text(" ".join(filter(None, [parsed.get("style_prompt"), parsed.get("declared_genre")])))
        if preset:
            return preset, "из текстового файла трека"
        if t.get("album_id") is None:
            return None, "не указан"
        st = album_style or self.album_style(t["album_id"])
        return st["preset"], st["source"]

    # ------------------------------------------------------------------ exclusion
    def set_track_excluded(self, track_id: int, excluded: bool, reason: str = "") -> None:
        """Keep a track out of the release (and of repair/mastering). Its file is not touched."""
        def change(meta):
            if excluded:
                meta["release_excluded"] = {"reason": reason or "исключён вручную", "at": time.time()}
            else:
                meta.pop("release_excluded", None)
        self._mutate_meta("tracks", int(track_id), change)
        self.db.audit("track_excluded", {"track_id": track_id, "excluded": excluded, "reason": reason})

    def release_tracks(self, album) -> list[dict]:
        return [t for t in self.list_tracks(album) if not t["metadata"].get("release_excluded")]

    # ------------------------------------------------------------------ status
    def _repair_state(self, track_id: int, sha: str) -> dict | None:
        r = self.db.one("SELECT plan_json FROM repair_plans WHERE track_id=? AND source_sha=? AND status='APPROVED' "
                        "ORDER BY id DESC LIMIT 1", (track_id, sha))
        if not r:
            return None
        plan = json.loads(r["plan_json"])
        return {"applied": len([e for e in plan.get("edits", []) if e.get("decision") in ("APPROVED", "AUTO_ACCEPTED")]),
                "summary": plan.get("summary") or ""}

    def pipeline_status(self, album) -> dict:
        """Everything the interface needs about one album: per-track state and per-stage progress."""
        aid = self.album_id(album)
        a = self._album_row(aid)
        ast = self.album_style(aid)
        groups = self.album_version_groups(aid)
        in_group = {tid: key for key, ids in groups.items() for tid in ids}
        rows = []
        for t in self.list_tracks(aid):
            full = self.track(t["id"])
            preset, source = self.resolve_style(full, ast)
            excl = t["metadata"].get("release_excluded")
            rep = self._repair_state(t["id"], t["sha256"])
            lv = t["latest_version"]
            mastered = bool(lv and lv["status"] in ("RENDERED", "APPROVED"))
            br = full["probe"].get("bit_rate")
            rows.append({
                "id": t["id"], "track_no": t["track_no"], "title": t["title"], "file": Path(t["path"]).name,
                "codec": t["codec"], "is_lossy": bool(t["is_lossy"]), "bit_rate": br, "bits": t["bits"],
                "sample_rate": t["sample_rate"], "duration_s": t["duration_s"],
                "style": preset, "style_label": STYLE_LABELS.get(preset or "generic", preset), "style_source": source,
                "analyzed": bool(t["has_analysis"]), "version_group": in_group.get(t["id"]),
                "excluded": bool(excl), "excluded_reason": (excl or {}).get("reason"),
                "repair": rep, "mastered": mastered, "approved": bool(lv and lv["status"] == "APPROVED"),
                "version": lv, "missing": bool(t["missing"]),
            })
        active = [r for r in rows if not r["excluded"]]
        unresolved_groups = [k for k, ids in groups.items()
                             if len([r for r in active if r["id"] in ids]) > 1]
        exp = self.db.one("SELECT path, status, created_at FROM exports WHERE album_id=? ORDER BY id DESC LIMIT 1",
                          (aid,))
        stages = {
            "analyze": {"done": sum(r["analyzed"] for r in rows), "total": len(rows)},
            "versions": {"done": len(groups) - len(unresolved_groups), "total": len(groups)},
            "repair": {"done": sum(r["repair"] is not None for r in active), "total": len(active)},
            "master": {"done": sum(r["mastered"] for r in active), "total": len(active)},
            "release": {"done": 1 if exp and Path(exp["path"]).is_dir() else 0, "total": 1},
        }
        return {"album": {"id": aid, "name": a["name"], "title": Path(a["name"]).name, "folder": a["folder_hint"],
                          "style": ast, "export": dict(exp) if exp else None},
                "tracks": rows, "stages": stages}

    def albums_overview(self) -> list[dict]:
        """One light row per album for the album list (no per-track work)."""
        rows = self.db.query(
            "SELECT a.id, a.name, a.folder_hint, COUNT(t.id) n, "
            "SUM(CASE WHEN EXISTS(SELECT 1 FROM versions v WHERE v.track_id=t.id AND v.status IN "
            "('RENDERED','APPROVED')) THEN 1 ELSE 0 END) mastered, "
            "(SELECT COUNT(*) FROM exports e WHERE e.album_id=a.id) exports "
            "FROM albums a JOIN tracks t ON t.album_id=a.id GROUP BY a.id ORDER BY a.name")
        return [{"id": r["id"], "name": r["name"], "title": Path(r["name"]).name, "tracks": r["n"],
                 "mastered": r["mastered"] or 0, "released": bool(r["exports"])} for r in rows]

    # ------------------------------------------------------------------ versions
    def album_version_groups(self, album) -> dict[str, list[int]]:
        """Tracks of one album that are versions of the same song (explicit version marks or identical files)."""
        by_key: dict[str, list[int]] = {}
        by_sha: dict[str, list[int]] = {}
        for t in self.list_tracks(album):
            by_key.setdefault(version_key(Path(t["path"]).name), []).append(t["id"])
            by_sha.setdefault(t["sha256"], []).append(t["id"])
        groups = {k: ids for k, ids in by_key.items() if len(ids) > 1}
        for sha, ids in by_sha.items():
            if len(ids) > 1 and not any(set(ids) <= set(g) for g in groups.values()):
                groups[f"копия {sha[:8]}"] = ids
        return groups

    def _technical_score(self, track_id: int) -> tuple[float, list[str]]:
        from srm.versus.core import technical_penalties

        t = self.track(track_id)
        a = self.get_analysis(track_id) or self.analyze_track(track_id)
        m = a["measurements"]
        clips = [c for c in m["defects"]["candidates"] if c["type"] == "hard_clipping"]
        tech = {
            "clipping_events": len(clips),
            "clipping_longest_run": max((c["evidence"].get("longest_run_samples", 0) for c in clips), default=0),
            "true_peak_dbtp": m["peaks"]["true_peak_dbtp"], "plr_db": m["dynamics"].get("plr_db"),
            "abrupt_end": m["silence"].get("abrupt_end"), "abrupt_start": m["silence"].get("abrupt_start"),
            "leading_silence_s": m["silence"].get("leading_silence_s"),
            "trailing_silence_s": m["silence"].get("trailing_silence_s"),
            "is_lossy": bool(t["is_lossy"]), "bit_rate": t["probe"].get("bit_rate"),
            "sample_rate": t["sample_rate"], "channels": t["channels"],
            "decode_warnings": len((m.get("decode") or {}).get("warnings") or []),
        }
        pens = technical_penalties(tech)
        score = 100.0 - sum(p["points"] for p in pens)
        notes = [p["text"] for p in pens]
        if (m.get("duration_s") or 0) < MIN_RELEASE_S:
            score -= 40
            notes.append("короче двух минут")
        return score, notes

    def auto_pick_versions(self, album) -> list[dict]:
        """For every group of versions keep the technically best one and exclude the rest from the release.

        This is a technical choice (damage, clipping, ending, dynamics, source quality) - loudness does
        not count and musical taste cannot be automated; the blind A/B tool is there for listening."""
        out = []
        for key, ids in self.album_version_groups(album).items():
            scored = []
            for tid in ids:
                t = self.track(tid)
                if t["metadata"].get("release_excluded", {}).get("reason", "").startswith("исключён вручную"):
                    continue
                score, notes = self._technical_score(tid)
                scored.append((score, not t["is_lossy"], t["probe"].get("bit_rate") or 0, -tid, tid, t["title"], notes))
            if len(scored) < 2:
                continue
            scored.sort(reverse=True)
            best = scored[0]
            self.set_track_excluded(best[4], False)
            for s in scored[1:]:
                self.set_track_excluded(s[4], True, f"версия: выбрана «{Path(self.track(best[4])['path']).name}» "
                                                    f"(оценка {best[0]:.0f} против {s[0]:.0f})")
            out.append({"song": key, "kept": best[4], "kept_title": best[5], "kept_score": best[0],
                        "excluded": [{"id": s[4], "score": s[0], "notes": s[6]} for s in scored[1:]]})
        return out

    # ------------------------------------------------------------------ repair
    def auto_repair_track(self, track_id: int, *, force: bool = False) -> dict:
        """Automatic repair of one track: isolated clicks and a clean start/ending, nothing else.

        Only HIGH-confidence clicks are interpolated, each one has to pass the regression checks, and a
        track with many such events is left alone (dozens of 'clicks' are drums, not damage)."""
        from srm.dsp.repair import apply_edit, plan_from_candidates, regression_check
        from srm.service.engine import _jdump

        t = self.track(track_id)
        if not force and self._repair_state(t["id"], t["sha256"]) is not None:
            return {"track_id": track_id, "skipped": True}
        a = self.get_analysis(track_id) or self.analyze_track(track_id)
        d = a["measurements"]["defects"]
        plan = plan_from_candidates(d["candidates"], d["listening_required"])
        clicks = [e for e in plan["edits"] if e["method"] == "lsar_interpolation" and e["auto_eligible"]]
        fades = [e for e in plan["edits"] if e["method"] in ("fade_in", "fade_out")]
        notes = []
        if len(clicks) > MAX_AUTO_CLICKS:
            notes.append(f"резких импульсов много ({len(clicks)}) — это ритм, а не дефекты: не трогаем")
            clicks = []
        wanted = {e["edit_id"] for e in clicks + fades}
        applied = {"click": 0, "fade": 0}
        if wanted:
            x, fs = self._source_float(t)
            for e in plan["edits"]:
                if e["edit_id"] not in wanted:
                    continue
                if t["channels"] == 1:
                    e["channel"] = None
                try:
                    y = apply_edit(x, fs, e)
                    e["regression"] = regression_check(x, y, fs, e)
                except Exception as exc:
                    e["regression"] = {"passed": False, "error": str(exc)}
                if e["regression"].get("passed"):
                    e["decision"] = "APPROVED"
                    applied["click" if e["method"] == "lsar_interpolation" else "fade"] += 1
        for e in plan["edits"]:
            if e["decision"] != "APPROVED":
                e["decision"] = "REJECTED"
        if applied["click"]:
            notes.append(f"убрано щелчков: {applied['click']}")
        if applied["fade"]:
            notes.append("сглажено резкое начало/окончание")
        if not notes:
            notes.append("дефектов для ремонта нет")
        plan.update(auto=True, summary="; ".join(notes))
        self.db.execute("INSERT INTO repair_plans (track_id, source_sha, plan_json, status, created_at) VALUES (?,?,?,?,?)",
                        (track_id, t["sha256"], _jdump(plan), "APPROVED", time.time()))
        self.db.audit("auto_repair", {"track_id": track_id, **applied})
        return {"track_id": track_id, "skipped": False, "applied": applied["click"] + applied["fade"],
                "summary": plan["summary"]}

    # ------------------------------------------------------------------ master
    def auto_master_track(self, track_id: int, *, force: bool = False) -> dict:
        """Render the release-sounding version: repairs first (if any), then the bounded mastering chain
        with the preset of the declared style."""
        t = self.track(track_id)
        if not force:
            lv = self.db.one("SELECT version_no, config_json FROM versions WHERE track_id=? AND status IN "
                             "('RENDERED','APPROVED') ORDER BY version_no DESC LIMIT 1", (track_id,))
            if lv and json.loads(lv["config_json"]).get("source_sha256", t["sha256"]) == t["sha256"]:
                return {"track_id": track_id, "skipped": True, "version_no": lv["version_no"]}
        rp = self.approved_repair_plan(track_id)
        mode = "RESTORE_AND_MASTER" if rp and rp["edits"] else "SAFE_MASTER"
        prop = self.suggest_mastering(track_id, mode=mode)
        rep = self.render_version(track_id, prop["version_no"])
        om = rep["output_measurements"]
        return {"track_id": track_id, "skipped": False, "version_no": prop["version_no"], "preset": prop["preset"],
                "lufs": om["loudness"]["integrated_lufs"], "true_peak": om["peaks"]["true_peak_dbtp"],
                "policy": {k: v.get("status") for k, v in rep["policy_check"].items()}}

    # ------------------------------------------------------------------ release
    def find_folder_cover(self, album) -> Path | None:
        a = self._album_row(album)
        folder = Path(a["folder_hint"]) if a["folder_hint"] else None
        if not folder or not folder.is_dir():
            return None
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in COVER_EXT and f.stem.casefold() in COVER_NAMES:
                return f
        return None

    def build_release(self, album) -> dict:
        """WAV masters + MP3 with the cover + reports. A draft until a human has listened and approved."""
        aid = self.album_id(album)
        notes = []
        try:
            has_cover = bool(self.album_artwork(aid)["cover"])
        except ValueError:
            has_cover = False
        if not has_cover:
            cover = self.find_folder_cover(aid)
            if cover:
                try:
                    self.import_album_cover(aid, cover)
                    notes.append(f"обложка взята из {cover.name}")
                except Exception as exc:
                    notes.append(f"обложку {cover.name} использовать не удалось: {exc}")
        r = self.export_album(aid, profile="release_wav", include_mp3=True, allow_draft=True, confirm=True)
        return {"path": r["path"], "status": r["status"], "technical": r["technical"], "notes": notes,
                "unresolved": len(r["unresolved"])}

    def approve_album(self, album, note: str = "Альбом прослушан") -> int:
        """The user's own decision after listening: sign off and approve the mastered version of every track."""
        n = 0
        for t in self.release_tracks(album):
            lv = t["latest_version"]
            if lv and lv["status"] in ("RENDERED", "APPROVED"):
                self.listening_signoff(t["id"], lv["version_no"], note=note)
                self.approve(t["id"], lv["version_no"], note=note)
                n += 1
        return n

    # ------------------------------------------------------------------ runners
    def run_stage(self, album, stage: str, *, progress: Callable[[str, int, int, str], None] | None = None,
                  cancel: threading.Event | None = None, force: bool = False, workers: int | None = None) -> dict:
        """Run one stage for the whole album. Returns a summary; one failing track never stops the rest."""
        if stage not in STAGE_NAMES:
            raise ValueError(stage)
        aid = self.album_id(album)
        say = progress or (lambda *a: None)
        res = {"stage": stage, "name": STAGE_NAMES[stage], "done": 0, "skipped": 0, "failed": [], "messages": [],
               "cancelled": False}

        def per_track(tracks, fn, describe):
            total, lock, count = len(tracks), threading.Lock(), [0]
            say(stage, 0, total, f"{STAGE_NAMES[stage]}…")

            def work(t):
                if cancel is not None and cancel.is_set():
                    return
                try:
                    out = fn(t)
                    msg = describe(t, out)
                    with lock:
                        if out.get("skipped"):
                            res["skipped"] += 1
                        else:
                            res["done"] += 1
                            if msg:
                                res["messages"].append(f"{t['title']}: {msg}")
                except Exception as exc:
                    with lock:
                        res["failed"].append({"track_id": t["id"], "title": t["title"], "error": str(exc)[:300]})
                with lock:
                    count[0] += 1
                    say(stage, count[0], total, t["title"])

            n = max(1, workers or int(self.settings.get("workers", 2)))
            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(work, tracks))

        if stage == "analyze":
            def analyze(t):
                if t["has_analysis"] and not force:
                    return {"skipped": True}
                self.analyze_track(t["id"], force=force)
                return {"skipped": False}

            per_track(self.list_tracks(aid), analyze, lambda t, out: None)
        elif stage == "versions":
            say(stage, 0, 1, "Сравниваем версии…")
            picks = self.auto_pick_versions(aid)
            res["done"] = len(picks)
            for p in picks:
                res["messages"].append(f"«{p['kept_title']}»: оставлена лучшая версия, отложено: {len(p['excluded'])}")
            if not picks:
                res["messages"].append("повторных версий в альбоме нет")
            say(stage, 1, 1, "")
        elif stage == "repair":
            per_track(self.release_tracks(aid), lambda t: self.auto_repair_track(t["id"], force=force),
                      lambda t, out: out.get("summary"))
        elif stage == "master":
            per_track(self.release_tracks(aid), lambda t: self.auto_master_track(t["id"], force=force),
                      lambda t, out: (f"{out['lufs']:.1f} LUFS, пик {out['true_peak']:.1f} dBTP"
                                      + ("" if all(v in ("PASS", "WARN") for v in out["policy"].values())
                                         else " — проверьте: " + ", ".join(k for k, v in out["policy"].items()
                                                                           if v not in ("PASS", "WARN")))))
        else:
            say(stage, 0, 1, "Собираем пакет релиза…")
            try:
                r = self.build_release(aid)
            except Exception as exc:
                res["failed"].append({"track_id": None, "title": "Сборка релиза", "error": str(exc)[:500]})
            else:
                res["done"], res["release"] = 1, r
                res["messages"] += r["notes"]
                res["messages"].append(("Пакет собран, техническая проверка пройдена." if r["technical"] == "PASS"
                                        else "Пакет собран, но техническая проверка нашла ошибки.")
                                       + f" Папка: {r['path']}")
            say(stage, 1, 1, "")
        res["cancelled"] = bool(cancel is not None and cancel.is_set())
        return res

    def run_auto(self, album, *, progress: Callable[[str, int, int, str], None] | None = None,
                 cancel: threading.Event | None = None, on_stage: Callable[[dict], None] | None = None) -> list[dict]:
        """All stages in order. Stops before the release when mastering failed for some track."""
        out = []
        for stage, _name in STAGES:
            if cancel is not None and cancel.is_set():
                break
            if stage == "release" and any(r["failed"] for r in out if r["stage"] in ("analyze", "master")):
                out.append({"stage": stage, "name": STAGE_NAMES[stage], "done": 0, "skipped": 0, "failed": [],
                            "cancelled": False, "blocked": True,
                            "messages": ["Релиз не собран: сначала нужно разобраться с треками, на которых "
                                         "произошла ошибка (см. выше)."]})
                if on_stage:
                    on_stage(out[-1])
                break
            try:
                r = self.run_stage(album, stage, progress=progress, cancel=cancel)
            except Exception as exc:
                r = {"stage": stage, "name": STAGE_NAMES[stage], "done": 0, "skipped": 0, "cancelled": False,
                     "failed": [{"track_id": None, "title": STAGE_NAMES[stage], "error": str(exc)[:400]}],
                     "messages": []}
            out.append(r)
            if on_stage:
                on_stage(r)
            if r["cancelled"] or (stage == "release" and r["failed"]):
                break
        return out
