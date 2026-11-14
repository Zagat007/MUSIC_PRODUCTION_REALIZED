"""Produce reproducible QA evidence: run the real CLI workflow on synthetic fixtures, log every command with its
exit code, probe exported files with ffprobe (raw JSON), and write SHA-256 checksums.

Usage: python scripts/make_qa_evidence.py <work_dir> [--exe dist/srm/srm.exe]
Writes evidence to tests/qa/evidence/ (small text files only; audio stays in <work_dir>).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "audio_fixtures"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--exe")
    a = ap.parse_args()
    from make_fixtures import make_all
    from srm.core.paths import sha256_file
    from srm.core.tools import get_tools

    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = work / f"evidence_{stamp}"
    lib = Path(make_all(run_dir / "fx")["library"])
    proj = run_dir / "project"
    ev = ROOT / "tests" / "qa" / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    base = [str(Path(a.exe).resolve())] if a.exe else [sys.executable, "-m", "srm.cli.main"]
    log = []

    def srm(*args, ok=(0,)):
        cmd = base + list(args)
        t0 = time.time()
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=3600)
        rec = {"cmd": " ".join(f'"{c}"' if " " in c else c for c in cmd[len(base):]), "exit_code": p.returncode,
               "seconds": round(time.time() - t0, 1), "stderr_tail": p.stderr.decode("utf-8", "replace")[-300:]}
        log.append(rec)
        print(f"[{p.returncode}] srm {rec['cmd']}")
        if p.returncode not in ok:
            raise SystemExit(f"unexpected exit code {p.returncode}: {rec['stderr_tail']}")
        return p.stdout.decode("utf-8", "replace")

    album_dir = lib / "Album A"
    src_hashes = {str(p.relative_to(lib)): sha256_file(p) for p in lib.rglob("*") if p.is_file()}
    srm("scan", str(lib), "--project", str(proj))
    srm("analyze", "--project", str(proj), ok=(0, 2))  # 2 = completed with errors (corrupt fixtures)
    tracks = json.loads(srm("tracks", "--project", str(proj), "--album", "Album A", "--json"))
    ids = [str(t["id"]) for t in tracks]
    srm("propose", "--project", str(proj), "--album", "Album A", "--mode", "safe_master")
    srm("render", "--project", str(proj), "--album", "Album A")
    for tid in ids:
        srm("approve", "--project", str(proj), "--track-id", tid, "--version", "1", "--note", "evidence run")
    srm("export", "--project", str(proj), "--album", "Album A", ok=(1,))  # must be refused without --confirm
    out = srm("export", "--project", str(proj), "--album", "Album A", "--confirm", "--flac")
    exp = json.loads(out)
    srm("validate", "--project", str(proj), "--album", "Album A", "--full")
    verify = json.loads(srm("verify-sources", "--project", str(proj)))
    pkg = Path(exp["path"])
    t = get_tools()
    probes, sums = {}, []
    for f in sorted(pkg.rglob("*")):
        if f.is_file():
            sums.append(f"{sha256_file(f)}  {f.relative_to(pkg).as_posix()}")
            if f.suffix.lower() in (".wav", ".mp3", ".flac"):
                p = subprocess.run([t.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                                    "file:" + str(f)], capture_output=True)
                probes[f.relative_to(pkg).as_posix()] = json.loads(p.stdout.decode("utf-8"))
    after = {str(p.relative_to(lib)): sha256_file(p) for p in lib.rglob("*") if p.is_file()}
    (ev / "cli_run_log.json").write_text(json.dumps({"ffmpeg": t.version_line, "commands": log}, indent=1,
                                                    ensure_ascii=False), "utf-8")
    (ev / "ffprobe_exports.json").write_text(json.dumps(probes, indent=1, ensure_ascii=False), "utf-8")
    (ev / "export_checksums.sha256").write_text("\n".join(sums) + "\n", "utf-8")
    (ev / "source_hashes_unchanged.json").write_text(json.dumps(
        {"unchanged": src_hashes == after, "files": len(src_hashes), "verify_sources": verify}, indent=1), "utf-8")
    for n in ("release_validation.json", "album_qa.html", "processing_manifest.json", "UNRESOLVED_WARNINGS.txt",
              "album_metadata.csv", "rights_ai_disclosure_checklist.json"):
        shutil.copyfile(pkg / n, ev / n)
    print("evidence written to", ev, "| export status:", exp["status"], "| sources unchanged:", src_hashes == after)
    _ = album_dir
    return 0


if __name__ == "__main__":
    sys.exit(main())
