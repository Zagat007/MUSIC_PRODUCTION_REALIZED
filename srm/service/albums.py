"""Album management, metadata, text-match decisions and release export."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from srm import __version__
from srm.analysis.report import CSS, esc, track_report_html
from srm.core.paths import atomic_write_text, safe_name, sha256_file
from srm.core.tools import ffmpeg_input, get_tools, run
from srm.dsp.convert import choose_rate
from srm.dsp.quantize import write_float
from srm.ingest.decode import decode
from srm.ingest.probe import probe
from srm.release import metadata as M
from srm.release.profiles import EXPORT_PROFILES, load_profiles
from srm.release.validate import failed_validation, validate_package


class AlbumMixin:
    # ------------------------------------------------------------ organisation
    def create_album(self, name: str) -> dict:
        aid = self._ensure_album(name.strip(), None)
        self.db.audit("album_create", {"album_id": aid, "name": name})
        return {"id": aid, "name": name}

    def rename_album(self, album, new_name: str) -> dict:
        aid = self.album_id(album)
        self.db.execute("UPDATE albums SET name=? WHERE id=?", (new_name.strip(), aid))
        return {"id": aid, "name": new_name}

    def assign_track(self, track_id: int, album, position: int | None = None) -> dict:
        """Assign a track to an album (the source file is never moved)."""
        aid = self.album_id(album)
        n = self.db.one("SELECT COUNT(*) c FROM tracks WHERE album_id=?", (aid,))["c"]
        self.db.execute("UPDATE tracks SET album_id=?, track_no=? WHERE id=?", (aid, n + 1, int(track_id)))
        if position is not None:
            ids = [t["id"] for t in self.list_tracks(aid) if t["id"] != int(track_id)]
            ids.insert(max(0, int(position) - 1), int(track_id))
            self.reorder_album(aid, ids)
        self.db.audit("assign_track", {"track_id": track_id, "album_id": aid, "position": position})
        return {"track_id": int(track_id), "album_id": aid}

    def reorder_album(self, album, track_ids: list[int]) -> list[dict]:
        aid = self.album_id(album)
        current = {t["id"] for t in self.list_tracks(aid)}
        if set(map(int, track_ids)) != current:
            raise ValueError("track_ids must list exactly the tracks of the album")
        with self.db.transaction():
            for i, tid in enumerate(track_ids, start=1):
                self.db.execute("UPDATE tracks SET track_no=? WHERE id=?", (i, int(tid)))
        return self.list_tracks(aid)

    def set_track_gap(self, track_id: int, gap_after_s: float) -> None:
        if not 0 <= gap_after_s <= 30:
            raise ValueError("gap must be 0..30 s")
        self.db.execute("UPDATE tracks SET gap_after_s=? WHERE id=?", (float(gap_after_s), int(track_id)))

    def set_track_metadata(self, track_id: int, update: dict) -> dict:
        clean = M.clean_track_meta(update)
        self.track(track_id)  # raises for an unknown track
        self._mutate_meta("tracks", int(track_id), lambda meta: meta.setdefault("release", {}).update(clean))
        if "title" in clean and clean["title"]:
            self.db.execute("UPDATE tracks SET title=? WHERE id=?", (clean["title"], int(track_id)))
        self.db.audit("track_metadata", {"track_id": track_id, "fields": sorted(clean)})
        return self.track(track_id)["metadata"]["release"]

    def set_album_metadata(self, album, update: dict) -> dict:
        aid = self.album_id(album)
        clean = M.clean_album_meta(update)
        return self._mutate_meta("albums", aid, lambda meta: meta.update(clean))

    def set_style_override(self, track_id: int, preset: str | None) -> None:
        from srm.dsp.presets import PRESETS

        if preset is not None and preset not in PRESETS:
            raise ValueError(f"unknown preset {preset}")
        self.db.execute("UPDATE tracks SET style_override=? WHERE id=?", (preset, int(track_id)))
        self.db.audit("style_override", {"track_id": track_id, "preset": preset})

    def decide_text_match(self, track_id: int, *, accept: bool, text_path: str | None = None) -> dict:
        """Human decision on prompt/lyrics association (never overwritten by later scans)."""
        t = self.track(track_id)
        if not accept:
            self.db.execute("UPDATE tracks SET text_id=NULL, text_match_status='REJECTED', text_match_confidence=0 "
                            "WHERE id=?", (t["id"],))
        else:
            tid = t["text_id"]
            if text_path:
                r = self.db.one("SELECT id FROM text_files WHERE path=?", (str(Path(text_path).resolve()),))
                if not r:
                    raise ValueError("text file not known to this project (scan it first)")
                tid = r["id"]
            if not tid:
                raise ValueError("no text file to confirm")
            self.db.execute("UPDATE tracks SET text_id=?, text_match_status='CONFIRMED', text_match_confidence=1.0 "
                            "WHERE id=?", (tid, t["id"]))
        self.db.audit("text_match_decision", {"track_id": track_id, "accept": accept, "text_path": text_path})
        return self.track(track_id)

    def listening_signoff(self, track_id: int, version_no: int, note: str = "", actor: str = "local-user") -> dict:
        return self.approve(track_id, version_no, decision="LISTENING_SIGNOFF", actor=actor, note=note)

    # ------------------------------------------------------------ export
    def export_album(self, album, *, profile: str = "release_wav", include_mp3: bool = True,
                     include_flac: bool = False, allow_draft: bool = False, confirm: bool = False,
                     distributor: str = "generic") -> dict:
        """Build RELEASES/<album>/vNNN. Requires confirm=True (explicit user consent for a final write)."""
        from srm.service.engine import EngineError

        if not confirm:
            raise EngineError("export requires explicit confirmation (confirm=True / --confirm)")
        if profile not in EXPORT_PROFILES:
            raise EngineError(f"unknown profile {profile}")
        prof = EXPORT_PROFILES[profile]
        aid = self.album_id(album)
        arow = self.db.one("SELECT * FROM albums WHERE id=?", (aid,))
        ameta = json.loads(arow["metadata_json"] or "{}")
        tracks = self.list_tracks(aid)
        if not tracks:
            raise EngineError("album has no tracks")
        self._check_disk()
        chosen, problems = [], []
        for tr in tracks:
            v = self.approved_version(tr["id"])
            if v is None:
                r = self.db.one("SELECT version_no FROM versions WHERE track_id=? AND status='RENDERED' "
                                "ORDER BY version_no DESC LIMIT 1", (tr["id"],))
                if r and allow_draft:
                    v = self.version(tr["id"], r["version_no"])
                    problems.append(f"track {tr['track_no']} '{tr['title']}': using UNAPPROVED v{v['version_no']:03d}")
                else:
                    problems.append(f"track {tr['track_no']} '{tr['title']}': no approved rendered version")
                    v = None
            chosen.append((tr, v))
        if any(v is None for _, v in chosen):
            raise EngineError("cannot export: " + "; ".join(problems))
        if problems and not allow_draft:
            raise EngineError("; ".join(problems))
        base = self.root / "RELEASES" / safe_name(arow["name"], 80)
        base.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name[1:]) for p in base.iterdir() if p.is_dir() and p.name[:1] == "v" and p.name[1:].isdigit()]
        vno = max(existing, default=0) + 1
        final_dir = base / f"v{vno:03d}"
        build = base / f".building_v{vno:03d}_{os.getpid()}"
        build.mkdir()
        try:
            manifest = self._build_package(build, arow, ameta, chosen, prof, profile, include_mp3, include_flac,
                                           distributor, problems)
            os.rename(build, final_dir)  # atomic publish of the finished package
        except BaseException:
            shutil.rmtree(build, ignore_errors=True)  # only our own temporary build dir
            raise
        # The manifest hash is recorded outside the package, so a later edit of the manifest is detectable.
        man_sha = sha256_file(final_dir / "processing_manifest.json")
        try:
            val = validate_package(final_dir, ceiling_dbtp=manifest["ceiling_dbtp"], expected_manifest_sha256=man_sha)
        except Exception as exc:  # a published package must always carry a validation verdict and a DB record
            val = failed_validation(final_dir, f"validator error: {type(exc).__name__}: {exc}", profile)
        atomic_write_text(final_dir / "release_validation.json", json.dumps(val, ensure_ascii=False, indent=1))
        atomic_write_text(final_dir / "album_qa.html", album_qa_html(arow["name"], manifest, val))
        self.db.execute("INSERT INTO exports (album_id, path, profile, status, manifest_json, created_at) "
                        "VALUES (?,?,?,?,?,?)", (aid, str(final_dir), profile, val["overall"],
                                                 json.dumps({"tracks": len(chosen), "manifest_sha256": man_sha}),
                                                 time.time()))
        self.db.audit("export", {"album": arow["name"], "dir": str(final_dir), "status": val["overall"]})
        return {"path": str(final_dir), "status": val["overall"], "technical": val["technical"],
                "human_gates": val["human_gates"], "unresolved": manifest["unresolved"], "validation": val}

    def _build_package(self, d: Path, arow, ameta, chosen, prof, profile, include_mp3, include_flac,
                       distributor, problems) -> dict:
        (d / "WAV_MASTER").mkdir()
        (d / "QA").mkdir()
        if include_mp3:
            (d / "MP3_PREVIEW").mkdir()
        if include_flac:
            (d / "FLAC_ARCHIVE").mkdir()
        tools = get_tools()
        artwork = self.album_artwork(arow['id'])
        ameta = dict(ameta, album_description=artwork['description'])
        cover_path = None
        if artwork['cover']:
            cover_path = d / 'ARTWORK' / 'cover.jpg'
            cover_path.parent.mkdir()
            shutil.copyfile(artwork['cover']['path'], cover_path)
        recs, mtracks, unresolved = [], [], list(problems)
        ceilings = []
        for pos, (tr, v) in enumerate(chosen, start=1):
            t = self.track(tr["id"])
            rec = M.track_record(t, pos)
            stem = f"{pos:02d}_{safe_name(rec['title'], 80)}"
            src_wav = Path(v["output_path"])
            if sha256_file(src_wav) != v["output_sha256"]:
                raise RuntimeError(f"rendered file changed since render: {src_wav}")
            out_wav = d / "WAV_MASTER" / f"{stem}.wav"
            conv = None
            opr = probe(src_wav)
            if opr.codec == prof["codec"] and opr.sample_rate in prof["allowed_rates"]:
                shutil.copyfile(src_wav, out_wav)  # bytes identical to the approved render
            else:
                tgt = choose_rate(opr.sample_rate, tuple(prof["allowed_rates"]))
                dec = decode(src_wav, sample_rate=opr.sample_rate, channels=opr.channels,
                             target_rate=tgt if tgt != opr.sample_rate else None)
                conv = write_float(out_wav, dec.audio, dec.sample_rate, prof["bits"], dither="tpdf",
                                   seed=pos, clip_policy="fail")
                conv["resampled_from"] = dec.resampled_from
                conv["resampler"] = dec.resampler or None
            wsha = sha256_file(out_wav)
            if include_mp3:
                mp3 = d / "MP3_PREVIEW" / f"{stem}.mp3"
                p = run([tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", ffmpeg_input(out_wav),
                         "-c:a", "libmp3lame", "-b:a", "320k", "-map_metadata", "-1", ffmpeg_input(mp3)])
                if p.returncode != 0:
                    raise RuntimeError("MP3 preview encode failed: " + p.stderr.decode("utf-8", "replace")[-300:])
                if cover_path:
                    from srm.release.artwork import attach_cover

                    attach_cover(mp3, cover_path)
            if include_flac:
                fl = d / "FLAC_ARCHIVE" / f"{stem}.flac"
                p = run([tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", ffmpeg_input(out_wav),
                         "-c:a", "flac", "-compression_level", "8", ffmpeg_input(fl)])
                if p.returncode != 0:
                    raise RuntimeError("FLAC encode failed")
            vrep = v["report"] or {}
            om = vrep.get("output_measurements") or {}
            chain = (v["config"].get("chain") or {})
            ceiling = chain.get("limiter", {}).get("ceiling_dbtp", 0.0) if chain.get("limiter", {}).get("enabled") else 0.0
            ceilings.append(ceiling)
            signoff = any(a["decision"] == "LISTENING_SIGNOFF" for a in v["approvals"])
            issues = M.unresolved(rec)
            if not signoff:
                issues.append("no listening sign-off for delivered version")
            if t["is_lossy"]:
                issues.append("source is lossy (AAC/MP3): not Hi-Res / Apple Digital Masters eligible")
            unresolved += [f"track {pos:02d}: {i}" for i in issues]
            recs.append(rec)
            mtracks.append({
                "track_number": pos, "file": out_wav.name, "wav_sha256": wsha,
                "source": self._provenance(t), "source_duration_s": om.get("duration_s") or t["duration_s"],
                "version_no": v["version_no"], "version_mode": v["mode"], "version_status": v["status"],
                "version_config_hash": v["config_hash"], "version_config": v["config"],
                "version_integrated_lufs": (om.get("loudness") or {}).get("integrated_lufs") if conv is None else None,
                "export_conversion": conv, "listening_signoff": signoff,
                "mp3_preview_required": include_mp3,
                "approvals": [{k: a[k] for k in ("decision", "actor", "note", "created_at")} for a in v["approvals"]],
                "gap_after_s": t.get("gap_after_s"),
            })
            qa = {"track": rec, "version": {"version_no": v["version_no"], "mode": v["mode"], "status": v["status"]},
                  **{k: vrep.get(k) for k in ("processing", "output", "comparison", "qc", "policy_check", "codec_preview")},
                  "output_measurements": om, "measurements": (self.get_analysis(t["id"]) or {}).get("measurements"),
                  "source": self._provenance(t), "unresolved": issues}
            atomic_write_text(d / "QA" / f"{stem}.json", json.dumps(qa, ensure_ascii=False, indent=1, default=str))
            atomic_write_text(d / "QA" / f"{stem}.html", track_report_html(rec["title"], qa))
        album = {f: ameta.get(f, M.UNKNOWN) for f in M.ALBUM_FIELDS}
        if album["album_title"] == M.UNKNOWN:
            album["album_title"] = arow["name"]
        for f in M.REQUIRED_ALBUM:
            if album[f] in ("", None, M.UNKNOWN):
                unresolved.append(f"album: missing required field {f}")
        meta_doc = {"album": album, "tracks": recs, "generated_by": f"Suno Release Master {__version__}",
                    "note": "UNKNOWN = not provided by the user. ISRC/UPC/credits are never generated."}
        atomic_write_text(d / "album_metadata.json", json.dumps(meta_doc, ensure_ascii=False, indent=1))
        buf = io.StringIO()
        cols = ["track_number", "title", "artist", "version", "genre", "language", "year", "isrc", "explicit",
                "songwriters", "ai_tool", "ai_model_version", "ai_generated_vocals", "ai_generated_music",
                "ai_generated_lyrics"]
        w = csv.writer(buf)
        w.writerow(cols)
        for r in recs:
            w.writerow([r.get(c, "") for c in cols])
        atomic_write_text(d / "album_metadata.csv", "﻿" + buf.getvalue())  # BOM: Excel-safe Cyrillic
        profiles = load_profiles(self.root)
        checklist = {
            "status": "INCOMPLETE" if any("rights" in u or "AI disclosure" in u for u in unresolved) else "USER_COMPLETED",
            "suno_rights_reference": profiles["suno_rights"],
            "distributor": distributor, "distributor_rules": profiles["distributors"].get(distributor),
            "tracks": [{"track_number": r["track_number"], "title": r["title"],
                        **{f: r.get(f) for f in M.AI_FIELDS + M.RIGHTS_FIELDS}} for r in recs],
            "disclaimer": "Checklist only - this is not legal advice or automatic rights approval.",
        }
        atomic_write_text(d / "rights_ai_disclosure_checklist.json", json.dumps(checklist, ensure_ascii=False, indent=1))
        cover = {"items": ["Square image, typically 3000x3000 px (check distributor)", "RGB JPG/PNG", "No URLs, "
                           "prices, social handles or misleading text", "Rights to all imagery cleared",
                           "Matches album title/artist exactly"],
                 "status": "TECHNICAL_IMAGE_CHECKED" if cover_path else "NOT_CHECKED",
                 "note": "Image format and MP3 embedding are verified; visual quality and rights need human review."}
        atomic_write_text(d / "cover_art_checklist.json", json.dumps(cover, ensure_ascii=False, indent=1))
        manifest = {"app_version": __version__, "created_at": time.time(), "profile": profile, "profile_spec": prof,
                    "ceiling_dbtp": min(ceilings) if ceilings else -1.0, "distributor": distributor,
                    "tracks": mtracks, "unresolved": unresolved,
                    "ffmpeg": tools.version_line}
        if cover_path:
            manifest['artwork'] = {**artwork['cover'], 'file': 'ARTWORK/cover.jpg'}
        atomic_write_text(d / "processing_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1, default=str))
        atomic_write_text(d / "UNRESOLVED_WARNINGS.txt", "\n".join(unresolved) + "\n" if unresolved else "none\n")
        return manifest

    def validate_release(self, album, version: int | None = None) -> dict:
        aid = self.album_id(album)
        row = self.db.one("SELECT name FROM albums WHERE id=?", (aid,))
        base = self.root / "RELEASES" / safe_name(row["name"], 80)
        dirs = sorted(p for p in base.glob("v[0-9][0-9][0-9]") if p.is_dir())
        if not dirs:
            raise ValueError("no export for this album")
        pkg = base / f"v{version:03d}" if version else dirs[-1]
        rec = self.db.one("SELECT manifest_json FROM exports WHERE path=? ORDER BY id DESC LIMIT 1", (str(pkg),))
        recorded = json.loads(rec["manifest_json"] or "{}").get("manifest_sha256") if rec else None
        return validate_package(pkg, expected_manifest_sha256=recorded)


def album_qa_html(name: str, manifest: dict, val: dict) -> str:
    rows = []
    for t, mt in zip(val["tracks"], manifest["tracks"]):
        c = t["checks"]
        bad = [k for k, v in c.items() if v["status"] in ("FAIL", "WARN", "PENDING")]
        lufs = c.get("loudness_measured", {}).get("integrated_lufs")
        tp = c.get("true_peak_vs_policy", {}).get("value")
        rows.append(f"<tr><td>{mt['track_number']:02d}</td><td>{esc(t['file'])}</td><td>{esc(mt['version_mode'])} "
                    f"v{mt['version_no']:03d} ({esc(mt['version_status'])})</td><td>{lufs}</td><td>{tp}</td>"
                    f"<td>{esc(', '.join(bad) or 'none')}</td></tr>")
    unresolved = "".join(f"<li>{esc(u)}</li>" for u in manifest["unresolved"])
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(name)} QA</title><style>{CSS}</style>"
            f"</head><body><h1>{esc(name)} - album QA</h1><h2 class='{esc(val['overall'])}'>{esc(val['overall'])}</h2>"
            f"<p>Technical: <b>{val['technical']}</b> &middot; Human gates: <b>{val['human_gates']}</b></p>"
            f"<p class='small'>{esc(val['note'])}</p><table><tr><th>#</th><th>file</th><th>version</th><th>LUFS</th>"
            f"<th>dBTP</th><th>open checks</th></tr>{''.join(rows)}</table><h2>Unresolved</h2><ul>{unresolved}</ul>"
            "</body></html>")


__all__ = ["AlbumMixin", "np"]
