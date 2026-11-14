# Status vocabulary

- `TECHNICAL_PASS` / `NEEDS_LISTENING_REVIEW` / `FAILED` — technical QA only, never aesthetic approval.
- Severity: `NONE`, `MINOR`, `MODERATE`, `SEVERE`, `UNCERTAIN` (independent of confidence `LOW/MEDIUM/HIGH`).
- `REQUIRES_LISTENING_REVIEW` — cannot be judged by numbers.
- `NEEDS_SOURCE_REGENERATION_OR_STEMS` — no safe automatic fix; regenerate in Suno or use stems.
- `SOURCE_LIMITATION` — e.g. lossy bandwidth limit; cannot be restored.
- Version states: `PROPOSED` → `RENDERED` → `APPROVED` / `REJECTED` (immutable; changes = new version).
- Export: `DRAFT_NOT_RELEASE_READY` while any approval, listening sign-off, rights/AI field or lossy-source
  note is open; `TECHNICAL_PASS_AND_HUMAN_GATES_RECORDED` otherwise. The app never says "approved for release".
- `UNAVAILABLE` — optional engine not installed (e.g. separation models, Essentia).
