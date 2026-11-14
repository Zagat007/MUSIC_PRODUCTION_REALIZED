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
