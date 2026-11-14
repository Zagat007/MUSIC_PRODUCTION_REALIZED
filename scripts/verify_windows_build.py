"""Acceptance of the rebuilt executables, using generated audio only."""
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import httpx2
import numpy as np
import soundfile as sf
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(tempfile.mkdtemp(prefix='build_acceptance_', dir=ROOT.parent / '_work'))
EXE = ROOT / 'dist/srm/srm.exe'
PROJ = OUT / 'project'
checks = {}

def check(name, condition):
    checks[name] = 'PASS' if condition else 'FAIL'
    print(name, checks[name], flush=True)
    assert condition, name

def cli(*args):
    r = subprocess.run([str(EXE), *args], capture_output=True, encoding='utf-8', errors='replace', timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout)

info = json.loads((ROOT / 'dist/BUILD_INFO.json').read_text('utf-8-sig'))
check('build_sources_match', all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest().upper() == sha
                                for p, sha in info['source_files_sha256'].items()))
check('executable_hashes_match', all(hashlib.sha256((ROOT / 'dist' / p).read_bytes()).hexdigest().upper() == sha
                                    for p, sha in info['executables_sha256'].items()))
lib = OUT / 'library' / '01 — Проверка'
lib.mkdir(parents=True)
fs = 48000
t = np.arange(fs * 4) / fs
x = .12 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, t / .1) * np.minimum(1, (4 - t) / .1)
source = lib / '01 — Test.wav'
sf.write(source, np.column_stack([x, x]), fs, subtype='PCM_16')
source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
scan = cli('scan', str(lib.parent), '--project', str(PROJ))
check('frozen_scan', scan['audio_files'] == 1 and scan['probe_failed'] == 0)
tracks = cli('tracks', '--project', str(PROJ), '--json')
tid = tracks[0]['id']
check('numbered_filename_fix_in_exe', tracks[0]['title'] == 'Test' and tracks[0]['track_no'] == 1)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
url = f'http://127.0.0.1:{port}'
log = (OUT / 'server.log').open('w', encoding='utf-8')
p = subprocess.Popen([str(EXE), 'serve-mcp', '--project', str(PROJ), '--port', str(port)], stdout=log, stderr=log)
try:
    ready = False
    for _ in range(100):
        try:
            ready = httpx2.get(url + '/health', timeout=1, trust_env=False).status_code == 200
        except Exception:
            pass
        if ready or p.poll() is not None:
            break
        time.sleep(.1)
    check('frozen_mcp_health', ready)
    check('unauthenticated_request_rejected', httpx2.post(url + '/mcp', json={}, trust_env=False).status_code == 401)
    token = json.loads((PROJ / 'mcp_secret.json').read_text('utf-8'))['token']

    async def flow():
        async with httpx2.AsyncClient(headers={'Authorization': f'Bearer {token}'}, timeout=120, trust_env=False) as hc:
            async with streamable_http_client(url + '/mcp', http_client=hc) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    names = [v.name for v in (await session.list_tools()).tools]
                    check('18_mcp_tools', len(names) == 18)

                    async def call(name, args):
                        r = await session.call_tool(name, args)
                        assert not r.is_error, name
                        return r.structured_content or json.loads(r.content[0].text)

                    await call('analyze_track', {'track_id': tid})
                    prop = await call('suggest_mastering', {'track_id': tid})
                    vn = prop['version_no']
                    prev = await call('create_preview', {'track_id': tid, 'version_no': vn, 'duration_s': 3})
                    link = next(iter(prev['clips'].values()))['download']['url']
                    response = await hc.get(link)
                    check('preview_download_is_wav', response.status_code == 200 and response.content[:4] == b'RIFF')
                    rejected = await session.call_tool('approve_processing_version', {
                        'track_id': tid, 'version_no': vn, 'decision': 'APPROVED', 'confirmation': 'yes'})
                    check('approval_requires_confirmation', bool(rejected.is_error))
                    return vn

    vn = asyncio.run(flow())
    with sqlite3.connect(PROJ / 'srm_project.sqlite') as db:
        row = db.execute('SELECT status, report_json FROM versions WHERE track_id=? AND version_no=?', (tid, vn)).fetchone()
        report = json.loads(row[1])
        check('mcp_render_persisted_in_shared_db', row[0] == 'RENDERED')
        check('latest_analysis_in_frozen_engine', report['output_measurements']['analysis_version'] == 'analysis-1.3')
    check('source_unchanged', hashlib.sha256(source.read_bytes()).hexdigest() == source_hash)
finally:
    p.terminate()
    p.wait(timeout=20)
    log.close()

env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
with (OUT / 'gui.log').open('w', encoding='utf-8') as glog:
    gui = subprocess.Popen([str(ROOT / 'dist/srm-gui/srm-gui.exe'), str(PROJ)], env=env, stdout=glog, stderr=glog)
    try:
        time.sleep(5)
        startup_log = (OUT / 'gui.log').read_text('utf-8', errors='replace')
        check('frozen_gui_stays_running', gui.poll() is None)
        check('frozen_gui_no_startup_exception', not any(word in startup_log for word in
              ('Traceback', 'ImportError', 'Failed to execute script', 'DLL load failed')))
    finally:
        if gui.poll() is None:
            gui.terminate()
        gui.wait(timeout=20)
result = {'checks': checks, 'evidence_folder': str(OUT), 'live_chatgpt': 'NOT VERIFIED',
          'listening_quality': 'NOT VERIFIED'}
(OUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
(ROOT / 'tests/qa/build_acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
print('Evidence:', OUT, flush=True)
