"""`srm` command-line interface. Uses the same Engine as the GUI and MCP server."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from srm import __version__


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _engine(args):
    from srm.service.engine import Engine

    return Engine(args.project, create=getattr(args, "create", False) or args.cmd == "scan")


def _track_ids(e, args) -> list[int]:
    if getattr(args, "track_id", None):
        return [int(t) for t in args.track_id]
    tracks = e.list_tracks(args.album) if getattr(args, "album", None) else e.list_tracks()
    return [t["id"] for t in tracks]


def _run_job(e, jid: int, args) -> dict:
    ev = threading.Event()

    def on_sigint(sig, frm):
        print("\n[srm] cancel requested; finishing current items (resume later with `srm job resume`)",
              file=sys.stderr)
        e.cancel_job(jid)

    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        def prog(st):
            print(f"\r[job {jid}] {st['done']}/{st['total_items']} done, {st['failed']} failed", end="",
                  file=sys.stderr, flush=True)
        st = e.run_job(jid, workers=getattr(args, "workers", None), progress=prog,
                       retry_failed=getattr(args, "retry_failed", False),
                       stop_after=getattr(args, "stop_after", None))
        print(file=sys.stderr)
        return st
    finally:
        signal.signal(signal.SIGINT, old)
        ev.set()


def cmd_health(e, a):
    _out(e.health())


def cmd_scan(e, a):
    rid = e.add_library_root(a.library, exclude=a.exclude)
    s = e.scan(rid, verify_decode=a.verify_decode,
               progress=lambda i, n, p: print(f"\r[scan] {i + 1}/{n}", end="", file=sys.stderr))
    print(file=sys.stderr)
    _out(s)


def cmd_albums(e, a):
    _out(e.list_albums())


def cmd_tracks(e, a):
    rows = e.list_tracks(a.album)
    if a.json:
        _out(rows)
        return
    for t in rows:
        lv = t["latest_version"]
        print(f"{t['id']:>5}  {t['album_name'][:24]:<24} {t['track_no'] or '':>3} {t['title'][:40]:<40} "
              f"{t['codec'] or '?':<10} {'LOSSY' if t['is_lossy'] else 'lossless' if t['is_lossy'] == 0 else '?':<8} "
              f"{t['text_match_status']:<18} {'analyzed' if t['has_analysis'] else '-':<8} "
              f"{('v%03d %s %s' % (lv['version_no'], lv['mode'], lv['status'])) if lv else ''}")


def cmd_analyze(e, a):
    ids = _track_ids(e, a)
    jid = e.create_job("analyze", ids, ["analyze"], {"force": a.force}, idempotency_key=a.idempotency_key)
    st = _run_job(e, jid, a)
    _out(st)
    return 0 if st["status"] == "COMPLETED" else 2


def cmd_report(e, a):
    _out(e.analysis_summary(int(a.track_id)) if not a.full else e.get_analysis(int(a.track_id)))


def cmd_convert(e, a):
    ids = _track_ids(e, a)
    jid = e.create_job("convert", ids, ["analyze", "convert"],
                       {"target_bits": a.bits, "overs_policy": a.overs_policy})
    st = _run_job(e, jid, a)
    _out(st)
    return 0 if st["status"] == "COMPLETED" else 2


def cmd_job(e, a):
    if a.action == "status":
        _out(e.job_status(int(a.job_id)))
    elif a.action == "resume":
        st = _run_job(e, int(a.job_id), a)
        _out(st)
    elif a.action == "cancel":
        _out(e.cancel_job(int(a.job_id)))
    elif a.action == "list":
        _out([dict(r) for r in e.db.query("SELECT id, kind, status, created_at, updated_at FROM jobs ORDER BY id")])


def cmd_versions(e, a):
    _out(e.list_versions(int(a.track_id)))


def cmd_approve(e, a):
    _out(e.approve(int(a.track_id), int(a.version), decision=a.decision, actor=a.actor, note=a.note))


def cmd_verify_sources(e, a):
    bad = []
    for t in e.list_tracks():
        if Path(t["path"]).exists() and not e.verify_source_unchanged(t["id"]):
            bad.append(t["path"])
    _out({"checked": len(e.list_tracks()), "changed": bad, "status": "PASS" if not bad else "FAIL"})
    return 0 if not bad else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="srm", description="Suno Release Master (offline, local)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--project", required=True, help="project directory (created by scan)")
        sp.set_defaults(fn=fn)
        return sp

    add("health", cmd_health, "versions, FFmpeg and capability report")
    sp = add("scan", cmd_scan, "register a library root and scan it (read-only)")
    sp.add_argument("library")
    sp.add_argument("--verify-decode", action="store_true", help="fully decode every file during scan")
    sp.add_argument("--exclude", nargs="*", help='folder-name globs to skip, e.g. "_*" (stored with the root)')
    add("albums", cmd_albums, "list album candidates")
    sp = add("tracks", cmd_tracks, "list tracks")
    sp.add_argument("--album")
    sp.add_argument("--json", action="store_true")
    for name, fn, hlp in (("analyze", cmd_analyze, "analyze tracks (restartable job)"),
                          ("convert", cmd_convert, "CONVERT_ONLY render to PCM WAV (restartable job)")):
        sp = add(name, fn, hlp)
        sp.add_argument("--album")
        sp.add_argument("--track-id", nargs="*")
        sp.add_argument("--workers", type=int)
        sp.add_argument("--stop-after", type=int, help=argparse.SUPPRESS)
        if name == "analyze":
            sp.add_argument("--force", action="store_true")
            sp.add_argument("--idempotency-key")
        else:
            sp.add_argument("--bits", type=int, choices=(16, 24), default=24)
            sp.add_argument("--overs-policy", choices=("trim", "clip", "fail"), default="trim")
    sp = add("report", cmd_report, "show analysis of a track")
    sp.add_argument("track_id")
    sp.add_argument("--full", action="store_true")
    sp = add("job", cmd_job, "job status/resume/cancel/list")
    sp.add_argument("action", choices=("status", "resume", "cancel", "list"))
    sp.add_argument("job_id", nargs="?")
    sp.add_argument("--workers", type=int)
    sp.add_argument("--retry-failed", action="store_true")
    sp = add("versions", cmd_versions, "list processing versions of a track")
    sp.add_argument("track_id")
    sp = add("approve", cmd_approve, "record an explicit approval / rejection / listening sign-off")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--version", required=True)
    sp.add_argument("--decision", choices=("APPROVED", "REJECTED", "LISTENING_SIGNOFF"), default="APPROVED")
    sp.add_argument("--actor", default="local-user")
    sp.add_argument("--note", default="")
    add("verify-sources", cmd_verify_sources, "re-hash all sources and compare with scan-time SHA-256")
    try:
        from srm.cli import extra

        extra.register(sub, add)
    except ImportError:
        pass
    return p


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        e = _engine(args)
        rc = args.fn(e, args)
        return int(rc or 0)
    except Exception as exc:  # user-facing error, non-zero exit
        from srm.core.tools import ToolMissingError

        print(f"[srm] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4 if isinstance(exc, ToolMissingError) else 1


if __name__ == "__main__":
    sys.exit(main())
