"""Engine facade: the single backend used by CLI, GUI and MCP.

Source files are only ever *read*. All outputs live under the project directory in
unique, versioned paths. Long operations run as restartable jobs (per track, per stage).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from srm import __version__
from srm.analysis.analyze import ANALYSIS_VERSION, analyze_audio, technical_status
from srm.analysis.report import strip_timelines, track_report_html
from srm.core.db import DB
from srm.core.paths import (
    PathSafetyError,
    atomic_write_text,
    free_disk_bytes,
    safe_name,
    sha256_file,
)
from srm.core.tools import get_tools, tools_status
from srm.ingest.decode import DecodeError, decode
from srm.ingest.probe import ProbeResult, probe
from srm.ingest.textmatch import match_texts, norm_title, parse_prompt_text, read_text
from srm.service.albums import AlbumMixin
from srm.service.artwork import ArtworkMixin
from srm.service.mastering import MasteringMixin
from srm.service.repairs import RepairMixin

AUDIO_EXT = {".m4a", ".mp4", ".aac", ".wav", ".wave", ".flac", ".mp3", ".aif", ".aiff", ".ogg", ".opus", ".alac"}
TEXT_EXT = {".txt", ".md"}
SKIP_DIRS = {".git", "$recycle.bin", "system volume information", "__pycache__", ".srm_project"}
MIN_FREE_BYTES = 512 * 1024 * 1024


class EngineError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _now() -> float:
    return time.time()


def _jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_default(o):
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(_jdump(cfg).encode("utf-8")).hexdigest()[:16]


class Engine(MasteringMixin, RepairMixin, AlbumMixin, ArtworkMixin):
    def __init__(self, project_dir: str | Path, *, create: bool = True):
        self.root = Path(project_dir).resolve()
        if not self.root.exists():
            if not create:
                raise EngineError(f"project does not exist: {self.root}")
            self.root.mkdir(parents=True)
        self.meta_path = self.root / "project.json"
        if not self.meta_path.exists():
            atomic_write_text(self.meta_path, _jdump({
                "name": self.root.name, "created_at": _now(), "app_version": __version__,
                "settings": {"workers": 2, "default_profile": "release_wav"},
            }))
        self.db = DB(self.root / "srm_project.sqlite")
        for sub in ("renders", "reports", "RELEASES", "logs", "previews"):
            (self.root / sub).mkdir(exist_ok=True)
        self._cancel_flags: dict[int, threading.Event] = {}

    # ------------------------------------------------------------------ meta
    @property
    def settings(self) -> dict:
        return json.loads(self.meta_path.read_text("utf-8")).get("settings", {})

    def health(self) -> dict:
        import platform

        return {
            "app_version": __version__,
            "analysis_version": ANALYSIS_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "project": str(self.root),
            "schema_version": self.db.schema_version,
            "ffmpeg": tools_status(),
            "capabilities": capabilities(),
            "network_required": False,
        }

    # ------------------------------------------------------------------ library roots
    def add_library_root(self, path: str | Path, exclude: list[str] | None = None) -> int:
        """Authorize a library root. ``exclude``: folder-name glob patterns skipped during scans (e.g. "_*")."""
        p = Path(path).resolve()
        if not p.is_dir():
            raise EngineError(f"not a directory: {p}")
        try:
            self.root.relative_to(p)
            # project inside library root is allowed; it's skipped during scan
        except ValueError:
            pass
        row = self.db.one("SELECT id FROM library_roots WHERE path=?", (str(p),))
        if row:
            if exclude is not None:
                self.db.execute("UPDATE library_roots SET exclude_json=? WHERE id=?", (_jdump(exclude), row["id"]))
            return row["id"]
        cur = self.db.execute("INSERT INTO library_roots (path, added_at, exclude_json) VALUES (?,?,?)",
                              (str(p), _now(), _jdump(exclude or [])))
        self.db.audit("add_library_root", {"path": str(p), "exclude": exclude or []})
        return cur.lastrowid

    def library_roots(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM library_roots ORDER BY id")]

    def resolve_root(self, root: str | int | None) -> dict:
        roots = self.library_roots()
        if root is None:
            if len(roots) != 1:
                raise EngineError("specify a library root id")
            return roots[0]
        for r in roots:
            if str(r["id"]) == str(root) or os.path.normcase(r["path"]) == os.path.normcase(str(Path(str(root)).resolve())):
                return r
        raise PathSafetyError("library root is not authorized in this project")

    # ------------------------------------------------------------------ scan
    def scan(self, root: str | int | None = None, *, progress: Callable[[int, int, str], None] | None = None,
             cancel: threading.Event | None = None, verify_decode: bool = False) -> dict:
        r = self.resolve_root(root)
        base = Path(r["path"])
        excl = [e.lower() for e in json.loads(r.get("exclude_json") or "[]")]
        audio, texts = [], []
        skipped_dirs = []
        for dirpath, dirnames, filenames in os.walk(base):
            dp = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                if any(fnmatch.fnmatch(d.lower(), pat) for pat in excl):
                    skipped_dirs.append(str((dp / d).relative_to(base)))
                elif d.lower() not in SKIP_DIRS and not d.startswith(".") and (dp / d).resolve() != self.root:
                    keep.append(d)
            dirnames[:] = keep
            for fn in sorted(filenames):
                ext = Path(fn).suffix.lower()
                if ext in AUDIO_EXT:
                    audio.append(dp / fn)
                elif ext in TEXT_EXT:
                    texts.append(dp / fn)
        summary = {"root": str(base), "excluded_dirs": skipped_dirs, "audio_files": len(audio), "text_files": len(texts), "new": 0,
                   "unchanged": 0, "changed": 0, "probe_failed": 0, "lossy": 0, "errors": []}
        seen = set()
        for i, ap in enumerate(audio):
            if cancel is not None and cancel.is_set():
                summary["cancelled"] = True
                break
            if progress:
                progress(i, len(audio), str(ap))
            try:
                self._ingest_audio(r["id"], base, ap, summary, verify_decode=verify_decode)
                seen.add(str(ap))
            except Exception as exc:  # per-file errors never abort the scan
                summary["errors"].append({"path": str(ap), "error": f"{type(exc).__name__}: {exc}"})
        # Mark missing files (never delete rows: provenance is kept)
        for row in self.db.query("SELECT id, path FROM sources WHERE root_id=?", (r["id"],)):
            if row["path"] not in seen and not Path(row["path"]).exists():
                self.db.execute("UPDATE sources SET missing=1 WHERE id=?", (row["id"],))
        self._ingest_texts(texts, audio)
        self._assign_duplicate_groups()
        summary["albums"] = len(self.list_albums())
        self.db.audit("scan", {k: v for k, v in summary.items() if k != "errors"} | {"errors": len(summary["errors"])})
        return summary

    def _ingest_audio(self, root_id: int, base: Path, ap: Path, summary: dict, verify_decode: bool) -> None:
        st = ap.stat()
        row = self.db.one("SELECT * FROM sources WHERE path=?", (str(ap),))
        if row and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1e-3 and row["sha256"]:
            self.db.execute("UPDATE sources SET last_seen=?, missing=0 WHERE id=?", (_now(), row["id"]))
            summary["unchanged"] += 1
            return
        sha = sha256_file(ap)
        pr = probe(ap)
        rel = str(ap.relative_to(base))
        vals = (root_id, str(ap), rel, st.st_size, st.st_mtime, sha, _jdump(pr.to_dict()), pr.codec, pr.family,
                None if pr.is_lossy is None else int(pr.is_lossy), pr.sample_rate, pr.channels, pr.bits_per_sample,
                pr.duration_s, "PROBE_OK" if pr.ok else "PROBE_FAILED", pr.error or None, _now(), _now())
        if row:
            self.db.execute(
                """UPDATE sources SET root_id=?, path=?, rel_path=?, size=?, mtime=?, sha256=?, probe_json=?, codec=?,
                family=?, is_lossy=?, sample_rate=?, channels=?, bits=?, duration_s=?, decode_status=?, decode_error=?,
                first_seen=COALESCE(first_seen, ?), last_seen=?, missing=0 WHERE id=?""", vals + (row["id"],))
            summary["changed"] += 1
            sid = row["id"]
        else:
            cur = self.db.execute(
                """INSERT INTO sources (root_id, path, rel_path, size, mtime, sha256, probe_json, codec, family, is_lossy,
                sample_rate, channels, bits, duration_s, decode_status, decode_error, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
            sid = cur.lastrowid
            summary["new"] += 1
        if not pr.ok:
            summary["probe_failed"] += 1
        if pr.is_lossy:
            summary["lossy"] += 1
        if verify_decode and pr.ok:
            self._verify_decode(sid, ap, pr)
        trow = self.db.one("SELECT id FROM tracks WHERE source_id=?", (sid,))
        if not trow:
            parent_rel = Path(rel).parent
            album_name = str(parent_rel).replace("\\", " / ") if str(parent_rel) != "." else "Unsorted"
            album_id = self._ensure_album(album_name, str(ap.parent))
            num, clean = split_track_number(ap.stem)
            if pr.tags.get("title"):
                title, src_kind = pr.tags["title"], "tag"
            else:
                title, src_kind = clean, ("filename_numbered" if num is not None else "filename")
            meta = {"title_source": src_kind, "source_filename": ap.name}
            if num is not None:
                meta["filename_track_number"] = num
                track_no = num
            else:
                n = self.db.one("SELECT MAX(track_no) m FROM tracks WHERE album_id=?", (album_id,))["m"] or 0
                track_no = n + 1
            self.db.execute(
                "INSERT INTO tracks (source_id, title, album_id, track_no, metadata_json, created_at) VALUES (?,?,?,?,?,?)",
                (sid, title, album_id, track_no, _jdump(meta), _now()))

    def _verify_decode(self, sid: int, path: Path, pr: ProbeResult) -> dict:
        try:
            dec = decode(path, sample_rate=pr.sample_rate, channels=pr.channels)
            status = "DECODE_OK"
            err = None
            if pr.duration_s and abs(dec.duration_s - pr.duration_s) > max(0.5, 0.01 * pr.duration_s):
                status = "DECODE_LENGTH_MISMATCH"
                err = f"decoded {dec.duration_s:.2f}s vs container {pr.duration_s:.2f}s"
            elif dec.warnings:
                status = "DECODE_WARNINGS"
                err = "; ".join(dec.warnings[:5])
        except DecodeError as exc:
            status, err, dec = "DECODE_FAILED", str(exc)[-500:], None
        self.db.execute("UPDATE sources SET decode_status=?, decode_error=? WHERE id=?", (status, err, sid))
        return {"status": status, "error": err}

    def _ensure_album(self, name: str, folder_hint: str | None = None) -> int:
        row = self.db.one("SELECT id FROM albums WHERE name=?", (name,))
        if row:
            return row["id"]
        meta = {}
        num, clean = split_track_number(Path(name).name)
        if num is not None and folder_hint:
            meta = {"album_title": clean, "album_order_from_folder": num}
        return self.db.execute("INSERT INTO albums (name, folder_hint, metadata_json, created_at) VALUES (?,?,?,?)",
                               (name, folder_hint, _jdump(meta), _now())).lastrowid

    def _ingest_texts(self, texts: list[Path], audio: list[Path]) -> None:
        matches = match_texts(audio, texts)
        for tp in texts:
            if self.db.one("SELECT id FROM text_files WHERE path=?", (str(tp),)):
                continue
            try:
                txt, enc, warns = read_text(tp)
                parsed = parse_prompt_text(txt) | {"encoding_warnings": warns}
                self.db.execute("INSERT INTO text_files (path, sha256, encoding, parsed_json, first_seen) VALUES (?,?,?,?,?)",
                                (str(tp), sha256_file(tp), enc, _jdump(parsed), _now()))
            except OSError as exc:
                self.db.audit("text_read_error", {"path": str(tp), "error": str(exc)})
        for ap, m in matches.items():
            trow = self.db.one("SELECT t.id, t.text_match_status FROM tracks t JOIN sources s ON s.id=t.source_id "
                               "WHERE s.path=?", (str(ap),))
            if not trow or trow["text_match_status"] in ("CONFIRMED", "REJECTED"):
                continue  # never overwrite a human decision
            tid = None
            if m["text"] is not None:
                r = self.db.one("SELECT id FROM text_files WHERE path=?", (str(m["text"]),))
                tid = r["id"] if r else None
            meta_row = self.db.one("SELECT metadata_json FROM tracks WHERE id=?", (trow["id"],))
            meta = json.loads(meta_row["metadata_json"] or "{}")
            meta["text_match_alternatives"] = m["alternatives"]
            self.db.execute("UPDATE tracks SET text_id=?, text_match_confidence=?, text_match_status=?, metadata_json=? "
                            "WHERE id=?", (tid, m["confidence"], m["status"], _jdump(meta), trow["id"]))

    def _assign_duplicate_groups(self) -> None:
        rows = self.db.query("SELECT t.id, s.sha256, s.path FROM tracks t JOIN sources s ON s.id=t.source_id")
        by_sha: dict[str, list[int]] = {}
        by_title: dict[str, list[int]] = {}
        for r in rows:
            by_sha.setdefault(r["sha256"], []).append(r["id"])
            by_title.setdefault(norm_title(Path(r["path"]).name), []).append(r["id"])
        groups: dict[int, str] = {}
        for sha, ids in by_sha.items():
            if len(ids) > 1:
                for i in ids:
                    groups[i] = f"identical:{sha[:12]}"
        for t, ids in by_title.items():
            if len(ids) > 1:
                for i in ids:
                    groups.setdefault(i, f"alternate_take:{t[:40]}")
        for r in rows:
            self.db.execute("UPDATE tracks SET dup_group=? WHERE id=?", (groups.get(r["id"]), r["id"]))

    # ------------------------------------------------------------------ listing
    def list_albums(self) -> list[dict]:
        rows = self.db.query("SELECT a.*, COUNT(t.id) n FROM albums a LEFT JOIN tracks t ON t.album_id=a.id "
                             "GROUP BY a.id ORDER BY a.name")
        return [{"id": r["id"], "name": r["name"], "tracks": r["n"], "folder_hint": r["folder_hint"],
                 "metadata": json.loads(r["metadata_json"] or "{}")} for r in rows]

    def album_id(self, album: str | int) -> int:
        row = self.db.one("SELECT id FROM albums WHERE id=? OR name=?", (album, str(album)))
        if not row:
            raise EngineError(f"album not found: {album}")
        return row["id"]

    def list_tracks(self, album: str | int | None = None) -> list[dict]:
        sql = ("SELECT t.*, s.path, s.rel_path, s.codec, s.family, s.is_lossy, s.sample_rate, s.channels, s.bits, "
               "s.duration_s, s.sha256, s.decode_status, s.missing, a.name album_name FROM tracks t "
               "JOIN sources s ON s.id=t.source_id LEFT JOIN albums a ON a.id=t.album_id")
        params: tuple = ()
        if album is not None:
            sql += " WHERE t.album_id=?"
            params = (self.album_id(album),)
        sql += " ORDER BY a.name, t.track_no, t.id"
        out = []
        for r in self.db.query(sql, params):
            d = dict(r)
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            d["has_analysis"] = bool(self.db.one("SELECT 1 FROM analyses WHERE track_id=? AND kind='source' "
                                                 "AND source_sha=?", (r["id"], r["sha256"])))
            v = self.db.one("SELECT version_no, mode, status FROM versions WHERE track_id=? ORDER BY version_no DESC "
                            "LIMIT 1", (r["id"],))
            d["latest_version"] = dict(v) if v else None
            out.append(d)
        return out

    def track(self, track_id: int) -> dict:
        r = self.db.one("SELECT t.*, s.path, s.rel_path, s.sha256, s.probe_json, s.codec, s.family, s.is_lossy, "
                        "s.sample_rate, s.channels, s.bits, s.duration_s, s.size, s.decode_status, a.name album_name "
                        "FROM tracks t JOIN sources s ON s.id=t.source_id LEFT JOIN albums a ON a.id=t.album_id "
                        "WHERE t.id=?", (int(track_id),))
        if not r:
            raise EngineError(f"track not found: {track_id}")
        d = dict(r)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        d["probe"] = json.loads(d.pop("probe_json") or "{}")
        if d["text_id"]:
            tr = self.db.one("SELECT * FROM text_files WHERE id=?", (d["text_id"],))
            d["text"] = {"path": tr["path"], "encoding": tr["encoding"], "parsed": json.loads(tr["parsed_json"] or "{}")}
        else:
            d["text"] = None
        return d

    def probe_result(self, t: dict) -> ProbeResult:
        p = dict(t["probe"])
        return ProbeResult(**p)

    # ------------------------------------------------------------------ source protection
    def verify_source_unchanged(self, track_id: int) -> bool:
        t = self.track(track_id)
        return sha256_file(t["path"]) == t["sha256"]

    def _check_source(self, t: dict) -> None:
        p = Path(t["path"])
        if not p.exists():
            raise EngineError(f"source missing: {p}")
        st = p.stat()
        if st.st_size != t["size"]:
            raise EngineError("source changed on disk since scan (size); rescan before processing")

    def _check_disk(self) -> None:
        if free_disk_bytes(self.root) < MIN_FREE_BYTES:
            raise EngineError(f"low disk space under {self.root} (< {MIN_FREE_BYTES // 2**20} MiB free)")

    # ------------------------------------------------------------------ analysis
    def _prompt_text(self, t: dict) -> str | None:
        if not t.get("text"):
            return None
        p = t["text"]["parsed"]
        return " ".join(filter(None, [p.get("style_prompt"), p.get("declared_genre")])) or None

    def load_source_audio(self, t: dict, target_rate: int | None = None):
        return decode(t["path"], sample_rate=t["sample_rate"], channels=t["channels"], target_rate=target_rate)

    def analyze_track(self, track_id: int, *, force: bool = False) -> dict:
        t = self.track(track_id)
        if not force:
            row = self.db.one("SELECT result_json FROM analyses WHERE track_id=? AND source_sha=? AND analysis_version=? "
                              "AND kind='source'", (t["id"], t["sha256"], ANALYSIS_VERSION))
            if row:
                return json.loads(row["result_json"])
        self._check_source(t)
        if t["decode_status"] == "PROBE_FAILED":
            raise EngineError(f"cannot analyze: probe failed ({t['probe'].get('error')})")
        try:
            dec = self.load_source_audio(t)
        except DecodeError as exc:
            self.db.execute("UPDATE sources SET decode_status='DECODE_FAILED', decode_error=? WHERE id=?",
                            (str(exc)[-500:], t["source_id"]))
            raise EngineError(f"decode failed: {exc}") from exc
        status = "DECODE_OK"
        if t["duration_s"] and abs(dec.duration_s - t["duration_s"]) > max(0.5, 0.01 * t["duration_s"]):
            status = "DECODE_LENGTH_MISMATCH"
        elif dec.warnings:
            status = "DECODE_WARNINGS"
        self.db.execute("UPDATE sources SET decode_status=?, decode_error=? WHERE id=?",
                        (status, "; ".join(dec.warnings[:5]) or None, t["source_id"]))
        meas = analyze_audio(dec.audio, dec.sample_rate, prompt_text=self._prompt_text(t))
        meas["decode"] = {"status": status, "warnings": dec.warnings[:20], "decoded_duration_s": round(dec.duration_s, 4)}
        qc = technical_status(meas, source_is_lossy=bool(t["is_lossy"]), decode_warnings=dec.warnings)
        if status != "DECODE_OK":
            qc["warnings"].append(f"decode status {status}")
            if qc["technical_status"] == "TECHNICAL_PASS":
                qc["technical_status"] = "NEEDS_LISTENING_REVIEW"
        result = {"track_id": t["id"], "source": self._provenance(t), "measurements": meas, "qc": qc,
                  "created_at": _now()}
        self.db.execute("INSERT OR REPLACE INTO analyses (track_id, source_sha, analysis_version, kind, result_json, "
                        "created_at) VALUES (?,?,?,?,?,?)",
                        (t["id"], t["sha256"], ANALYSIS_VERSION, "source", _jdump(result), _now()))
        self._write_track_report(t, result, "source_analysis")
        return result

    def _provenance(self, t: dict) -> dict:
        pr = t["probe"]
        return {
            "path": t["path"], "rel_path": t["rel_path"], "sha256": t["sha256"], "size_bytes": t["size"],
            "container": pr.get("container"), "codec": t["codec"], "codec_family": t["family"],
            "is_lossy": bool(t["is_lossy"]), "bit_rate": pr.get("bit_rate"), "sample_rate": t["sample_rate"],
            "channels": t["channels"], "channel_layout": pr.get("channel_layout"), "bits_per_sample": t["bits"],
            "duration_s": t["duration_s"], "tags": pr.get("tags"), "probe_warnings": pr.get("warnings"),
            "text_match": {"status": t["text_match_status"], "confidence": t["text_match_confidence"],
                           "text_path": t["text"]["path"] if t["text"] else None},
        }

    def _report_dir(self, t: dict) -> Path:
        d = self.root / "reports" / f"{t['id']:05d}_{safe_name(t['title'] or 'track', 60)}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_track_report(self, t: dict, result: dict, stem: str) -> dict:
        d = self._report_dir(t)
        jp, hp = d / f"{stem}.json", d / f"{stem}.html"
        atomic_write_text(jp, json.dumps(result, ensure_ascii=False, indent=1, default=_json_default),
                          overwrite_generated=True)
        atomic_write_text(hp, track_report_html(t["title"] or "track", result), overwrite_generated=True)
        return {"json": str(jp), "html": str(hp)}

    def get_analysis(self, track_id: int) -> dict | None:
        t = self.track(track_id)
        row = self.db.one("SELECT result_json FROM analyses WHERE track_id=? AND source_sha=? AND analysis_version=? "
                          "AND kind='source'", (t["id"], t["sha256"], ANALYSIS_VERSION))
        return json.loads(row["result_json"]) if row else None

    def analysis_summary(self, track_id: int) -> dict:
        a = self.get_analysis(track_id)
        if not a:
            return {"track_id": track_id, "status": "NOT_ANALYZED"}
        m = a["measurements"]
        return {
            "track_id": track_id,
            "qc": a["qc"],
            "source": {k: a["source"][k] for k in ("codec", "codec_family", "is_lossy", "sample_rate", "channels",
                                                   "bits_per_sample", "duration_s", "sha256")},
            "loudness": m["loudness"], "peaks": {k: m["peaks"][k] for k in ("sample_peak_dbfs", "true_peak_dbtp",
                                                                           "intersample_over_count")},
            "dynamics": m["dynamics"], "stereo": {k: v for k, v in m["stereo"].items() if k != "worst_correlation_windows"},
            "spectral": m["spectral"], "tempo": m.get("tempo"),
            "style_top3": m["style"]["ranked"][:3], "style_confidence": m["style"]["confidence"],
            "prompt_vs_audio": m["style"]["prompt_vs_audio"],
            "defect_candidates": [
                {k: c[k] for k in ("type", "start_s", "end_s", "channel", "severity", "confidence", "auto_repair_eligible")}
                for c in m["defects"]["candidates"][:50]],
            "defect_candidate_count": len(m["defects"]["candidates"]),
            "listening_required": [f["type"] for f in m["defects"]["listening_required"]],
        }

    # ------------------------------------------------------------------ versions
    def _next_version_no(self, track_id: int) -> int:
        r = self.db.one("SELECT MAX(version_no) m FROM versions WHERE track_id=?", (track_id,))
        return (r["m"] or 0) + 1

    def _new_version(self, track_id: int, mode: str, cfg: dict, status: str, parent: int | None = None) -> dict:
        with self.db.transaction():
            vno = self._next_version_no(track_id)
            vid = self.db.execute(
                "INSERT INTO versions (track_id, version_no, mode, config_json, config_hash, parent_version_id, status, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (track_id, vno, mode, _jdump(cfg), config_hash(cfg), parent, status, _now())).lastrowid
        return {"id": vid, "version_no": vno}

    def version(self, track_id: int, version_no: int) -> dict:
        r = self.db.one("SELECT * FROM versions WHERE track_id=? AND version_no=?", (int(track_id), int(version_no)))
        if not r:
            raise EngineError(f"version {version_no} of track {track_id} not found")
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        d["report"] = json.loads(d.pop("report_json") or "null")
        d["approvals"] = [dict(a) for a in self.db.query("SELECT * FROM approvals WHERE version_id=? ORDER BY id",
                                                          (d["id"],))]
        return d

    def list_versions(self, track_id: int) -> list[dict]:
        return [{"version_no": r["version_no"], "mode": r["mode"], "status": r["status"], "config_hash": r["config_hash"],
                 "output_path": r["output_path"], "created_at": r["created_at"]}
                for r in self.db.query("SELECT * FROM versions WHERE track_id=? ORDER BY version_no", (int(track_id),))]

    def _render_dir(self, t: dict, vno: int) -> Path:
        d = self.root / "renders" / f"{t['id']:05d}_{safe_name(t['title'] or 'track', 60)}" / f"v{vno:03d}"
        d.mkdir(parents=True, exist_ok=False)
        return d

    def convert_track(self, track_id: int, *, target_bits: int = 24, overs_policy: str = "trim") -> dict:
        """CONVERT_ONLY render: no discretionary processing. Creates a new immutable version."""
        from srm.dsp.convert import convert

        t = self.track(track_id)
        self._check_source(t)
        self._check_disk()
        cfg = {"mode": "CONVERT_ONLY", "target_bits": target_bits, "overs_policy": overs_policy,
               "allowed_rates": [44100, 48000], "app_version": __version__}
        v = self._new_version(t["id"], "CONVERT_ONLY", cfg, "RENDERING")
        try:
            out_dir = self._render_dir(t, v["version_no"])
            out = out_dir / f"{safe_name(t['title'] or 'track')}.wav"
            pr = self.probe_result(t)
            rec = convert(t["path"], pr, out, target_bits=target_bits, overs_policy=overs_policy,
                          seed=t["id"] * 1000 + v["version_no"])
            rec.pop("float_audio_for_measurement", None)
            report = self._finalize_render(t, v, out, rec, cfg)
            return report
        except Exception as exc:
            self.db.execute("UPDATE versions SET status='FAILED', report_json=? WHERE id=?",
                            (_jdump({"error": f"{type(exc).__name__}: {exc}"}), v["id"]))
            raise

    def _finalize_render(self, t: dict, v: dict, out: Path, rec: dict, cfg: dict) -> dict:
        opr = probe(out)
        dec = decode(out, sample_rate=opr.sample_rate, channels=opr.channels)
        out_meas = analyze_audio(dec.audio, dec.sample_rate, prompt_text=None, with_tempo=False)
        src_an = self.get_analysis(t["id"])
        src_meas = src_an["measurements"] if src_an else None
        sha = sha256_file(out)
        report = {
            "track_id": t["id"], "version_no": v["version_no"], "mode": cfg["mode"], "config": cfg,
            "config_hash": config_hash(cfg), "source": self._provenance(t), "processing": rec,
            "output": {"path": str(out), "sha256": sha, "probe": opr.to_dict()},
            "measurements": strip_timelines(src_meas) if src_meas else None,
            "output_measurements": out_meas,
            "comparison": compare_measurements(src_meas, out_meas) if src_meas else None,
            "qc": technical_status(out_meas, source_is_lossy=bool(t["is_lossy"])),
            "source_unchanged": sha256_file(t["path"]) == t["sha256"],
        }
        report["qc"]["approval"] = "NOT_APPROVED"
        rp = self._write_track_report(t, report, f"v{v['version_no']:03d}_{cfg['mode'].lower()}")
        report["report_files"] = rp
        cfg_path = out.parent / "processing_config.json"
        atomic_write_text(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=1))
        self.db.execute("UPDATE versions SET status='RENDERED', output_path=?, output_sha256=?, report_json=? WHERE id=?",
                        (str(out), sha, _jdump(strip_report_for_db(report)), v["id"]))
        self.db.audit("render", {"track_id": t["id"], "version": v["version_no"], "mode": cfg["mode"], "sha256": sha})
        return report

    # ------------------------------------------------------------------ approvals
    def approve(self, track_id: int, version_no: int, *, decision: str = "APPROVED", actor: str = "local-user",
                note: str = "") -> dict:
        if decision not in ("APPROVED", "REJECTED", "LISTENING_SIGNOFF"):
            raise EngineError("decision must be APPROVED, REJECTED or LISTENING_SIGNOFF")
        v = self.version(track_id, version_no)
        if v["status"] not in ("RENDERED", "APPROVED", "REJECTED"):
            raise EngineError(f"version status {v['status']} cannot be approved")
        if v["output_path"] and sha256_file(v["output_path"]) != v["output_sha256"]:
            raise EngineError("rendered file changed since render; refusing approval")
        self.db.execute("INSERT INTO approvals (version_id, decision, actor, note, created_at) VALUES (?,?,?,?,?)",
                        (v["id"], decision, actor, note, _now()))
        if decision in ("APPROVED", "REJECTED"):
            self.db.execute("UPDATE versions SET status=? WHERE id=?", (decision, v["id"]))
        self.db.audit("approval", {"track_id": track_id, "version": version_no, "decision": decision, "actor": actor})
        return self.version(track_id, version_no)

    def approved_version(self, track_id: int) -> dict | None:
        r = self.db.one("SELECT version_no FROM versions WHERE track_id=? AND status='APPROVED' "
                        "ORDER BY version_no DESC LIMIT 1", (track_id,))
        return self.version(track_id, r["version_no"]) if r else None

    # ------------------------------------------------------------------ jobs
    def create_job(self, kind: str, track_ids: list[int], stages: list[str], params: dict | None = None,
                   idempotency_key: str | None = None) -> int:
        if idempotency_key:
            r = self.db.one("SELECT id FROM jobs WHERE idempotency_key=?", (idempotency_key,))
            if r:
                return r["id"]
        with self.db.transaction():
            jid = self.db.execute("INSERT INTO jobs (kind, params_json, status, created_at, updated_at, idempotency_key) "
                                  "VALUES (?,?,?,?,?,?)",
                                  (kind, _jdump(params or {}), "PENDING", _now(), _now(), idempotency_key)).lastrowid
            for tid in track_ids:
                for st in stages:
                    self.db.execute("INSERT INTO job_items (job_id, track_id, stage, status, updated_at) VALUES (?,?,?,?,?)",
                                    (jid, tid, st, "PENDING", _now()))
        self.db.audit("job_created", {"job_id": jid, "kind": kind, "tracks": len(track_ids), "stages": stages})
        return jid

    def job_status(self, job_id: int) -> dict:
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (int(job_id),))
        if not j:
            raise EngineError(f"job not found: {job_id}")
        counts = {r["status"]: r["c"] for r in self.db.query(
            "SELECT status, COUNT(*) c FROM job_items WHERE job_id=? GROUP BY status", (job_id,))}
        total = sum(counts.values())
        done = counts.get("DONE", 0)
        failed = [dict(r) for r in self.db.query(
            "SELECT track_id, stage, error, attempts FROM job_items WHERE job_id=? AND status='FAILED'", (job_id,))]
        return {"job_id": j["id"], "kind": j["kind"], "status": j["status"], "total_items": total,
                "done": done, "failed": len(failed), "pending": counts.get("PENDING", 0) + counts.get("RUNNING", 0),
                "progress": round(done / total, 4) if total else 1.0, "failures": failed[:50],
                "params": json.loads(j["params_json"]), "updated_at": j["updated_at"], "error": j["error"]}

    def cancel_job(self, job_id: int) -> dict:
        ev = self._cancel_flags.get(int(job_id))
        if ev:
            ev.set()
        self.db.execute("UPDATE jobs SET status='CANCEL_REQUESTED', updated_at=? WHERE id=? AND status IN "
                        "('PENDING','RUNNING')", (_now(), job_id))
        return self.job_status(job_id)

    def run_job(self, job_id: int, *, workers: int | None = None, retry_failed: bool = False,
                progress: Callable[[dict], None] | None = None, stop_after: int | None = None) -> dict:
        """Run (or resume) a job. Items already DONE are skipped; RUNNING items from a crash are retried.

        ``stop_after`` processes at most N items then returns (used to test interruption/resume).
        """
        job_id = int(job_id)
        ev = self._cancel_flags.setdefault(job_id, threading.Event())
        ev.clear()
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        params = json.loads(j["params_json"])
        statuses = ("PENDING", "RUNNING", "FAILED") if retry_failed else ("PENDING", "RUNNING")
        items = self.db.query(f"SELECT * FROM job_items WHERE job_id=? AND status IN ({','.join('?' * len(statuses))}) "
                              "ORDER BY id", (job_id, *statuses))
        if stop_after is not None:
            items = items[:stop_after]
        self.db.execute("UPDATE jobs SET status='RUNNING', updated_at=? WHERE id=?", (_now(), job_id))
        workers = workers or int(self.settings.get("workers", 2))
        lock = threading.Lock()

        def work(item):
            if ev.is_set():
                raise Cancelled()
            self.db.execute("UPDATE job_items SET status='RUNNING', attempts=attempts+1, updated_at=? WHERE id=?",
                            (_now(), item["id"]))
            try:
                res = self._run_stage(item["stage"], item["track_id"], params)
                self.db.execute("UPDATE job_items SET status='DONE', error=NULL, result_json=?, updated_at=? WHERE id=?",
                                (_jdump(res), _now(), item["id"]))
            except Cancelled:
                self.db.execute("UPDATE job_items SET status='PENDING', updated_at=? WHERE id=?", (_now(), item["id"]))
                raise
            except Exception as exc:
                self.db.execute("UPDATE job_items SET status='FAILED', error=?, updated_at=? WHERE id=?",
                                (f"{type(exc).__name__}: {exc}"[:2000], _now(), item["id"]))
                (self.root / "logs").mkdir(exist_ok=True)
                with open(self.root / "logs" / f"job_{job_id}.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.ctime()} item {item['id']} track {item['track_id']} {item['stage']}\n"
                             f"{traceback.format_exc()}\n")
            if progress:
                with lock:
                    progress(self.job_status(job_id))

        cancelled = False
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(work, it) for it in items]
            for f in futures:
                try:
                    f.result()
                except Cancelled:
                    cancelled = True
        st = self.job_status(job_id)
        if cancelled or ev.is_set():
            final = "CANCELLED"
        elif st["pending"] > 0:
            final = "PAUSED"
        elif st["failed"]:
            final = "COMPLETED_WITH_ERRORS"
        else:
            final = "COMPLETED"
        self.db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (final, _now(), job_id))
        self.db.audit("job_run", {"job_id": job_id, "status": final})
        return self.job_status(job_id)

    def _run_stage(self, stage: str, track_id: int, params: dict) -> dict:
        if stage == "analyze":
            a = self.analyze_track(track_id, force=bool(params.get("force")))
            return {"technical_status": a["qc"]["technical_status"]}
        if stage == "convert":
            r = self.convert_track(track_id, target_bits=int(params.get("target_bits", 24)),
                                   overs_policy=params.get("overs_policy", "trim"))
            return {"version_no": r["version_no"]}
        if stage == "render":
            # render the latest PROPOSED version, or propose SAFE_MASTER first (still not approved)
            r = self.db.one("SELECT version_no FROM versions WHERE track_id=? AND status='PROPOSED' "
                            "ORDER BY version_no DESC LIMIT 1", (track_id,))
            if not r:
                if params.get("approved_only"):
                    raise EngineError("no PROPOSED version to render")
                vno = self.suggest_mastering(track_id, mode=params.get("mode", "SAFE_MASTER"),
                                             preset=params.get("preset"),
                                             ceiling_dbtp=float(params.get("ceiling_dbtp", -1.0)))["version_no"]
            else:
                vno = r["version_no"]
            rep = self.render_version(track_id, vno, codec_preview=params.get("codec_preview", True))
            return {"version_no": vno, "policy": {k: v.get("status") for k, v in rep["policy_check"].items()}}
        extra = getattr(self, f"_stage_{stage}", None)
        if extra:
            return extra(track_id, params)
        raise EngineError(f"unknown stage {stage}")


_NUM_PREFIX = re.compile(r"^\s*(\d{1,3})\s*(?:[\u2014\u2013\-_.)]+\s*|\s+)(\S.*)$")


def split_track_number(stem: str) -> tuple[int | None, str]:
    """'01 — CONFESSION AT DAWN' -> (1, 'CONFESSION AT DAWN'); names without a leading number are unchanged."""
    m = _NUM_PREFIX.match(stem)
    if not m:
        return None, stem
    return int(m.group(1)), m.group(2).strip()


def strip_report_for_db(report: dict) -> dict:
    r = dict(report)
    if r.get("output_measurements"):
        r["output_measurements"] = strip_timelines(r["output_measurements"])
    r.pop("measurements", None)
    return r


def compare_measurements(a: dict | None, b: dict | None) -> dict | None:
    if not a or not b:
        return None

    def g(m, *ks):
        for k in ks:
            m = (m or {}).get(k)
        return m

    keys = [("loudness", "integrated_lufs"), ("loudness", "lra_lu"), ("peaks", "true_peak_dbtp"),
            ("peaks", "sample_peak_dbfs"), ("dynamics", "crest_factor_db"), ("dynamics", "plr_db"),
            ("dynamics", "median_short_window_crest_db"), ("stereo", "correlation"), ("stereo", "side_to_mid_db"),
            ("spectral", "spectral_centroid_hz"), ("duration_s",)]
    out = {}
    for k in keys:
        va, vb = g(a, *k), g(b, *k)
        name = ".".join(k)
        out[name] = {"before": va, "after": vb,
                     "delta": round(vb - va, 3) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None}
    return out


def capabilities() -> dict:
    from srm.analysis.genre import essentia_capability

    caps = {
        "loudness_bs1770": "AVAILABLE",
        "true_peak_oversampled": "AVAILABLE",
        "tempo_basic": "AVAILABLE",
        "style_heuristic": "AVAILABLE (LOW confidence)",
        "style_model": essentia_capability()["status"],
        "defect_candidates": "AVAILABLE (candidates only)",
        "perceptual_ai_artifact_detection": "UNAVAILABLE (REQUIRES_LISTENING_REVIEW)",
        "source_separation": "UNAVAILABLE (no license-checked model installed; import Suno stems instead)",
        "stem_import_validation": "AVAILABLE",
        "repair_click_lsar": "AVAILABLE (approval / high-confidence auto with regression checks)",
        "repair_declip": "AVAILABLE_EXPERIMENTAL (approval only)",
        "speech_denoise_full_mix": "DISABLED_BY_POLICY",
    }
    try:
        t = get_tools()
        caps["ffmpeg"] = "AVAILABLE"
        caps["soxr_resampler"] = "AVAILABLE" if t.has_soxr else "UNAVAILABLE (fallback swr)"
        caps["mp3_preview"] = "AVAILABLE" if t.has_libmp3lame else "UNAVAILABLE"
    except Exception:
        caps["ffmpeg"] = "UNAVAILABLE"
    return caps
