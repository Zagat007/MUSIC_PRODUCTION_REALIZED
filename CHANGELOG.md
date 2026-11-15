# Changelog

## Unreleased
New main window (`srm/gui/flow.py`) and album pipeline (`srm/service/pipeline.py`): open a folder and
every album appears (an album folder gives one album; `_*` and service folders are skipped); six ordered
stages - analysis, versions, repair, mastering, listening, release - each with its own button, plus AUTO
for one album or for all. Automatic technical choice among versions of a song (losers are excluded from
the release, never moved), repair that leaves rhythm alone, preset from the declared album style
(`СТИЛЬ:` line, album name or the user's choice), cover taken from `COVER.png`, instant original/master
player, plain-language log. One gold-on-graphite theme for all windows (`srm/gui/theme.py`).

Blind comparison of song versions (`srm/versus`, `srm-versus.exe`, button in the studio sidebar):
groups of 2+ versions found by file name, technical check with admission rules (shorter than two
minutes, corrupt, exact SHA-256 duplicates), random blind labels A/B/C/..., full round for 2-5 versions
and a two-stage tournament above that, gap-free switching at the same position with playback-only
loudness matching, 12 weighted criteria, technical and overall winner with confidence, final A/B on a
near tie, recorded manual override, TXT/CSV/JSON report, confirmed apply (rename, move to album,
`_ОТЛОЖЕНО_ВЕРСИИ`) that never deletes or overwrites, state saved after every step.
User guide: docs/VERSUS_GUIDE_RU.md. FFmpeg may also be shipped in `tools/ffmpeg` beside the program.

Audit fixes (independent review of f412904; regression tests in tests/integration/test_audit_regressions.py):
- Listening: a prepared audition is dropped on project switch (track ids repeat across projects), so an
  unheard version can no longer be signed off. The latest approval wins and supersedes the older one.
- GUI: closing the window during work asks, cancels the job and waits for the worker instead of aborting
  the process; one job at a time (no duplicate renders); renders left RENDERING by a crash become FAILED.
- Jobs: items are claimed atomically, a version is rendered by one caller only.
- Release validator never raises (corrupt MP3 / missing manifest or metadata -> FAIL), checks the manifest
  against the hash recorded at export, matches tracks by file name and accounts for every file in the package.
- Render: a damaged cover or MP3 check problem is reported as a warning, the master stays RENDERED.
- Sources are re-hashed before processing (same-size edits are detected); `verify-sources` reports missing files.
- Proposals: the preset's peak-reduction bound also applies to sources already above the ceiling.
- Repair: edits for mono sources apply to both output channels.
- Storage: metadata updates are atomic; a failed BEGIN no longer leaks the DB lock; "analyzed" requires the
  current analysis version; 1/0 flags are stored as booleans.
- Ingest: albums are identified by folder (same-named folders in different roots stay separate, renamed
  albums keep their folder); edited prompt/lyrics files are re-read on rescan.
- MCP: the capability token is redacted from server logs; the secret file is restricted to the current user.
Album descriptions with autosave, square cover import, free local abstract generation with variant history,
MP3 APIC embedding without audio re-encoding, cover-bytes validation and three artwork MCP tools.
Album-first folder tree: real library root with album children, album-scoped counts/actions and synchronized
audition/export context. The main screen opens the first album instead of a mixed all-tracks view.
Focused studio UI: three steps (library, audition, release), warm dark styling, plain-language A/B player,
one-click conservative preview, explicit listening approval, draft export and advanced tools on demand.
Worker callbacks are delivered on the GUI thread; album preparation reuses existing rendered/approved versions.
Real-library fixes: click detector absolute floor (fade-out false positives), like-for-like tempo ambiguity,
LOW-confidence style hints no longer pick a loudness preset. analysis-1.3.

## 0.1.0 — 2026-11-14
First working build: ingest/probe/decode verification, BS.1770 loudness + oversampled true peak, defect
candidates, CONVERT_ONLY (bit-exact path), bounded mastering chain with verified true-peak limiter, explainable
proposals, loudness-matched A-B previews, MP3 codec check, LSAR click repair with regression checks, repair plans,
stem validation, album management, metadata/rights/AI checklists, crash-safe export + byte-level validator,
PySide6 GUI, CLI, optional authenticated MCP server, companion ChatGPT skill, Windows PyInstaller build.
