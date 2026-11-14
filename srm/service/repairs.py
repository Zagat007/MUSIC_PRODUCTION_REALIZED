"""Repair-plan workflow (RESTORE_AND_MASTER) and stem import/validation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from srm.analysis.loudness import integrated_lufs
from srm.core.paths import atomic_write_text, ensure_within, next_version_dir
from srm.dsp.quantize import write_float
from srm.dsp.repair import apply_edit, plan_from_candidates, regression_check
from srm.ingest.decode import decode
from srm.ingest.probe import probe

STEM_EXT = {".wav", ".flac", ".m4a", ".aif", ".aiff"}


class RepairMixin:
    # ------------------------------------------------------------ plans
    def propose_repairs(self, track_id: int) -> dict:
        from srm.service.engine import _jdump

        a = self.get_analysis(track_id) or self.analyze_track(track_id)
        t = self.track(track_id)
        d = a["measurements"]["defects"]
        plan = plan_from_candidates(d["candidates"], d["listening_required"])
        x, fs = self._source_float(t)
        for ed in plan["edits"]:
            if t["channels"] == 1:
                ed["channel"] = None  # mono source is processed as dual mono: repair both output channels
            try:
                y = apply_edit(x, fs, ed)
                ed["regression"] = regression_check(x, y, fs, ed)
            except Exception as exc:
                ed["regression"] = {"passed": False, "error": str(exc)}
            if ed["auto_eligible"] and ed["regression"].get("passed"):
                ed["decision"] = "AUTO_ACCEPTED_PENDING_CONFIRMATION"
        pid = self.db.execute("INSERT INTO repair_plans (track_id, source_sha, plan_json, status, created_at) "
                              "VALUES (?,?,?,?,?)", (track_id, t["sha256"], _jdump(plan), "PROPOSED", time.time())).lastrowid
        plan["plan_id"] = pid
        self.db.audit("repair_plan_proposed", {"track_id": track_id, "plan_id": pid, "edits": len(plan["edits"])})
        return plan

    def repair_plan(self, track_id: int, plan_id: int) -> dict:
        r = self.db.one("SELECT * FROM repair_plans WHERE id=? AND track_id=?", (int(plan_id), int(track_id)))
        if not r:
            from srm.service.engine import EngineError

            raise EngineError("repair plan not found")
        p = json.loads(r["plan_json"])
        p.update(plan_id=r["id"], status=r["status"], source_sha=r["source_sha"])
        return p

    def decide_repairs(self, track_id: int, plan_id: int, *, approve: list[int] | None = None,
                       reject: list[int] | None = None, accept_auto: bool = False, actor: str = "local-user") -> dict:
        """Record explicit decisions. AUTO edits apply only if the user accepts the auto set (accept_auto)."""
        from srm.service.engine import EngineError, _jdump

        p = self.repair_plan(track_id, plan_id)
        if p["status"] not in ("PROPOSED", "PARTIALLY_DECIDED"):
            raise EngineError(f"plan is {p['status']}")
        approve, reject = set(approve or []), set(reject or [])
        for ed in p["edits"]:
            if ed["edit_id"] in reject:
                ed["decision"] = "REJECTED"
            elif ed["edit_id"] in approve:
                if not (ed.get("regression") or {}).get("passed"):
                    raise EngineError(f"edit {ed['edit_id']} failed regression checks and cannot be approved")
                ed["decision"] = "APPROVED"
            elif accept_auto and ed["decision"] == "AUTO_ACCEPTED_PENDING_CONFIRMATION":
                ed["decision"] = "AUTO_ACCEPTED"
        pending = [e for e in p["edits"] if e["decision"] in ("PENDING", "AUTO_ACCEPTED_PENDING_CONFIRMATION")]
        status = "APPROVED" if not pending else "PARTIALLY_DECIDED"
        body = {k: p[k] for k in ("edits", "not_repairable", "policy")}
        self.db.execute("UPDATE repair_plans SET plan_json=?, status=? WHERE id=?", (_jdump(body), status, plan_id))
        self.db.audit("repair_decision", {"track_id": track_id, "plan_id": plan_id, "approve": sorted(approve),
                                          "reject": sorted(reject), "accept_auto": accept_auto, "actor": actor})
        return self.repair_plan(track_id, plan_id)

    def approved_repair_plan(self, track_id: int) -> dict | None:
        t = self.track(track_id)
        r = self.db.one("SELECT id FROM repair_plans WHERE track_id=? AND source_sha=? AND status='APPROVED' "
                        "ORDER BY id DESC LIMIT 1", (track_id, t["sha256"]))
        if not r:
            return None
        p = self.repair_plan(track_id, r["id"])
        applied = [e for e in p["edits"] if e["decision"] in ("APPROVED", "AUTO_ACCEPTED")]
        return {"plan_id": p["plan_id"], "edits": applied}

    def preview_repair(self, track_id: int, plan_id: int, edit_id: int, *, context_s: float = 1.5) -> dict:
        """Loudness-matched A-B clips around one edit: original, repaired, and difference."""
        from srm.service.engine import EngineError

        p = self.repair_plan(track_id, plan_id)
        ed = next((e for e in p["edits"] if e["edit_id"] == int(edit_id)), None)
        if not ed:
            raise EngineError("edit not found")
        t = self.track(track_id)
        x, fs = self._source_float(t)
        y = apply_edit(x, fs, dict(ed))
        s = max(0, int((ed["start_s"] - context_s) * fs))
        e = min(x.shape[0], int((ed["end_s"] + context_s) * fs))
        out_dir = next_version_dir(self.root / "previews" / f"{t['id']:05d}" / f"repair_{plan_id}_{edit_id}", prefix="p")
        files = {}
        for name, clip in (("original", x[s:e]), ("repaired", y[s:e]), ("difference_x4", (y[s:e] - x[s:e]) * 4)):
            pk = float(np.abs(clip).max()) or 1.0
            g = min(1.0, 0.98 / pk)
            fp = out_dir / f"{name}.wav"
            write_float(fp, clip * g, fs, 24, dither="none", clip_policy="fail")
            files[name] = str(fp)
        info = {"edit": ed, "clip_start_s": s / fs, "files": files,
                "loudness": {"original": integrated_lufs(x[s:e], fs), "repaired": integrated_lufs(y[s:e], fs)},
                "note": "Listen to 'difference' to hear exactly what the repair removed."}
        atomic_write_text(out_dir / "repair_preview.json", json.dumps(info, ensure_ascii=False, indent=1, default=str))
        return info

    def _source_float(self, t: dict) -> tuple[np.ndarray, int]:
        from srm.dsp.convert import plan_conversion

        pr = self.probe_result(t)
        plan = plan_conversion(pr, 24)
        d = decode(t["path"], sample_rate=pr.sample_rate, channels=pr.channels,
                   target_rate=plan.target_rate if plan.resample else None)
        x = d.audio.astype(np.float64)
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        return x, d.sample_rate

    # ------------------------------------------------------------ stems
    def separation_capability(self) -> dict:
        import importlib.util

        mods = {m: importlib.util.find_spec(m) is not None for m in ("demucs", "audio_separator", "openunmix")}
        return {"status": "UNAVAILABLE" if not any(mods.values()) else "INSTALLED_NOT_ENABLED",
                "modules_found": mods,
                "policy": ("Model-based separation is optional and not bundled (licence/maintenance review needed; "
                           "upstream Demucs is unmaintained). Prefer importing original Suno stems."),
                "warning": "Separation artifacts can degrade the stereo mix; never replace the original mix silently."}

    def import_stems(self, track_id: int, folder: str | Path) -> dict:
        """Register original stems for a track (read-only) and validate them against the mix."""
        from srm.service.engine import EngineError

        folder = Path(folder).resolve()
        roots = [Path(r["path"]) for r in self.library_roots()]
        if not any(_inside(folder, r) for r in roots):
            raise EngineError("stem folder must be inside an authorized library root")
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in STEM_EXT and p.is_file())
        if not files:
            raise EngineError("no stem audio files found")
        t = self.track(track_id)
        mix, fs = self._source_float(t)
        total = np.zeros_like(mix)
        stems = []
        for f in files:
            pr = probe(f)
            if not pr.ok:
                stems.append({"path": str(f), "status": "PROBE_FAILED"})
                continue
            d = decode(f, sample_rate=pr.sample_rate, channels=pr.channels,
                       target_rate=fs if pr.sample_rate != fs else None)
            s = d.audio.astype(np.float64)
            if s.shape[1] == 1:
                s = np.repeat(s, 2, axis=1)
            n = min(len(s), len(total))
            total[:n] += s[:n]
            stems.append({"path": str(f), "codec": pr.codec, "is_lossy": pr.is_lossy, "sample_rate": pr.sample_rate,
                          "duration_s": pr.duration_s, "length_matches_mix": abs((pr.duration_s or 0) - t["duration_s"]) < 0.05})
        res = total - mix
        null_db = 20 * np.log10((np.sqrt(np.mean(res ** 2)) + 1e-12) / (np.sqrt(np.mean(mix ** 2)) + 1e-12))
        verdict = "CONSISTENT" if null_db < -30 else ("APPROXIMATE" if null_db < -12 else "DOES_NOT_SUM_TO_MIX")
        info = {"track_id": track_id, "folder": str(folder), "stems": stems, "sum_vs_mix_residual_db": round(null_db, 2),
                "verdict": verdict,
                "usage": ("Stems are registered for manual/external stem-based repair. The application does not "
                          "remix stems automatically.")}
        self._mutate_meta("tracks", track_id, lambda meta: meta.__setitem__("stems", info))
        self.db.audit("stems_imported", {"track_id": track_id, "verdict": verdict, "n": len(stems)})
        return info


def _inside(p: Path, root: Path) -> bool:
    try:
        ensure_within(p, root)
        return True
    except Exception:
        return False
