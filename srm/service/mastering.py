"""Mastering workflow on top of the Engine: propose -> render -> A-B preview -> codec preview."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from srm import __version__
from srm.analysis.analyze import analyze_audio, technical_status
from srm.analysis.loudness import integrated_lufs, measure_loudness
from srm.analysis.report import strip_timelines
from srm.analysis.truepeak import db, measure_peaks, peak_envelope
from srm.core.paths import atomic_write_text, next_version_dir, safe_name, sha256_file
from srm.core.tools import ffmpeg_input, get_tools, run
from srm.dsp.chain import run_chain, validate_chain
from srm.dsp.convert import plan_conversion
from srm.dsp.proposal import propose
from srm.dsp.quantize import write_float
from srm.ingest.decode import decode
from srm.ingest.probe import probe


class MasteringMixin:
    # ------------------------------------------------------------- propose
    def suggest_mastering(self, track_id: int, *, mode: str = "SAFE_MASTER", preset: str | None = None,
                          ceiling_dbtp: float = -1.0, loudness_policy: str = "preset_window",
                          target_lufs: float | None = None, album_offset_db: float | None = None,
                          chain_overrides: dict | None = None, target_bits: int = 24, save: bool = True) -> dict:
        """Return (and optionally store as a PROPOSED version) an explainable proposal. Never renders."""
        from srm.service.engine import EngineError

        a = self.get_analysis(track_id) or self.analyze_track(track_id)
        t = self.track(track_id)
        if t["style_override"] and not preset:
            preset = t["style_override"]
        prop = propose(a["measurements"], mode=mode, preset=preset, ceiling_dbtp=ceiling_dbtp,
                       target_lufs=target_lufs, loudness_policy=loudness_policy, album_offset_db=album_offset_db)
        if chain_overrides:
            merged = json.loads(json.dumps(prop["chain"]))
            for st, params in chain_overrides.items():
                merged.setdefault(st, {}).update(params)
            prop["chain"] = validate_chain(merged)
            prop["user_overrides"] = chain_overrides
        if mode == "RESTORE_AND_MASTER":
            rp = getattr(self, "approved_repair_plan", lambda _tid: None)(track_id)
            if not rp:
                raise EngineError("RESTORE_AND_MASTER needs an approved repair plan for this track "
                                  "(review candidates, approve edits first)")
            prop["repair_plan"] = rp
        cfg = {"mode": mode, "chain": prop["chain"], "target_bits": target_bits, "allowed_rates": [44100, 48000],
               "proposal": {k: prop[k] for k in ("preset", "preset_reason", "actions")},
               "repair_plan": prop.get("repair_plan"), "app_version": __version__,
               "source_sha256": t["sha256"]}
        if save:
            v = self._new_version(track_id, mode, cfg, "PROPOSED")
            prop["version_no"] = v["version_no"]
        prop["config"] = cfg
        return prop

    # ------------------------------------------------------------- render
    def render_version(self, track_id: int, version_no: int, *, codec_preview: bool = True) -> dict:
        from srm.service.engine import EngineError, _jdump

        v = self.version(track_id, version_no)
        if v["status"] != "PROPOSED":
            raise EngineError(f"version {version_no} is {v['status']}; versions are immutable - propose a new one")
        t = self.track(track_id)
        if t["sha256"] != v["config"].get("source_sha256", t["sha256"]):
            raise EngineError("source changed since proposal")
        self._check_source(t)
        self._check_disk()
        cfg = v["config"]
        self.db.execute("UPDATE versions SET status='RENDERING' WHERE id=?", (v["id"],))
        try:
            pr = self.probe_result(t)
            plan = plan_conversion(pr, cfg["target_bits"], tuple(cfg["allowed_rates"]))
            dec = decode(t["path"], sample_rate=pr.sample_rate, channels=pr.channels,
                         target_rate=plan.target_rate if plan.resample else None)
            x = dec.audio.astype(np.float64)
            if x.shape[1] == 1:
                x = np.repeat(x, 2, axis=1)
            repair_log = None
            if cfg.get("repair_plan"):
                from srm.dsp.repair import apply_repair_plan

                x, repair_log = apply_repair_plan(x, plan.target_rate, cfg["repair_plan"])
            res = run_chain(x, plan.target_rate, cfg["chain"])
            out_dir = self._render_dir(t, v["version_no"])
            out = out_dir / f"{safe_name(t['title'] or 'track')}.wav"
            qinfo = write_float(out, res.audio, plan.target_rate, cfg["target_bits"], dither="tpdf",
                                seed=t["id"] * 1000 + v["version_no"], clip_policy="fail")
            rec = {"mode": cfg["mode"], "plan": plan.__dict__, "stages": res.stages, "quantize": qinfo,
                   "resampler": dec.resampler or None, "repair_log": repair_log,
                   "decoder_warnings": dec.warnings}
            report = self._finalize_render(t, {"id": v["id"], "version_no": v["version_no"]}, out, rec, cfg)
            if codec_preview:
                cover = self.album_artwork(t['album_id'])['cover']
                report["codec_preview"] = self.codec_preview(out, cfg["chain"]["limiter"]["ceiling_dbtp"],
                                                            cover_path=cover['path'] if cover else None)
                self.db.execute("UPDATE versions SET report_json=? WHERE id=?",
                                (_jdump(_strip(report)), v["id"]))
            report["policy_check"] = self._policy_check(report, cfg)
            self.db.execute("UPDATE versions SET report_json=? WHERE id=?", (_jdump(_strip(report)), v["id"]))
            return report
        except Exception as exc:
            self.db.execute("UPDATE versions SET status='FAILED', report_json=? WHERE id=?",
                            (_jdump({"error": f"{type(exc).__name__}: {exc}"}), v["id"]))
            raise

    def _policy_check(self, report: dict, cfg: dict) -> dict:
        om = report["output_measurements"]
        ceiling = cfg["chain"]["limiter"]["ceiling_dbtp"] if cfg["chain"]["limiter"]["enabled"] else 0.0
        tp = om["peaks"]["true_peak_dbtp"]
        checks = {"true_peak_within_ceiling": {"value": tp, "limit": ceiling, "tolerance": 0.05,
                                               "status": "PASS" if tp <= ceiling + 0.05 else "FAIL"}}
        cp = report.get("codec_preview")
        if cp and cp.get("status") == "OK":
            checks["mp3_preview_true_peak"] = {"value": cp["true_peak_dbtp"], "limit": 0.0,
                                               "status": "PASS" if cp["true_peak_dbtp"] <= 0.0 else "WARN",
                                               "note": cp.get("advice")}
        sd = report["comparison"]["duration_s"]["delta"] if report.get("comparison") else None
        if sd is not None:
            checks["duration_preserved"] = {"delta_s": sd, "status": "PASS" if abs(sd) < 0.01 else "FAIL"}
        return checks

    # ------------------------------------------------------------- codec preview
    def codec_preview(self, wav_path: str | Path, ceiling_dbtp: float, bitrate: str = "320k",
                      cover_path: str | Path | None = None) -> dict:
        t = get_tools()
        if not t.has_libmp3lame:
            return {"status": "UNAVAILABLE", "reason": "FFmpeg build lacks libmp3lame"}
        wav_path = Path(wav_path)
        mp3 = wav_path.with_name(wav_path.stem + "_codec_check.mp3")
        if mp3.exists():
            return {"status": "SKIPPED", "reason": "already exists", "path": str(mp3)}
        p = run([t.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", ffmpeg_input(wav_path),
                 "-c:a", "libmp3lame", "-b:a", bitrate, "-map_metadata", "-1", ffmpeg_input(mp3)])
        if p.returncode != 0:
            return {"status": "FAILED", "error": p.stderr.decode("utf-8", "replace")[-500:]}
        if cover_path:
            from srm.release.artwork import attach_cover

            attach_cover(mp3, Path(cover_path))
        pr = probe(mp3)
        d = decode(mp3, sample_rate=pr.sample_rate, channels=pr.channels)
        tp = measure_peaks(d.audio, d.sample_rate)
        advice = None
        if tp["true_peak_dbtp"] > 0.0:
            advice = (f"MP3 decode peaks at {tp['true_peak_dbtp']:+.2f} dBTP (codec overs). Consider a "
                      f"{ceiling_dbtp - 1.0:.1f} dBTP ceiling for lossy-heavy distribution.")
        return {"status": "OK", "path": str(mp3), "bitrate": bitrate, "true_peak_dbtp": tp["true_peak_dbtp"],
                "sample_peak_dbfs": tp["sample_peak_dbfs"], "advice": advice,
                "integrated_lufs": round(integrated_lufs(d.audio, d.sample_rate) or -99, 2)}

    # ------------------------------------------------------------- A-B preview
    def create_preview(self, track_id: int, version_no: int | None = None, *, start_s: float | None = None,
                       duration_s: float = 30.0, include_mp3: bool = True) -> dict:
        """Loudness-matched audition clips: source, conversion, (repair/master) candidate, encoded preview."""
        from srm.service.engine import EngineError

        t = self.track(track_id)
        pr = self.probe_result(t)
        plan = plan_conversion(pr, 24)
        src = decode(t["path"], sample_rate=pr.sample_rate, channels=pr.channels,
                     target_rate=plan.target_rate if plan.resample else None)
        fs = src.sample_rate
        xs = src.audio.astype(np.float64)
        if xs.shape[1] == 1:
            xs = np.repeat(xs, 2, axis=1)
        n = xs.shape[0]
        seg = int(min(duration_s, n / fs) * fs)
        if start_s is None:
            st = measure_loudness(xs, fs).short_term
            if st.size:
                # pick the loudest region (3 s short-term index * 0.1 s hop), centred
                i = int(np.nanargmax(np.where(np.isfinite(st), st, -np.inf)))
                start = max(0, min(n - seg, int((i * 0.1 + 1.5) * fs) - seg // 2))
            else:
                start = 0
        else:
            start = max(0, min(n - seg, int(start_s * fs)))
        clips = {"source_decoded": xs[start:start + seg]}
        if version_no is not None:
            v = self.version(track_id, version_no)
            if not v["output_path"]:
                raise EngineError("version not rendered yet")
            opr = probe(v["output_path"])
            od = decode(v["output_path"], sample_rate=opr.sample_rate, channels=opr.channels)
            if od.sample_rate != fs:
                raise EngineError("sample-rate mismatch between source decode and render")
            clips[f"v{version_no:03d}_{v['mode'].lower()}"] = od.audio.astype(np.float64)[start:start + seg]
            mp3 = Path(v["output_path"]).with_name(Path(v["output_path"]).stem + "_codec_check.mp3")
            if include_mp3 and mp3.exists():
                mpr = probe(mp3)
                md = decode(mp3, sample_rate=mpr.sample_rate, channels=mpr.channels).audio.astype(np.float64)
                # MP3 decoders add encoder delay; align by cross-correlation on a short window
                lag = _align_lag(od.audio[:, 0], md[:, 0], fs)
                md = md[lag:] if lag >= 0 else np.pad(md, ((-lag, 0), (0, 0)))
                clips["encoded_mp3_preview"] = md[start:start + seg]
        loud = {k: integrated_lufs(c, fs) for k, c in clips.items()}
        valid = [v for v in loud.values() if v is not None]
        ref = min(valid) if valid else None
        out_dir = next_version_dir(self.root / "previews" / f"{t['id']:05d}", prefix="p")
        files = {}
        for k, c in clips.items():
            g = 0.0 if (ref is None or loud[k] is None) else ref - loud[k]
            y = c * 10 ** (g / 20)
            pk = float(peak_envelope(y, fs).max()) if y.size else 0.0
            if pk > 0.999:  # never let an audition clip clip
                y *= 0.999 / pk
                g += db(0.999 / pk)
            p = out_dir / f"{k}_LM.wav"
            write_float(p, y, fs, 24, dither="tpdf", seed=1, clip_policy="fail")
            files[k] = {"path": str(p), "measured_lufs": None if loud[k] is None else round(loud[k], 2),
                        "matching_gain_db": round(g, 2)}
        info = {"track_id": track_id, "version_no": version_no, "start_s": round(start / fs, 3),
                "duration_s": round(seg / fs, 3), "sample_rate": fs, "reference_lufs": None if ref is None else round(ref, 2),
                "clips": files,
                "note": "Clips are loudness-matched for fair A-B (louder is not better). Original branch retained."}
        atomic_write_text(out_dir / "preview.json", json.dumps(info, ensure_ascii=False, indent=1))
        self.db.audit("preview", {"track_id": track_id, "version": version_no, "dir": str(out_dir)})
        return info

    # ------------------------------------------------------------- album cohesion
    def album_cohesion(self, album) -> dict:
        tracks = self.list_tracks(album)
        rows = []
        for tr in tracks:
            a = self.get_analysis(tr["id"])
            if not a:
                rows.append({"track_id": tr["id"], "title": tr["title"], "status": "NOT_ANALYZED"})
                continue
            m = a["measurements"]
            b = m["spectral"]["band_energy_db_rel"]
            rows.append({"track_id": tr["id"], "track_no": tr["track_no"], "title": tr["title"],
                         "integrated_lufs": m["loudness"]["integrated_lufs"], "lra_lu": m["loudness"]["lra_lu"],
                         "true_peak_dbtp": m["peaks"]["true_peak_dbtp"],
                         "centroid_hz": m["spectral"]["spectral_centroid_hz"], "bass_rel_db": b.get("bass_40_120"),
                         "side_to_mid_db": m["stereo"].get("side_to_mid_db"),
                         "style_top": m["style"]["ranked"][0]["style"]})
        valid = [r for r in rows if r.get("integrated_lufs") is not None]
        if not valid:
            return {"album": album, "tracks": rows, "status": "INSUFFICIENT_DATA"}
        med = float(np.median([r["integrated_lufs"] for r in valid]))
        cmed = float(np.median([r["centroid_hz"] for r in valid if r["centroid_hz"]] or [0]))
        bmed = float(np.median([r["bass_rel_db"] for r in valid if r["bass_rel_db"] is not None] or [0]))
        for r in valid:
            r["rel_loudness_lu"] = round(r["integrated_lufs"] - med, 2)
            r["rel_brightness_pct"] = round((r["centroid_hz"] / cmed - 1) * 100, 1) if cmed else None
            r["rel_bass_db"] = round(r["bass_rel_db"] - bmed, 2) if r["bass_rel_db"] is not None else None
            flags = []
            if abs(r["rel_loudness_lu"]) > 4:
                flags.append("loudness differs >4 LU from album median (intentional interlude?)")
            if r["rel_brightness_pct"] is not None and abs(r["rel_brightness_pct"]) > 35:
                flags.append("brightness differs strongly")
            if r["rel_bass_db"] is not None and abs(r["rel_bass_db"]) > 4:
                flags.append("bass balance differs strongly")
            r["flags"] = flags
            # suggestion: pull outliers halfway toward the median, never force equal LUFS
            r["suggested_album_offset_db"] = round(-0.5 * r["rel_loudness_lu"], 2) if abs(r["rel_loudness_lu"]) > 2 else 0.0
        return {"album": album, "median_lufs": round(med, 2), "tracks": rows, "status": "OK",
                "note": ("Suggestions preserve intended contrast (half-way moves only for >2 LU outliers); "
                         "identical LUFS per track is NOT enforced.")}


def _align_lag(ref: np.ndarray, sig: np.ndarray, fs: int, max_lag_s: float = 0.1) -> int:
    n = min(len(ref), len(sig), fs * 10)
    a = ref[:n] - ref[:n].mean()
    b = sig[:n] - sig[:n].mean()
    m = int(max_lag_s * fs)
    N = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(b, N) * np.conj(np.fft.rfft(a, N)), N)
    lags = np.concatenate([c[: m + 1], c[-m:]])
    idx = int(np.argmax(lags))
    return idx if idx <= m else idx - len(lags)


def _strip(report: dict) -> dict:
    r = dict(report)
    if r.get("output_measurements"):
        r["output_measurements"] = strip_timelines(r["output_measurements"])
    r.pop("measurements", None)
    return r


__all__ = ["MasteringMixin", "analyze_audio", "technical_status", "sha256_file"]
