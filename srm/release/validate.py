"""Independent release validation from the exported bytes (never from our own manifest claims alone).

The validator never raises on a damaged package: an unreadable manifest, a corrupt audio file or a
missing sidecar is reported as FAIL. The manifest itself is not trusted blindly either - the caller can
pass the SHA-256 recorded at export time, and every file in the package must be accounted for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from srm.analysis.loudness import measure_loudness
from srm.analysis.metrics import silence_metrics
from srm.analysis.truepeak import measure_peaks
from srm.core.paths import sha256_file
from srm.ingest.decode import decode
from srm.ingest.probe import probe
from srm.release.profiles import EXPORT_PROFILES

MAX_PATH = 240
_NAME = re.compile(r"^(\d{2})_.+\.(wav|mp3|flac)$", re.I)
TOP_LEVEL_FILES = {
    "album_metadata.json", "album_metadata.csv", "processing_manifest.json", "release_validation.json",
    "album_qa.html", "rights_ai_disclosure_checklist.json", "cover_art_checklist.json", "UNRESOLVED_WARNINGS.txt",
}
DELIVERY_DIRS = ("WAV_MASTER", "MP3_PREVIEW", "FLAC_ARCHIVE", "ARTWORK")
NOTE = ("Technical validation of exported bytes only. It is not legal, rights or aesthetic approval, and it "
        "does not publish anything. Check your distributor's current requirements before upload.")


def _chk(status: str, **kw) -> dict:
    return {"status": status, **kw}


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[-300:]


def failed_validation(pkg: str | Path, reason: str, profile: str | None = None) -> dict:
    """Result for a package that cannot be validated at all (same shape as a normal result)."""
    return {
        "package": str(pkg), "profile": profile, "overall": "FAILED", "technical": "FAIL",
        "human_gates": "PENDING", "unresolved_count": 0,
        "global_checks": {"package_readable": _chk("FAIL", error=reason)}, "tracks": [], "note": NOTE,
    }


def validate_package(pkg: str | Path, *, ceiling_dbtp: float | None = None, tolerance_db: float = 0.05,
                     expected_manifest_sha256: str | None = None) -> dict:
    pkg = Path(pkg)
    try:
        man = json.loads((pkg / "processing_manifest.json").read_text("utf-8"))
        meta = json.loads((pkg / "album_metadata.json").read_text("utf-8"))
        prof = EXPORT_PROFILES[man["profile"]]
        man_tracks = [t for t in man["tracks"] if isinstance(t, dict)]
        meta_tracks = list(meta["tracks"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return failed_validation(pkg, f"manifest or metadata missing/unreadable: {_err(exc)}")
    ceiling = ceiling_dbtp if ceiling_dbtp is not None else man.get("ceiling_dbtp", -1.0)
    tracks_out = []
    global_checks: dict = {}
    if expected_manifest_sha256:
        actual = sha256_file(pkg / "processing_manifest.json")
        global_checks["manifest_matches_export_record"] = _chk(
            "PASS" if actual == expected_manifest_sha256 else "FAIL",
            note=None if actual == expected_manifest_sha256 else "processing_manifest.json changed after export")
    artwork = man.get('artwork')
    expected_cover = artwork.get('sha256') if artwork else None
    artwork_rel = None
    if artwork:
        # Resolve only inside the package even when validating an externally edited manifest.
        try:
            from PIL import Image

            from srm.core.paths import ensure_within

            image_path = ensure_within(pkg / artwork['file'], pkg)
            artwork_rel = image_path.relative_to(pkg.resolve()).as_posix()
            image_ok = image_path.is_file() and sha256_file(image_path) == expected_cover
            if image_ok:
                with Image.open(image_path) as image:
                    image_ok = image.size == (3000, 3000) and image.mode == 'RGB'
            global_checks['album_cover'] = _chk('PASS' if image_ok else 'FAIL')
        except Exception as exc:
            global_checks['album_cover'] = _chk('FAIL', error=_err(exc))
    wavs = sorted((pkg / "WAV_MASTER").glob("*.wav"))
    names = [w.name for w in wavs]
    by_file = {t.get("file"): t for t in man_tracks}
    global_checks["track_count_matches_metadata"] = _chk(
        "PASS" if len(wavs) == len(meta_tracks) == len(man_tracks) and set(names) == set(by_file) else "FAIL",
        wav_files=len(wavs), metadata_tracks=len(meta_tracks), manifest_tracks=len(man_tracks))
    nums = [int(_NAME.match(n).group(1)) if _NAME.match(n) else None for n in names]
    global_checks["ordered_contiguous_numbering"] = _chk(
        "PASS" if nums == list(range(1, len(wavs) + 1)) else "FAIL", numbers=nums)
    lower = [n.lower() for n in names]
    global_checks["no_duplicate_names"] = _chk("PASS" if len(set(lower)) == len(lower) else "FAIL")
    too_long = [str(p) for p in pkg.rglob("*") if len(str(p)) > MAX_PATH]
    global_checks["path_lengths"] = _chk("PASS" if not too_long else "WARN", over_limit=too_long[:10], limit=MAX_PATH)
    # Every file must be accounted for: an orphan MP3/FLAC, a cover the manifest does not mention or an
    # extra audio file in a delivery folder is a failure; other stray files are a warning.
    expected = set(TOP_LEVEL_FILES)
    for w in wavs:
        expected |= {f"WAV_MASTER/{w.name}", f"MP3_PREVIEW/{w.stem}.mp3", f"FLAC_ARCHIVE/{w.stem}.flac",
                     f"QA/{w.stem}.json", f"QA/{w.stem}.html"}
    if artwork_rel:
        expected.add(artwork_rel)
    present = sorted(p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file())
    unexpected = [p for p in present if p not in expected]
    in_delivery = [p for p in unexpected if p.split("/")[0] in DELIVERY_DIRS]
    global_checks["no_unexpected_files"] = _chk(
        "FAIL" if in_delivery else ("WARN" if unexpected else "PASS"), unexpected=unexpected[:20])
    mp3_dir, flac_dir = pkg / "MP3_PREVIEW", pkg / "FLAC_ARCHIVE"
    for w in wavs:
        mt = by_file.get(w.name) or {}
        c: dict = {}
        c["listed_in_manifest"] = _chk("PASS" if w.name in by_file else "FAIL")
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
        except Exception as exc:  # DecodeError, tool timeout, unreadable file: a finding, not a crash
            c["decodes"] = _chk("FAIL", error=_err(exc))
        mp3 = mp3_dir / (w.stem + ".mp3")
        if mp3.exists():
            if expected_cover:
                try:
                    from srm.release.artwork import embedded_cover_hash

                    actual_cover = embedded_cover_hash(mp3)
                    c['embedded_album_cover'] = _chk('PASS' if actual_cover == expected_cover else 'FAIL',
                                                     sha256=actual_cover, expected=expected_cover)
                except Exception as exc:
                    c['embedded_album_cover'] = _chk('FAIL', error=_err(exc))
            try:
                mpr = probe(mp3)
                md = decode(mp3, sample_rate=mpr.sample_rate, channels=mpr.channels)
                tp = measure_peaks(md.audio, md.sample_rate)["true_peak_dbtp"]
                c["mp3_preview"] = _chk("PASS" if mpr.codec == "mp3" and tp <= 0.0 else "WARN", codec=mpr.codec,
                                        bit_rate=mpr.bit_rate, true_peak_dbtp=tp,
                                        note=None if tp <= 0.0 else "decoded MP3 exceeds 0 dBTP (codec overs)")
            except Exception as exc:
                c["mp3_preview"] = _chk("FAIL", error=_err(exc), note="MP3 preview cannot be decoded")
        elif mt.get('mp3_preview_required') or mp3_dir.is_dir():
            c['mp3_preview'] = _chk('FAIL', note='Required MP3 preview is missing')
        fl = flac_dir / (w.stem + ".flac")
        if fl.exists():
            try:
                fpr = probe(fl)
                a = decode(w, sample_rate=pr.sample_rate, channels=pr.channels, integer=True).audio
                b = decode(fl, sample_rate=fpr.sample_rate, channels=fpr.channels, integer=True).audio
                c["flac_lossless_vs_wav"] = _chk("PASS" if a.shape == b.shape and np.array_equal(a, b) else "FAIL")
            except Exception as exc:
                c["flac_lossless_vs_wav"] = _chk("FAIL", error=_err(exc))
        elif flac_dir.is_dir():
            c["flac_lossless_vs_wav"] = _chk("FAIL", note="FLAC archive copy is missing")
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
        "note": NOTE,
    }
