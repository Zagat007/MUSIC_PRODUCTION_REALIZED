"""Optional MCP bridge for ChatGPT (thin orchestration over the local Engine - no DSP here).

Security model
--------------
* Binds to 127.0.0.1 by default. Reaching it from ChatGPT needs an HTTPS tunnel/gateway the
  user controls (see docs/OPENAI_INTEGRATION.md). Nothing here opens a public port by itself.
* Every request to /mcp must authenticate: ``Authorization: Bearer <token>`` or the secret
  capability path ``/s/<token>/mcp`` (for clients that cannot send headers; weaker - the token is
  part of the URL). Tokens are compared in constant time. Per-client rate limiting.
* Tools never accept filesystem paths. Library roots must be authorized locally (CLI/GUI);
  `scan_library` takes a root *id*. Downloads are short-lived HMAC-signed URLs scoped to one file
  inside the project directory.
* Approvals and exports require an explicit confirmation phrase that the user must state.
* No shell, no deletion, no arbitrary URLs, no uploads of source audio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from srm import __version__
from srm.core.paths import PathSafetyError, ensure_within

SECRET_FILE = "mcp_secret.json"
ARTIFACT_TTL_S = 600
RATE_PER_MIN = 120


def load_or_create_secret(project_root: Path) -> dict:
    p = Path(project_root) / SECRET_FILE
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    data = {"token": secrets.token_urlsafe(32), "signing_key": secrets.token_hex(32), "created_at": time.time()}
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    _restrict_to_current_user(p)
    return data


def _restrict_to_current_user(path: Path) -> bool:
    """Best effort on Windows, where the POSIX mode bits above are ignored: drop inherited permissions and
    leave access to the current user only. Returns False (and changes nothing) when that is not possible."""
    user = os.environ.get("USERNAME")
    if os.name != "nt" or not user:
        return False
    import subprocess

    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class RedactTokenFilter:
    """Logging filter: the capability path /s/<token>/mcp must never reach the access log or console."""

    def __init__(self, token: str):
        self.token = token

    def _clean(self, value):
        return value.replace(self.token, "<redacted>") if isinstance(value, str) else value

    def filter(self, record) -> bool:
        record.msg = self._clean(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean(v) for k, v in record.args.items()}
        return True


def install_token_redaction(token: str) -> RedactTokenFilter:
    import logging

    flt = RedactTokenFilter(token)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "mcp"):
        logging.getLogger(name).addFilter(flt)
    return flt


class ArtifactSigner:
    def __init__(self, key_hex: str, root: Path):
        self.key = bytes.fromhex(key_hex)
        self.root = Path(root).resolve()

    def sign(self, path: Path, ttl: int = ARTIFACT_TTL_S) -> str:
        rel = str(ensure_within(path, self.root).relative_to(self.root)).replace("\\", "/")
        exp = int(time.time()) + ttl
        payload = base64.urlsafe_b64encode(json.dumps({"p": rel, "e": exp}).encode()).decode().rstrip("=")
        sig = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def verify(self, token: str) -> Path:
        try:
            payload, sig = token.rsplit(".", 1)
        except ValueError as exc:
            raise PermissionError("malformed artifact token") from exc
        good = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            raise PermissionError("bad signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if time.time() > data["e"]:
            raise PermissionError("artifact link expired")
        p = ensure_within(self.root / data["p"], self.root)
        if p.name == SECRET_FILE or p.suffix.lower() in (".sqlite", ".sqlite-wal", ".sqlite-shm"):
            raise PermissionError("not downloadable")
        return p


class AuthMiddleware:
    """ASGI middleware: bearer/capability-path auth, rate limit, audit hook. Fails closed."""

    def __init__(self, app, token: str, on_event=None, open_paths=("/health",), artifact_prefix="/artifact/"):
        self.app = app
        self.token = token
        self.on_event = on_event or (lambda *a: None)
        self.open_paths = set(open_paths)
        self.artifact_prefix = artifact_prefix
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _rate_ok(self, client: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[client]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
            q.append(now)
            return True

    async def _deny(self, send, status: int, msg: str):
        body = json.dumps({"error": msg}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        client = (scope.get("client") or ("?", 0))[0]
        if not self._rate_ok(client):
            self.on_event("rate_limited", {"client": client, "path": path})
            return await self._deny(send, 429, "rate limit exceeded")
        if path in self.open_paths or path.startswith(self.artifact_prefix):
            return await self.app(scope, receive, send)  # artifact URLs carry their own HMAC
        prefix = "/s/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            tok, _, tail = rest.partition("/")
            if hmac.compare_digest(tok.encode(), self.token.encode()):
                scope = dict(scope)
                scope["path"] = "/" + tail
                scope["raw_path"] = scope["path"].encode()
                return await self.app(scope, receive, send)
            self.on_event("auth_failed", {"client": client, "mode": "path"})
            return await self._deny(send, 401, "unauthorized")
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), self.token.encode()):
            return await self.app(scope, receive, send)
        self.on_event("auth_failed", {"client": client, "mode": "header", "path": path})
        return await self._deny(send, 401, "unauthorized")


def build_server(project_root: str | Path, *, public_base_url: str = "http://127.0.0.1:8787"):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse

    from srm.service.engine import Engine

    eng = Engine(project_root, create=False)
    secret = load_or_create_secret(eng.root)
    signer = ArtifactSigner(secret["signing_key"], eng.root)
    jobs_pool = ThreadPoolExecutor(max_workers=1)
    base = public_base_url.rstrip("/")

    srv = MCPServer(
        name="suno-release-master", version=__version__,
        instructions=("Local audio QA / mastering / release tool. All DSP runs on the user's PC. Workflow: "
                      "list -> analyze -> get_analysis_report -> suggest_mastering -> create_preview -> (user listens) "
                      "-> approve_processing_version (needs the user's explicit confirmation phrase) -> "
                      "start_batch_render -> validate_release -> export_album (confirmation phrase). Never claim "
                      "a master sounds professional from numbers; never claim AAC->WAV restores quality."),
    )

    def audit(action: str, details: dict):
        eng.db.audit(f"mcp:{action}", details, actor="mcp")

    def guard(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            audit(fn.__name__, {k: v for k, v in kw.items() if k not in ("confirmation",)})
            try:
                return fn(*a, **kw)
            except ToolError:
                raise
            except (PathSafetyError, PermissionError) as exc:
                raise ToolError(f"FORBIDDEN: {exc}") from exc
            except Exception as exc:
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        return wrapper

    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    wr = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
    read_only = {"health_and_capabilities", "list_projects", "list_albums", "list_tracks", "get_analysis_report",
                 "get_job_status", "validate_release", "get_release_manifest", "get_artifact_download", "get_album_artwork"}

    def tool(description: str):
        def deco(fn):
            return srv.tool(description=description, annotations=ro if fn.__name__ in read_only else wr)(fn)
        return deco

    def link(path: str | Path) -> dict:
        return {"url": f"{base}/artifact/{signer.sign(Path(path))}", "expires_in_s": ARTIFACT_TTL_S,
                "file": Path(path).name}

    @tool(description="Processor version, FFmpeg status, available/unavailable engines. Read-only.")
    @guard
    def health_and_capabilities() -> dict:
        return eng.health()

    @tool(description="Projects served by this MCP instance (one per server process). Read-only.")
    @guard
    def list_projects() -> list[dict]:
        return [{"name": eng.root.name, "library_roots": [{"id": r["id"], "name": Path(r["path"]).name}
                                                          for r in eng.library_roots()]}]

    @tool(description="List album candidates with track counts. Read-only.")
    @guard
    def list_albums() -> list[dict]:
        return [{k: a[k] for k in ("id", "name", "tracks")} for a in eng.list_albums()]

    def artwork_response(info):
        cover = info['cover']
        return {'album_id': info['album_id'], 'description': info['description'],
                'cover': ({'id': cover['id'], 'method': cover['method'], 'sha256': cover['sha256'],
                           'download': link(cover['path'])} if cover else None),
                'versions': len(info['history']), 'previews': info.get('previews')}

    @tool(description="Album description and selected cover with a signed image link. Read-only.")
    @guard
    def get_album_artwork(album_id: int) -> dict:
        return artwork_response(eng.album_artwork(album_id))

    @tool(description="Save the user's album description. Does not create or approve artwork.")
    @guard
    def set_album_description(album_id: int, description: str) -> dict:
        return artwork_response(eng.set_album_description(album_id, description))

    @tool(description="Create a new LOCAL ABSTRACT cover variant guided by the album description. "
                      "No neural image model or paid API. Updates derived MP3 previews; source audio and old exports stay intact.")
    @guard
    def generate_album_cover(album_id: int) -> dict:
        return artwork_response(eng.generate_album_cover(album_id))

    @tool(description="List tracks (optionally of one album id) with codec, lossy flag, analysis/version state.")
    @guard
    def list_tracks(album_id: int | None = None) -> list[dict]:
        keys = ("id", "track_no", "title", "album_name", "codec", "is_lossy", "duration_s", "text_match_status",
                "has_analysis", "latest_version", "dup_group")
        return [{k: t.get(k) for k in keys} for t in eng.list_tracks(album_id)]

    @tool(description="Rescan a library root that the user already authorized locally (by root id; "
                          "absolute paths are not accepted). Sources are only read.")
    @guard
    def scan_library(root_id: int) -> dict:
        ids = {r["id"] for r in eng.library_roots()}
        if int(root_id) not in ids:
            raise PermissionError("root id is not authorized in this project")
        s = eng.scan(int(root_id))
        s.pop("root", None)
        return s

    @tool(description="Analyze one track now (blocking, ~seconds per minute of audio).")
    @guard
    def analyze_track(track_id: int) -> dict:
        eng.analyze_track(int(track_id))
        return eng.analysis_summary(int(track_id))

    @tool(description="Start a background analysis job for an album. Returns job_id; poll get_job_status.")
    @guard
    def analyze_album(album_id: int, idempotency_key: str | None = None) -> dict:
        ids = [t["id"] for t in eng.list_tracks(int(album_id))]
        jid = eng.create_job("analyze", ids, ["analyze"], {}, idempotency_key=idempotency_key)
        jobs_pool.submit(eng.run_job, jid)
        return {"job_id": jid, "tracks": len(ids)}

    @tool(description="Measurements, defect candidates (with timestamps/confidence), style hypotheses and "
                          "unknowns for a track. detail='summary'|'full'. Includes a signed link to the HTML report.")
    @guard
    def get_analysis_report(track_id: int, detail: str = "summary") -> dict:
        s = eng.analysis_summary(int(track_id))
        if detail == "full":
            a = eng.get_analysis(int(track_id))
            if a:
                m = dict(a["measurements"])
                m.pop("timelines", None)
                s["full_measurements"] = m
        t = eng.track(int(track_id))
        rep = eng._report_dir(t) / "source_analysis.html"
        if rep.exists():
            s["report_html"] = link(rep)
        return s

    @tool(description="Return an explainable mastering PROPOSAL (problem/evidence/remedy/risk/settings) and store "
                          "it as a PROPOSED version. Applies nothing. loudness_policy: preset_window|preserve|target.")
    @guard
    def suggest_mastering(track_id: int, mode: str = "SAFE_MASTER", preset: str | None = None,
                          loudness_policy: str = "preset_window", target_lufs: float | None = None,
                          ceiling_dbtp: float = -1.0) -> dict:
        p = eng.suggest_mastering(int(track_id), mode=mode, preset=preset, loudness_policy=loudness_policy,
                                  target_lufs=target_lufs, ceiling_dbtp=ceiling_dbtp)
        return {k: p[k] for k in ("version_no", "mode", "preset", "preset_reason", "actions", "chain", "disclaimer")}

    @tool(description="Render a PROPOSED version (blocking) and return measured results, or create loudness-matched "
                          "A-B preview clips (with signed download links) for listening.")
    @guard
    def create_preview(track_id: int, version_no: int | None = None, render_if_needed: bool = True,
                       start_s: float | None = None, duration_s: float = 30.0) -> dict:
        out: dict[str, Any] = {}
        if version_no is not None:
            v = eng.version(int(track_id), int(version_no))
            if v["status"] == "PROPOSED" and render_if_needed:
                r = eng.render_version(int(track_id), int(version_no))
                out["render"] = {"policy_check": r["policy_check"], "comparison": r["comparison"],
                                 "codec_preview": r.get("codec_preview")}
        pv = eng.create_preview(int(track_id), version_no, start_s=start_s, duration_s=min(60.0, duration_s))
        out["preview"] = {k: pv[k] for k in ("start_s", "duration_s", "reference_lufs", "note")}
        out["clips"] = {k: {**{kk: c[kk] for kk in ("measured_lufs", "matching_gain_db")}, "download": link(c["path"])}
                        for k, c in pv["clips"].items()}
        out["listening_required"] = True
        return out

    @tool(description="Record the USER's decision on a rendered version. Requires the user to have said the exact "
                          "phrase 'APPROVE <track_id> v<version_no>' (or REJECT/SIGNOFF); pass it as confirmation.")
    @guard
    def approve_processing_version(track_id: int, version_no: int, decision: str, confirmation: str) -> dict:
        verb = {"APPROVED": "APPROVE", "REJECTED": "REJECT", "LISTENING_SIGNOFF": "SIGNOFF"}.get(decision)
        if not verb:
            raise ToolError("decision must be APPROVED, REJECTED or LISTENING_SIGNOFF")
        expected = f"{verb} {int(track_id)} v{int(version_no)}"
        if confirmation.strip() != expected:
            raise ToolError(f"CONFIRMATION_REQUIRED: ask the user to state exactly '{expected}'")
        v = eng.approve(int(track_id), int(version_no), decision=decision, actor="user-via-chatgpt")
        return {"version_no": v["version_no"], "status": v["status"], "approvals": len(v["approvals"])}

    @tool(description="Start a background render job for an album (latest PROPOSED version per track; tracks "
                          "without one get a SAFE_MASTER proposal unless proposed_only). Returns job_id.")
    @guard
    def start_batch_render(album_id: int, proposed_only: bool = True, idempotency_key: str | None = None) -> dict:
        ids = [t["id"] for t in eng.list_tracks(int(album_id))]
        jid = eng.create_job("render", ids, ["render"], {"approved_only": proposed_only},
                             idempotency_key=idempotency_key)
        jobs_pool.submit(eng.run_job, jid)
        return {"job_id": jid, "tracks": len(ids)}

    @tool(description="Job progress with per-track failures. Read-only.")
    @guard
    def get_job_status(job_id: int) -> dict:
        return eng.job_status(int(job_id))

    @tool(description="Request cancellation of a job (finishes current items; resumable).")
    @guard
    def cancel_job(job_id: int) -> dict:
        return eng.cancel_job(int(job_id))

    @tool(description="Independently validate the latest export of an album from its bytes.")
    @guard
    def validate_release(album_id: int) -> dict:
        v = eng.validate_release(int(album_id))
        return {k: v[k] for k in ("overall", "technical", "human_gates", "unresolved_count", "global_checks", "note")} | {
            "tracks": [{"file": t["file"], "open_checks": {k: c["status"] for k, c in t["checks"].items()
                                                           if c["status"] not in ("PASS", "INFO")}} for t in v["tracks"]]}

    @tool(description="Export an album package locally (never published). Requires the user's explicit phrase "
                          "'EXPORT <album_id>' as confirmation. profile: release_wav|compat_wav.")
    @guard
    def export_album(album_id: int, profile: str, confirmation: str, include_mp3: bool = True,
                     include_flac: bool = False, allow_draft: bool = False, distributor: str = "generic") -> dict:
        if confirmation.strip() != f"EXPORT {int(album_id)}":
            raise ToolError(f"CONFIRMATION_REQUIRED: ask the user to state exactly 'EXPORT {int(album_id)}'")
        r = eng.export_album(int(album_id), profile=profile, include_mp3=include_mp3, include_flac=include_flac,
                             allow_draft=allow_draft, confirm=True, distributor=distributor)
        return {"status": r["status"], "technical": r["technical"], "human_gates": r["human_gates"],
                "unresolved": r["unresolved"][:50], "package_name": Path(r["path"]).parent.name + "/" + Path(r["path"]).name}

    @tool(description="Metadata/processing manifest of the latest export, with signed links to QA files.")
    @guard
    def get_release_manifest(album_id: int) -> dict:
        v = eng.validate_release(int(album_id))
        pkg = Path(v["package"])
        man = json.loads((pkg / "processing_manifest.json").read_text("utf-8"))
        return {"profile": man["profile"], "unresolved": man["unresolved"],
                "tracks": [{k: t[k] for k in ("track_number", "file", "wav_sha256", "version_no", "version_mode",
                                              "version_status", "listening_signoff")} for t in man["tracks"]],
                "files": {n: link(pkg / n) for n in ("album_metadata.json", "album_metadata.csv", "album_qa.html",
                                                     "release_validation.json", "rights_ai_disclosure_checklist.json")
                          if (pkg / n).exists()}}

    @tool(description="Short-lived signed download link for an export file (kind: wav|mp3|flac|qa_html|qa_json) "
                          "by album id and track number. Explicit user consent is assumed by calling this tool.")
    @guard
    def get_artifact_download(album_id: int, kind: str, track_number: int) -> dict:
        v = eng.validate_release(int(album_id))
        pkg = Path(v["package"])
        sub, ext = {"wav": ("WAV_MASTER", ".wav"), "mp3": ("MP3_PREVIEW", ".mp3"), "flac": ("FLAC_ARCHIVE", ".flac"),
                    "qa_html": ("QA", ".html"), "qa_json": ("QA", ".json")}.get(kind, (None, None))
        if not sub:
            raise ToolError("unknown kind")
        hits = sorted((pkg / sub).glob(f"{int(track_number):02d}_*{ext}"))
        if not hits:
            raise ToolError("file not found in latest export")
        return link(hits[0])

    @srv.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok", "app": "suno-release-master", "version": __version__})

    @srv.custom_route("/artifact/{token}", methods=["GET"])
    async def artifact(request: Request):
        try:
            p = signer.verify(request.path_params["token"])
        except (PermissionError, PathSafetyError, ValueError) as exc:
            audit("artifact_denied", {"reason": str(exc)})
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not p.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        audit("artifact_download", {"file": p.name})
        return FileResponse(str(p), filename=p.name)

    return srv, eng, secret


def build_app(project_root: str | Path, *, host: str = "127.0.0.1", port: int = 8787,
              public_base_url: str | None = None, allowed_hosts: list[str] | None = None):
    from mcp.server.transport_security import TransportSecuritySettings

    srv, eng, secret = build_server(project_root, public_base_url=public_base_url or f"http://{host}:{port}")
    hosts = [f"{host}:*", "127.0.0.1:*", "localhost:*"] + (allowed_hosts or [])
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                    allowed_origins=[f"http://{h}" for h in hosts] + ["https://chatgpt.com",
                                                                                      "https://chat.openai.com"])
    app = srv.streamable_http_app(transport_security=sec, host=host, stateless_http=True, json_response=True)
    wrapped = AuthMiddleware(app, secret["token"], on_event=lambda a, d: eng.db.audit(f"mcp:{a}", d, actor="mcp"))
    return wrapped, app, secret, eng


def serve(project_root: str | Path, host: str = "127.0.0.1", port: int = 8787) -> int:
    import uvicorn

    public = os.environ.get("SRM_PUBLIC_BASE_URL")
    extra_hosts = [h for h in os.environ.get("SRM_ALLOWED_HOSTS", "").split(",") if h]
    wrapped, app, secret, eng = build_app(project_root, host=host, port=port, public_base_url=public,
                                          allowed_hosts=extra_hosts)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("[srm-mcp] WARNING: binding to a non-loopback interface. Use only behind your own HTTPS gateway.")
    print(f"[srm-mcp] project: {eng.root}")
    print(f"[srm-mcp] endpoint: http://{host}:{port}/mcp  (Authorization: Bearer <token>)")
    print(f"[srm-mcp] token stored in {Path(eng.root) / SECRET_FILE} (keep private; delete the file to rotate)")

    # Filters on the named loggers survive uvicorn's own logging setup (it replaces handlers, not filters).
    install_token_redaction(secret["token"])
    uvicorn.run(wrapped, host=host, port=port, log_level="info", lifespan="on")
    return 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="srm-mcp")
    ap.add_argument("--project", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    return serve(a.project, a.host, a.port)
