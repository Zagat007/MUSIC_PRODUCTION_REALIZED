# IMPLEMENTATION_LOG — Suno Release Master

Specification: `docs/SPEC_v1_1.md` (v1.1 "Zero-cost edition", supersedes v1.0 — adds zero-cost/offline constraint).
Status vocabulary: PASS / FAIL / NOT VERIFIED.

## M0 — Environment, decisions, zero-cost audit (2026-11-14)

Environment (inspected, not assumed):
- Windows 11 Pro 10.0.26300 x64, 12 logical CPUs, ~62 GB free on E:.
- Python 3.14.4 (default) and **3.11** (used: spec targets 3.11/3.12; venv `../.venv`).
- FFmpeg/ffprobe **8.1.1 gyan.dev full build** on PATH: `--enable-gpl --enable-version3`, libsoxr, libmp3lame, native aac/alac/flac.
- git 2.56. No `uv`.
- Python deps (pinned in `requirements-lock.txt`): numpy 2.4.6, scipy 1.17.1, soundfile 0.14.0 (libsndfile, LGPL),
  pyloudnorm 0.2.0 (MIT, used only as test cross-check), PySide6 6.12.0 (LGPL-3), mcp 2.3.0 (MIT; `FastMCP` renamed
  `MCPServer` in v2 — verified in installed source), starlette/uvicorn (BSD), pytest, ruff.

Decisions:
- Package name `srm` (spec's `app/` renamed to avoid a generic top-level name); DSP engine has no GUI/MCP imports.
- No librosa/numba: tempo/onset implemented with SciPy STFT (fewer heavy deps, CPU-only).
- Loudness: own BS.1770-4 implementation (arbitrary-rate K-weighting from libebur128 prototype), cross-validated
  against pyloudnorm and FFmpeg `ebur128` in tests.
- True peak: 4x (fs<96k) / 2x polyphase, 64-tap/phase Kaiser(9) sinc. Cross-checked analytically and vs FFmpeg.
- FFmpeg is **not bundled** (the local build is GPL); the app locates a user-installed free build (PATH,
  `SRM_FFMPEG`, or `tools/ffmpeg/`). See LICENSE-THIRD-PARTY.md.
- Essentia (AGPL) / Discogs weights (CC BY-NC-SA) **not bundled**; heuristic style fallback; adapter reports UNAVAILABLE.
- Demucs / DeepFilterNet not installed (maintenance + licence + "no speech denoise on full mix" policy).

Zero-cost audit (M0): all core dependencies are free/open-source; no account, API key, payment, cloud or network
access is required by the core. Budget gate M0: **PASS — zero new charges**.

Known unknowns: official distributor/OpenAI docs verification (scheduled M4/M6, browser availability permitting);
Windows .exe packaging (M5/M7).

## M1 — Ingest, analysis, conversion, versions, CLI, reports

Files: `srm/core/{tools,paths,db}.py`, `srm/ingest/{probe,decode,textmatch}.py`,
`srm/analysis/{loudness,truepeak,metrics,tempo,genre,defects,analyze,report}.py`, `srm/dsp/{quantize,convert}.py`,
`srm/service/engine.py`, `srm/cli/main.py`, `tests/audio_fixtures/make_fixtures.py`, `tests/{conftest,unit/*,integration/*}`.

Tests: `..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` → **28 passed** (exit 0).

Defects found and fixed during M1 (root causes):
1. Click detector flagged synthetic drum/hat onsets → added HF-isolation test (residual must fall back to pre-event
   level) + sustained-onset rejection. Now: 0 false positives on music fixtures; detects clear clicks.
   Known miss: click masked by a hi-hat burst (documented limitation, test asserts it explicitly).
2. Clipping detector treated a full-scale alternating tone as one flat run (used |x|) → sign-aware runs.
3. Tempo locked to 1/3 tempo → log-Gaussian prior; candidate list now includes top distinct periodicity peaks
   with half/double readings (172 BPM fixture lists 114.7 primary + 86.5 + 173.1 → ambiguity flagged).
4. True-peak filter change briefly gave +12 dB (SciPy already applies the ×up gain to custom filters) → fixed;
   analytic case −6.021 vs −6.0206 dBTP.
5. Abrupt start/end false positives on short fades → compare first/last 10 ms with adjacent 40 ms (ramp detection).

Acceptance items touched: #2 source hashes unchanged (PASS, test_sources_untouched), #3 AAC vs ALAC (PASS),
#4 exported WAV probed from bytes (PASS), #5 CLI path (PASS), #7 TP cross-check (PASS).
Budget gate M1: PASS — zero new charges, no network.

Next: M2 mastering chain.

## M2 — Mastering chain, true-peak limiting, presets, A-B, codec preview

Files: `srm/dsp/{chain,presets,proposal}.py`, `srm/service/mastering.py` (Engine mixin), tests
`tests/unit/test_chain.py`, `tests/integration/test_m2_mastering.py`.

Implemented: bounded chain (dc_hpf → eq → compressor → stereo → gain → limiter) with per-stage bypass and
per-stage loudness/TP measurements; explainable proposals (problem/evidence/remedy/risk/settings); presets as
suggested windows (no forced −14 LUFS; level is capped so limiting stays ≤ preset max GR); immutable
PROPOSED→RENDERED→APPROVED versions with tamper check; real MP3 320k codec preview decoded and measured;
loudness-matched A-B clips (source / master / encoded MP3, MP3 delay aligned); album cohesion report.

Tests: `pytest -q` → **43 passed** (exit 0).

Defects found and fixed (root causes):
1. **Limiter leak at file start** (integration test: +2.7 dBTP output): causal box-average padded with gain 1.0
   before sample 0 → first ~1.5 ms unlimited; iterations compensated with 23.7 dB GR. Fixed (pad with first
   gain); GR now 4.2 dB, TP −1.10 dBTP. Regression test added.
2. Untouched regions not bit-identical: cumulative-sum rounding (1e-10) and asymptotic release. Fixed by
   averaging the gain deficit with sub-1e-12 snap and snapping release to unity below 1e-9 (≪ 1 LSB @24-bit).
3. Stale analysis cache after a measurement fix gave wrong proposals → `ANALYSIS_VERSION` is the cache key and
   must be bumped on any measurement change (now `analysis-1.1`).
4. Performance: 64-tap/phase interpolator → 24 taps (identical readings, 2.2× faster); single pass for
   per-channel + linked TP. Limiter keeps 0.1 dB internal margin for the ≤0.14 dB 4× under-read.

Acceptance: #7 TP within ceiling on rendered bytes + real MP3 preview analysed (PASS); #8 bypass/identity,
untouched-region and A-B path (PASS). Budget gate M2: PASS — zero new charges, offline.
Not verified: subjective sound quality of any preset (requires listening).

## M3 — Candidate artifact detection, localized repair, stems

Files: `srm/dsp/repair.py` (artifact registry, LSAR interpolation, declip candidate, fades, regression checks),
`srm/service/repairs.py` (repair plans, decisions, A-B repair previews, stem import/validation, separation
capability), DB migration 3 (`repair_plans`), `tests/integration/test_m3_repair.py`.

Policy implemented: AUTO only for HIGH-confidence clicks that pass regression checks **and** the user confirms the
auto set; declip/fades always need approval; failed-check edits cannot be approved; RESTORE_AND_MASTER refuses to
run without an approved plan; widespread clipping / dropouts / perceptual artifacts → NEEDS_SOURCE_REGENERATION_OR_STEMS
or REQUIRES_LISTENING_REVIEW. Repairs are re-checked when applied during render and logged per edit.

Tests: `pytest -q` → **50 passed** (exit 0).

Defects found and fixed:
1. Regression checks evaluated both channels while edits are per channel → false failures. Scoped per channel and
   added an "other channels bit-identical" check.
2. LSAR drift on bass-dominated material (AR(32) cannot carry long periods) → trend split: zero-phase low-pass over
   a linearly bridged gap + LSAR on the residual.
3. Flawed plausibility tests: RMS of a 0.6 ms region vs ±50 ms context (crest bias) and reflect padding that
   itself looked like a click → replaced by HF-energy ratio, local-peak bound and detector run on real context.
4. Declip offered on wall-to-wall clipping (would invent +8.9 dB peaks; checks rejected it) → widespread clipping
   classified up front as NEEDS_SOURCE_REGENERATION_OR_STEMS.
5. Clipping detector ignored clipped-then-attenuated audio (plateau below −0.3 dBFS) → floor −6 dBFS, MEDIUM
   confidence below full scale. `ANALYSIS_VERSION` → analysis-1.2.

Source separation: UNAVAILABLE (no licence-checked model installed); stem import + sum-vs-mix null test AVAILABLE.
Acceptance #8 (no auto-repair on uncertain regions; untouched sections bit-identical; A-B + history): PASS.
Budget gate M3: PASS — zero new charges, offline.

## M4 — Albums, prompt association, metadata, distributor profiles, release export, validation

External verification performed 2026-11-14 (WebFetch): Apple direct-delivery spec **VERIFIED**
(WAV/ALAC/FLAC; 44.1–192 kHz; 16/24-bit; mono rejected → dual mono; ADM forbids lossy sources / bit-padding;
Hi-Res needs ≥88.2 kHz); Spotify loudness **VERIFIED** (−14 LUFS is playback normalisation, not a delivery
requirement; −1 dBTP, −2 dBTP for louder masters); Yandex AI disclosure **VERIFIED** (no audio spec on that page);
Suno commercial rights page **VERIFIED** (rights for downloads while on paid plan; no copyright guarantee);
DistroKid formats/AI credits and OpenAI help-center MCP article: **NOT VERIFIED** (HTTP 403 to automated fetch);
OpenAI developers quickstart **VERIFIED** (remote HTTPS `/mcp`, streamable HTTP; "Add custom MCP server").

Files: `srm/release/{profiles,metadata,validate}.py`, `srm/service/albums.py`, `srm/cli/extra.py`,
`tests/integration/test_m4_release.py`.

Implemented: album create/assign/reorder/gaps (sources never moved); human text-match decisions that survive
rescans; validated metadata (ISRC/UPC syntax only, user-supplied; unknown → UNKNOWN; songwriters/rights/AI never
inferred); export requires `confirm`; unapproved renders only with `allow_draft`; crash-safe build in
`.building_*` then atomic rename to `vNNN`; package with WAV_MASTER, MP3_PREVIEW, FLAC_ARCHIVE, QA json/html,
metadata csv/json, rights/AI checklist, cover-art checklist, processing manifest, UNRESOLVED_WARNINGS, album QA;
independent validator from bytes (codec/bits/rate/channels via ffprobe, checksum, decode, duration vs source,
silence, TP vs policy, loudness vs render, MP3 overs, FLAC==WAV sample-exact). Status is
`DRAFT_NOT_RELEASE_READY` whenever any human gate/rights item/lossy-source note is open; the app never labels
a package "approved".

Tests: `pytest -q` → **57 passed** (exit 0). One test-expectation bug fixed (album order after reorder).
Acceptance #4, #9, #10: PASS. Budget gate M4: PASS — zero new charges; network used only by the developer for
documentation checks, never by the application.

## M5 — Windows GUI, jobs, A-B listening, packaging

Files: `srm/gui/app.py`, `tests/integration/test_m5_gui.py`, `scripts/{build_windows,srm_cli_entry,srm_gui_entry}`.
GUI (PySide6, dark, Russian): Album Library (tree + track table, batch select, scan, analyze/convert jobs,
pause/cancel/resume, per-item failure list, progress bar), Track Analysis (waveform with defect markers and
click-to-seek, A-B player switching loudness-matched clips at the same position, full meter table, defect table,
matched prompt/lyrics with lossy warning, manual style override, HTML report), Repair (plan, A-B/difference preview,
approve/reject/accept-auto), Mastering (mode/policy/target/ceiling, explainable proposal, editable JSON chain with
bounds, versions, render/approve/sign-off), Release Validation (cohesion, export with confirmation, validation,
copy package to chosen folder), Settings (workers, health/capabilities).
Tests: offscreen GUI end-to-end (scan → analyze job → meters → propose → render → preview → A-B swap → 6 tab
screenshots) **PASS**. Visual check of real screenshots done; fixed analysis-tab layout (player box too tall).
Windows build (PyInstaller 6.22.3, onedir): `dist/srm/srm.exe` (132 MB), `dist/srm-gui/srm-gui.exe` (274 MB),
both exit 0. Frozen `srm.exe` ran scan → analyze → convert → propose → render → export (draft, FLAC+MP3) →
validate (technical PASS) → verify-sources PASS. Frozen GUI launched and stayed up 8 s with no stderr.
Not verified: Windows 10, installer/.msi, code signing.

## M6 — MCP server + companion skill

Files: `srm/mcp_server/server.py`, `plugin/skill/{SKILL.md,agents/openai.yaml,references/*}`,
`plugin/manifest/README.md`, `docs/OPENAI_INTEGRATION.md`, `tests/integration/test_m6_mcp.py`.
18 tools (spec list) with ToolAnnotations; bearer or capability-path auth (constant-time), rate limit, audit log,
DNS-rebinding protection, 127.0.0.1 default bind, no path parameters, confirmation phrases for approval/export,
10-minute HMAC-signed single-file download URLs. Tests with a real uvicorn server and the real MCP client:
**4 passed** (401 on missing/wrong token and wrong capability path, 403 on tampered/unsigned artifact, FORBIDDEN on
unauthorized root id, CONFIRMATION_REQUIRED without phrase, analysis/proposal/approval/render/export land in the
same project DB, downloads return real RIFF bytes). Defect fixed: test client used v1 field names (`isError`) —
SDK v2 uses `is_error`/`structured_content`.
Live ChatGPT connection: **NOT VERIFIED** (needs user's account + public HTTPS URL). Skills article: NOT VERIFIED (403).
Budget gate M5/M6: PASS — zero new charges; tunnels documented as optional free-tier choices under user control.

## M7 — Full regression, robustness, security/licence audit, batch dry-run, evidence

Files: `tests/integration/test_m7_batch_robustness.py`, `scripts/make_qa_evidence.py`, `scripts/test_windows.ps1`,
`docs/{AUDIO_VALIDATION,LIMITATIONS,USER_GUIDE_RU,RELEASE_PROFILES,DSP_DECISIONS,ARCHITECTURE}.md`,
`LICENSE-THIRD-PARTY.md`, `README.md`, `CHANGELOG.md`, `tests/qa/evidence/*`, `tests/qa/pytest_log.txt`.

* 100-file batch (4 album folders, AAC): scan → analysis interrupted at 37 + simulated crash (item left RUNNING)
  → resumed from a fresh Engine (idempotency key returns same job) → 100/100 done, 0 failed, no duplicate
  analyses, incremental rescan (100 unchanged), all 100 source hashes identical. ~44 s.
* Robustness: missing FFmpeg (clear message, health UNAVAILABLE), read-only sources + undecodable text (works;
  bit-exact convert), low disk (refuses, nothing registered), cancel → CANCELLED → resume COMPLETED,
  corrupt/truncated files (per-file failure, job continues). GPU: not used (CPU-only design).
* Security: no network imports outside the optional MCP module (test); MCP fails closed (tests); no shell/delete/
  path tools; FFmpeg fixed argument lists with `file:` inputs.
* Licence audit from installed metadata → LICENSE-THIRD-PARTY.md (FFmpeg GPL build not bundled; PySide6 LGPL
  onedir; libsndfile LGPL DLL; Essentia/AGPL and model weights excluded).
* Evidence run with the frozen `srm.exe` (all exit codes as expected, unconfirmed export refused with exit 1):
  exported 24-bit/48 kHz PCM, TP −1.104 dBTP on all tracks (ceiling −1.0), MP3 previews −0.67…−1.30 dBTP,
  FLAC == WAV, status DRAFT_NOT_RELEASE_READY (no listening sign-off, artist/AI/rights unset, lossy sources).
* Executables rebuilt after M6 (dist/ now includes the MCP module); frozen `srm.exe serve-mcp` answered
  /health and returned 401 for unauthenticated /mcp.
* Final regression: `scripts/test_windows.ps1` → **68 passed, exit code 0** (log `tests/qa/pytest_log.txt`).
  `ruff check srm tests scripts` → clean.

### Final acceptance table

| # | Gate | Result |
|---|---|---|
| 1 | ≥100 fixtures discovered/analyzed, resume after interruption | PASS |
| 2 | Input hashes unchanged incl. failure/cancel | PASS |
| 3 | AAC-in-M4A lossy + warned; ALAC distinguished | PASS |
| 4 | Exported WAV codec/rate/bits/channels/duration probed from bytes | PASS |
| 5 | Real CLI path and GUI path on fixtures | PASS (CLI incl. frozen exe; GUI offscreen + frozen launch) |
| 6 | Reports with pre/post measurements, configs, provenance, per-track errors | PASS |
| 7 | True peak cross-checked; within profile tolerance; real MP3 previews analysed | PASS |
| 8 | No auto-repair on uncertain regions; bypass/history/A-B | PASS |
| 9 | Style uncertainty + override; prompt matching/ambiguity | PASS (heuristic only, LOW confidence) |
| 10 | Ordered export, truthful metadata, DRAFT when unresolved | PASS |
| 11 | MCP reaches same backend, not mocked; unauthorized fails closed | PASS (localhost); live ChatGPT NOT VERIFIED |
| 12 | Windows install/build steps + executable | PASS on Windows 11 x64 (onedir, unsigned); Windows 10 / installer NOT VERIFIED |
| 13 | CI-style test run with logged commands/exit codes; listening reported separately | PASS (automated); listening sign-off NOT VERIFIED (human task) |
| 14 | Graceful recovery (FFmpeg missing, models missing, partial files, encodings, read-only, low disk) | PASS (GPU: N/A) |
| 15 | Provenance, reproducibility, licensing documented | PASS |
| 16 | Zero-cost offline acceptance | PASS for no-payment/no-account/no-API; physically disconnected network run NOT VERIFIED (code audit only) |

Subjective audio quality of any master: **NOT VERIFIED** — requires human listening on real Suno material.
Budget gate M7: PASS — zero new charges.
