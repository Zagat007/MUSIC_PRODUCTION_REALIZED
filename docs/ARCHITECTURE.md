# Architecture

```
            ┌──────────── front-ends (no DSP) ────────────┐
            │  srm.cli (argparse)   srm.gui (PySide6)      │
            │  srm.mcp_server (MCP over HTTP, optional)    │
            └───────────────┬──────────────────────────────┘
                            │  same Python API
                 srm.service.engine.Engine  (+ Mastering/Repair/Album mixins)
                            │
   ┌──────────────┬─────────┴───────┬───────────────┬────────────────┐
 srm.ingest     srm.analysis      srm.dsp          srm.release      srm.core
 probe/decode   loudness, TP,     convert, chain,  profiles,        db (SQLite+migrations),
 textmatch      metrics, tempo,   limiter, repair, metadata,        paths (atomic, versioned),
                style, defects,   quantize/dither  validate         tools (FFmpeg, fixed args)
                report
```

* **Source protection**: sources are opened read-only; SHA-256 recorded at scan; `_check_source`
  refuses changed sources; `verify-sources` re-hashes; every write is temp-file + rename and never
  overwrites (`FileExistsError`). Exports are built in `.building_*` and atomically renamed to `vNNN`.
* **Immutable versions**: `versions` rows go PROPOSED → RENDERED → APPROVED/REJECTED. Each has the
  full JSON config + hash, output SHA-256, report. Re-rendering a version is refused; changes create a
  new version. Approvals are separate rows (approve / reject / listening sign-off) with actor and note.
* **Jobs**: `jobs` + `job_items` (track × stage). `run_job` skips DONE items, retries RUNNING items left
  by a crash, records per-item errors and continues; cancellation is cooperative (finish current items →
  `CANCELLED`, resumable). Idempotency keys de-duplicate job creation.
* **Caching**: analyses keyed by (track, source SHA-256, `ANALYSIS_VERSION`) — bump the version on any
  measurement change.
* **Project directory**: `srm_project.sqlite`, `project.json`, `renders/<track>/vNNN/`,
  `previews/`, `reports/<track>/`, `RELEASES/<album>/vNNN/`, `logs/`, `mcp_secret.json` (MCP only).
* **Concurrency**: thread pool (default 2 workers; NumPy/SciPy/FFmpeg release the GIL); SQLite WAL with
  an RLock-serialised connection. Memory ≈ 0.5–1 GB per concurrently processed 5-minute track.
