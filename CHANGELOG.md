# Changelog

## Unreleased
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
