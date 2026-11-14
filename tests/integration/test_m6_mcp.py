"""MCP end-to-end: a real HTTP server + real MCP client hitting the same Engine (no mocks)."""

import asyncio
import json
import socket
import threading
import time

import pytest

pytest.importorskip("mcp")
import httpx2  # noqa: E402
import uvicorn  # noqa: E402


def _local_get(url, **kwargs):
    # Local acceptance must bypass system proxies (some Windows setups proxy loopback -> HTTP 503).
    return httpx2.get(url, trust_env=False, **kwargs)


def _local_post(url, **kwargs):
    return httpx2.post(url, trust_env=False, **kwargs)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory, fixture_lib):
    from srm.mcp_server.server import build_app
    from srm.service.engine import Engine

    proj = tmp_path_factory.mktemp("mcp") / "proj"
    e = Engine(proj)
    e.add_library_root(fixture_lib / "Альбом Б")
    e.scan()
    port = _free_port()
    wrapped, app, secret, eng = build_app(proj, port=port)
    cfg = uvicorn.Config(wrapped, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            if _local_get(f"http://127.0.0.1:{port}/health").status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{port}", "token": secret["token"], "engine": eng}
    srv.should_exit = True
    th.join(timeout=10)


async def _call(url, token, calls, path="/mcp"):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=600, trust_env=False) as hc:
        async with streamable_http_client(url + path, http_client=hc) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as s:
                await s.initialize()
                out = []
                for name, args in calls:
                    if name == "__list__":
                        out.append([t.name for t in (await s.list_tools()).tools])
                        continue
                    r = await s.call_tool(name, args)
                    txt = r.content[0].text if r.content else ""
                    out.append({"is_error": bool(r.is_error), "data": r.structured_content, "text": txt})
                return out


def run(coro):
    return asyncio.run(coro)


def test_unauthorized_fails_closed(server):
    assert _local_post(server["url"] + "/mcp", json={}).status_code == 401
    assert _local_post(server["url"] + "/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert _local_post(server["url"] + "/s/wrong/mcp", json={}).status_code == 401
    assert _local_get(server["url"] + "/artifact/abc.def").status_code == 403


def test_tools_reach_real_engine(server):
    e = server["engine"]
    tid = [t["id"] for t in e.list_tracks() if "Песня" in t["rel_path"]][0]
    res = run(_call(server["url"], server["token"], [
        ("__list__", {}),
        ("health_and_capabilities", {}),
        ("list_tracks", {}),
        ("analyze_track", {"track_id": tid}),
        ("suggest_mastering", {"track_id": tid}),
    ]))
    names = res[0]
    for n in ("health_and_capabilities", "list_projects", "list_albums", "list_tracks", "scan_library", "analyze_track",
              "analyze_album", "get_analysis_report", "suggest_mastering", "create_preview",
              "approve_processing_version", "start_batch_render", "get_job_status", "cancel_job", "validate_release",
              "export_album", "get_release_manifest", "get_artifact_download"):
        assert n in names
    assert not any(n in names for n in ("shell", "exec", "delete", "read_file"))
    assert not res[3]["is_error"], res[3]["text"]
    # The analysis really happened in the shared backend
    assert e.get_analysis(tid) is not None
    vno = json.loads(res[4]["text"])["version_no"] if res[4]["data"] is None else res[4]["data"]["version_no"]
    assert e.version(tid, vno)["status"] == "PROPOSED"
    # preview (renders), approval guard, approval, artifact download
    res2 = run(_call(server["url"], server["token"], [
        ("create_preview", {"track_id": tid, "version_no": vno, "duration_s": 5}),
        ("approve_processing_version", {"track_id": tid, "version_no": vno, "decision": "APPROVED",
                                        "confirmation": "yes"}),
        ("approve_processing_version", {"track_id": tid, "version_no": vno, "decision": "APPROVED",
                                        "confirmation": f"APPROVE {tid} v{vno}"}),
    ]))
    assert not res2[0]["is_error"], res2[0]["text"]
    prev = res2[0]["data"] or json.loads(res2[0]["text"])
    url = next(iter(prev["clips"].values()))["download"]["url"]
    r = _local_get(url)
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert res2[1]["is_error"] and "CONFIRMATION_REQUIRED" in res2[1]["text"]
    assert not res2[2]["is_error"] and e.version(tid, vno)["status"] == "APPROVED"
    # tampered artifact token is rejected
    bad = url[:-1] + ("0" if url[-1] != "0" else "1")
    assert _local_get(bad).status_code == 403


def test_capability_path_and_forbidden_root(server):
    res = run(_call(server["url"], None, [("scan_library", {"root_id": 999}), ("list_projects", {})],
                    path=f"/s/{server['token']}/mcp"))
    assert res[0]["is_error"] and "FORBIDDEN" in res[0]["text"]
    assert not res[1]["is_error"]
    audit = [r["action"] for r in server["engine"].db.query("SELECT action FROM audit_log")]
    assert "mcp:auth_failed" in audit and "mcp:scan_library" in audit


def test_album_artwork_tools(server):
    aid = server['engine'].list_albums()[0]['id']
    results = run(_call(server['url'], server['token'], [
        ('set_album_description', {'album_id': aid, 'description': 'Ночной город и свет отеля'}),
        ('generate_album_cover', {'album_id': aid}),
        ('get_album_artwork', {'album_id': aid}),
    ]))
    assert all(not result['is_error'] for result in results)
    cover = results[-1]['data'] or json.loads(results[-1]['text'])
    response = _local_get(cover['cover']['download']['url'])
    assert response.status_code == 200 and response.content[:2] == b'\xff\xd8'


def test_export_requires_phrase_and_job_flow(server):
    e = server["engine"]
    aid = e.list_albums()[0]["id"]
    res = run(_call(server["url"], server["token"], [
        ("start_batch_render", {"album_id": aid, "proposed_only": False}),
    ]))
    jid = (res[0]["data"] or json.loads(res[0]["text"]))["job_id"]
    for _ in range(600):
        st = e.job_status(jid)
        if st["status"] not in ("PENDING", "RUNNING"):
            break
        time.sleep(0.5)
    assert st["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS")
    res = run(_call(server["url"], server["token"], [
        ("export_album", {"album_id": aid, "profile": "release_wav", "confirmation": "ok", "allow_draft": True}),
        ("export_album", {"album_id": aid, "profile": "release_wav", "confirmation": f"EXPORT {aid}",
                          "allow_draft": True}),
        ("validate_release", {"album_id": aid}),
        ("get_artifact_download", {"album_id": aid, "kind": "wav", "track_number": 1}),
    ]))
    assert res[0]["is_error"] and "CONFIRMATION_REQUIRED" in res[0]["text"]
    assert not res[1]["is_error"], res[1]["text"]
    out = res[1]["data"] or json.loads(res[1]["text"])
    assert out["status"] in ("DRAFT_NOT_RELEASE_READY", "FAILED") and out["technical"] == "PASS"
    dl = res[3]["data"] or json.loads(res[3]["text"])
    r = _local_get(dl["url"])
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
