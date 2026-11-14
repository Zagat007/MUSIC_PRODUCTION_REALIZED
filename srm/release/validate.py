"""Independent release validation from the exported bytes (never from our own manifest claims alone)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from srm.analysis.loudness import measure_loudness
from srm.analysis.metrics import silence_metrics
from srm.analysis.truepeak import measure_peaks
from srm.core.paths import sha256_file
from srm.ingest.decode import DecodeError, decode
from srm.ingest.probe import probe
from srm.release.profiles import EXPORT_PROFILES

MAX_PATH = 240
_NAME = re.compile(r"^(\d{2})_.+\.(wav|mp3|flac)$", re.I)


def _chk(status: str, **kw) -> dict:
    return {"status": status, **kw}


def validate_package(pkg: str | Path, *, ceiling_dbtp: float | None = None, tolerance_db: float = 0.05) -> dict:
    pkg = Path(pkg)
    man = json.loads((pkg / "processing_manifest.json").read_text("utf-8"))
    meta = json.loads((pkg / "album_metadata.json").read_text("utf-8"))
    prof = EXPORT_PROFILES[man["profile"]]
    ceiling = ceiling_dbtp if ceiling_dbtp is not None else man.get("ceiling_dbtp", -1.0)
    tracks_out = []
    global_checks: dict = {}
    wavs = sorted((pkg / "WAV_MASTER").glob("*.wav"))
    names = [w.name for w in wavs]
    global_checks["track_count_matches_metadata"] = _chk(
        "PASS" if len(wavs) == len(meta["tracks"]) == len(man["tracks"]) else "FAIL",
        wav_files=len(wavs), metadata_tracks=len(meta["tracks"]))
    nums = [int(_NAME.match(n).group(1)) if _NAME.match(n) else None for n in names]
    global_checks["ordered_contiguous_numbering"] = _chk(
        "PASS" if nums == list(range(1, len(wavs) + 1)) else "FAIL", numbers=nums)
    lower = [n.lower() for n in names]
    global_checks["no_duplicate_names"] = _chk("PASS" if len(set(lower)) == len(lower) else "FAIL")
    too_long = [str(p) for p in pkg.rglob("*") if len(str(p)) > MAX_PATH]
    global_checks["path_lengths"] = _chk("PASS" if not too_long else "WARN", over_limit=too_long[:10], limit=MAX_PATH)
    for i, w in enumerate(wavs):
        mt = man["tracks"][i] if i < len(man["tracks"]) else {}
        c: dict = {}
        pr = probe(w)
        c["probe_ok"] = _chk("PASS" if pr.ok else "FAIL", error=pr.error or None)
        c["codec"] = _chk("PASS" if pr.codec == prof["codec"] else "FAIL", value=pr.codec, expected=prof["codec"])
        c["bit_depth"] = _chk("PASS" if pr.bits_per_sample == prof["bits"] else "FAIL", value=pr.bits_per_sample)
        c["sample_rate"] = _chk("PASS" if pr.sample_rate in prof["allowed_rates"] else "FAIL", value=pr.sample_rate)
        c["channels"] = _chk("PASS" if pr.channels == prof["channels"] else "FAIL", value=pr.channels)
        sha = sha256_file(w)
        c["checksum_matches_manifest"] = _chk("PASS" if sha == mt.get("wav_sha256") else "FAIL", sha256=sha)
        try:
            d = decode(w, sample_rate=pr.sample_rate, channels=pr.channels)
            x = d.audio
            c["decodes"] = _chk("PASS" if not d.warnings else "WARN", warnings=d.warnings[:3])
            src_dur = mt.get("source_duration_s")
            dur = d.duration_s
            c["duration_vs_source"] = _chk(
                "PASS" if src_dur is None or abs(dur - src_dur) < 0.05 else "FAIL",
                exported_s=round(dur, 4), source_s=src_dur)
            sil = silence_metrics(x, d.sample_rate)
            c["not_silent"] = _chk("FAIL" if sil.get("entirely_silent") or sil.get("silent_channels") else "PASS")
            c["no_unintended_edge_silence"] = _chk(
                "WARN" if sil.get("leading_silence_s", 0) > 2.0 or sil.get("trailing_silence_s", 0) > 5.0 else "PASS",
                leading_s=sil.get("leading_silence_s"), trailing_s=sil.get("trailing_silence_s"))
            L = measure_loudness(x, d.sample_rate)
            P = measure_peaks(x, d.sample_rate)
            c["true_peak_vs_policy"] = _chk("PASS" if P["true_peak_dbtp"] <= ceiling + tolerance_db else "FAIL",
                                            value=P["true_peak_dbtp"], ceiling=ceiling, tolerance=tolerance_db)
            c["loudness_measured"] = _chk("INFO", integrated_lufs=L.summary()["integrated_lufs"],
                                          lra_lu=L.summary()["lra_lu"],
                                          vs_version_report=mt.get("version_integrated_lufs"))
            vi = mt.get("version_integrated_lufs")
            if vi is not None and L.integrated_lufs is not None:
                c["loudness_matches_render"] = _chk("PASS" if abs(L.integrated_lufs - vi) < 0.1 else "FAIL",
                                                    delta=round(L.integrated_lufs - vi, 3))
        except DecodeError as exc:
            c["decodes"] = _chk("FAIL", error=str(exc)[-300:])
        mp3 = pkg / "MP3_PREVIEW" / (w.stem + ".mp3")
        if mp3.exists():
            mpr = probe(mp3)
            md = decode(mp3, sample_rate=mpr.sample_rate, channels=mpr.channels)
            tp = measure_peaks(md.audio, md.sample_rate)["true_peak_dbtp"]
            c["mp3_preview"] = _chk("PASS" if mpr.codec == "mp3" and tp <= 0.0 else "WARN", codec=mpr.codec,
                                    bit_rate=mpr.bit_rate, true_peak_dbtp=tp,
                                    note=None if tp <= 0.0 else "decoded MP3 exceeds 0 dBTP (codec overs)")
        fl = pkg / "FLAC_ARCHIVE" / (w.stem + ".flac")
        if fl.exists():
            fpr = probe(fl)
            a = decode(w, sample_rate=pr.sample_rate, channels=pr.channels, integer=True).audio
            b = decode(fl, sample_rate=fpr.sample_rate, channels=fpr.channels, integer=True).audio
            c["flac_lossless_vs_wav"] = _chk("PASS" if a.shape == b.shape and np.array_equal(a, b) else "FAIL")
        c["listening_signoff"] = _chk("PASS" if mt.get("listening_signoff") else "PENDING",
                                      note="Human listening sign-off recorded" if mt.get("listening_signoff") else
                                      "No listening sign-off recorded for this version")
        c["version_approved"] = _chk("PASS" if mt.get("version_status") == "APPROVED" else "PENDING",
                                     value=mt.get("version_status"))
        tracks_out.append({"file": w.name, "checks": c})
    tech_fail = any(v["status"] == "FAIL" for v in global_checks.values()) or any(
        v["status"] == "FAIL" for t in tracks_out for v in t["checks"].values())
    human_pending = any(v["status"] == "PENDING" for t in tracks_out for v in t["checks"].values())
    unresolved = man.get("unresolved", [])
    if tech_fail:
        overall = "FAILED"
    elif human_pending or unresolved:
        overall = "DRAFT_NOT_RELEASE_READY"
    else:
        overall = "TECHNICAL_PASS_AND_HUMAN_GATES_RECORDED"
    return {
        "package": str(pkg), "profile": man["profile"], "overall": overall,
        "technical": "FAIL" if tech_fail else "PASS",
        "human_gates": "PENDING" if human_pending else "RECORDED",
        "unresolved_count": len(unresolved),
        "global_checks": global_checks, "tracks": tracks_out,
        "note": ("Technical validation of exported bytes only. It is not legal, rights or aesthetic approval, and it "
                 "does not publish anything. Check your distributor's current requirements before upload."),
    }
