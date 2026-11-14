"""Additional CLI commands (M2-M6): propose/render/preview, repairs, albums, metadata, export, validate."""

from __future__ import annotations

import json


def _out(o):
    print(json.dumps(o, ensure_ascii=False, indent=1, default=str))


def _kv(pairs):
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if v.lower() in ("true", "false"):
            v = v.lower() == "true"
        out[k] = v
    return out


def register(sub, add):
    from srm.cli.main import _run_job, _track_ids

    def propose(e, a):
        res = []
        for tid in _track_ids(e, a):
            p = e.suggest_mastering(tid, mode=a.mode.upper(), preset=a.preset, ceiling_dbtp=a.ceiling,
                                    loudness_policy=a.loudness_policy, target_lufs=a.target_lufs)
            res.append({"track_id": tid, "version_no": p["version_no"], "preset": p["preset"],
                        "actions": [{k: x.get(k) for k in ("stage", "problem", "remedy", "risk", "enabled")}
                                    for x in p["actions"]]})
        _out(res)

    sp = add("propose", propose, "create PROPOSED mastering versions (nothing rendered/approved)")
    sp.add_argument("--album")
    sp.add_argument("--track-id", nargs="*")
    sp.add_argument("--mode", default="safe_master", choices=("safe_master", "restore_and_master"))
    sp.add_argument("--preset")
    sp.add_argument("--ceiling", type=float, default=-1.0)
    sp.add_argument("--loudness-policy", default="preset_window", choices=("preset_window", "preserve", "target"))
    sp.add_argument("--target-lufs", type=float)

    def render(e, a):
        jid = e.create_job("render", _track_ids(e, a), ["render"],
                           {"approved_only": a.proposed_only, "codec_preview": True})
        st = _run_job(e, jid, a)
        _out(st)
        return 0 if st["status"] == "COMPLETED" else 2

    sp = add("render", render, "render latest PROPOSED version per track (restartable job)")
    sp.add_argument("--album")
    sp.add_argument("--track-id", nargs="*")
    sp.add_argument("--workers", type=int)
    sp.add_argument("--proposed-only", action="store_true", help="fail tracks that have no PROPOSED version")

    def preview(e, a):
        _out(e.create_preview(int(a.track_id), a.version, start_s=a.start, duration_s=a.duration))

    sp = add("preview", preview, "write loudness-matched A-B audition clips")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--version", type=int)
    sp.add_argument("--start", type=float)
    sp.add_argument("--duration", type=float, default=30.0)

    def signoff(e, a):
        _out(e.listening_signoff(int(a.track_id), int(a.version), note=a.note, actor=a.actor))

    sp = add("signoff", signoff, "record that a human listened to this version")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--version", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--actor", default="local-user")

    def repairs(e, a):
        if a.action == "propose":
            _out(e.propose_repairs(int(a.track_id)))
        elif a.action == "preview":
            _out(e.preview_repair(int(a.track_id), int(a.plan), int(a.edit)))
        else:
            _out(e.decide_repairs(int(a.track_id), int(a.plan), approve=[int(x) for x in a.approve or []],
                                  reject=[int(x) for x in a.reject or []], accept_auto=a.accept_auto))

    sp = add("repairs", repairs, "repair plans: propose / preview / decide")
    sp.add_argument("action", choices=("propose", "preview", "decide"))
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--plan")
    sp.add_argument("--edit")
    sp.add_argument("--approve", nargs="*")
    sp.add_argument("--reject", nargs="*")
    sp.add_argument("--accept-auto", action="store_true")

    def album(e, a):
        if a.action == "create":
            _out(e.create_album(a.name))
        elif a.action == "assign":
            _out(e.assign_track(int(a.track_id), a.name, a.position))
        elif a.action == "reorder":
            _out([{"id": t["id"], "track_no": t["track_no"], "title": t["title"]}
                  for t in e.reorder_album(a.name, [int(x) for x in a.order])])
        elif a.action == "meta":
            _out(e.set_album_metadata(a.name, _kv(a.set)))
        else:
            _out(e.album_cohesion(a.name))

    sp = add("album", album, "album create/assign/reorder/meta/cohesion (sources are never moved)")
    sp.add_argument("action", choices=("create", "assign", "reorder", "meta", "cohesion"))
    sp.add_argument("--name", required=True)
    sp.add_argument("--track-id")
    sp.add_argument("--position", type=int)
    sp.add_argument("--order", nargs="*")
    sp.add_argument("--set", nargs="*", help="field=value")

    def track_meta(e, a):
        if a.set:
            _out(e.set_track_metadata(int(a.track_id), _kv(a.set)))
        if a.style:
            e.set_style_override(int(a.track_id), None if a.style == "none" else a.style)
            _out({"track_id": int(a.track_id), "style_override": a.style})

    sp = add("track-meta", track_meta, "set release metadata / AI disclosure / rights fields; style override")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--set", nargs="*", default=[])
    sp.add_argument("--style")

    def text_match(e, a):
        t = e.decide_text_match(int(a.track_id), accept=not a.reject, text_path=a.text)
        _out({"track_id": t["id"], "text_match_status": t["text_match_status"], "text": t["text"]})

    sp = add("text-match", text_match, "confirm or reject the prompt/lyrics association")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--text")
    sp.add_argument("--reject", action="store_true")

    def export(e, a):
        r = e.export_album(a.album, profile=a.profile, include_mp3=not a.no_mp3, include_flac=a.flac,
                           allow_draft=a.allow_draft, confirm=a.confirm, distributor=a.distributor)
        r.pop("validation", None)
        _out(r)
        return 0 if r["technical"] == "PASS" else 2

    sp = add("export", export, "export an album package (requires --confirm)")
    sp.add_argument("--album", required=True)
    sp.add_argument("--profile", default="release_wav", choices=("release_wav", "compat_wav"))
    sp.add_argument("--no-mp3", action="store_true")
    sp.add_argument("--flac", action="store_true")
    sp.add_argument("--allow-draft", action="store_true", help="export unapproved renders as DRAFT_NOT_RELEASE_READY")
    sp.add_argument("--distributor", default="generic")
    sp.add_argument("--confirm", action="store_true")

    def validate(e, a):
        v = e.validate_release(a.album, a.version)
        _out(v if a.full else {k: v[k] for k in ("package", "overall", "technical", "human_gates", "unresolved_count")})
        return 0 if v["technical"] == "PASS" else 2

    sp = add("validate", validate, "independently validate an exported package from its bytes")
    sp.add_argument("--album", required=True)
    sp.add_argument("--version", type=int)
    sp.add_argument("--full", action="store_true")

    def profiles(e, a):
        from srm.release.profiles import load_profiles

        _out(load_profiles(e.root))

    add("profiles", profiles, "show export profiles and distributor rules (with verification status)")

    def stems(e, a):
        _out(e.import_stems(int(a.track_id), a.folder))

    sp = add("stems", stems, "register and validate original stems (folder inside a library root)")
    sp.add_argument("--track-id", required=True)
    sp.add_argument("--folder", required=True)

    def gui(e, a):
        from srm.gui.app import main as gmain

        return gmain([str(e.root)])

    add("gui", gui, "open the desktop GUI on this project")

    def serve_mcp(e, a):
        from srm.mcp_server.server import serve

        return serve(e.root, host=a.host, port=a.port)

    sp = add("serve-mcp", serve_mcp, "run the optional authenticated MCP server for ChatGPT")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
