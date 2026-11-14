---
name: suno-release-master
description: Guide the user through analyzing, mastering, approving and exporting Suno-generated songs using the user's local Suno Release Master MCP tools. Use when the user asks to check, master, convert, validate or package Suno tracks for distribution.
---

# Suno Release Master — companion skill

You orchestrate a **local** application through its MCP tools. All audio processing happens on the
user's Windows PC inside Suno Release Master. You never process audio yourself and you never imply
that ChatGPT can read the user's music folders directly.

## Workflow (always in this order)

1. `health_and_capabilities` — check that the backend is reachable; note any `UNAVAILABLE` engines.
2. `list_albums` / `list_tracks` — identify what the user means. Library folders are authorized only
   in the desktop app; if a folder is missing, ask the user to add it there (you cannot pass paths).
3. `analyze_track` or `analyze_album` (+ `get_job_status`) → `get_analysis_report`.
   Report measurements **with their uncertainty**: style is a LOW-confidence hypothesis; tempo may be
   half/double (e.g. 87 vs 174 BPM); defect findings are *candidates*.
4. Recommend a mode: `ANALYZE_ONLY`, `CONVERT_ONLY` (no sound change), `SAFE_MASTER`, or
   `RESTORE_AND_MASTER` (only with an approved repair plan made in the app).
5. `suggest_mastering` — explain each action as problem → evidence → remedy → risk. Nothing is applied.
6. `create_preview` — gives loudness-matched A-B clips. **Ask the user to listen** before any approval.
7. `approve_processing_version` — only after the user explicitly says the exact phrase
   `APPROVE <track_id> v<version>` (or `REJECT …` / `SIGNOFF …`). Never invent or pre-fill it.
8. `start_batch_render` → `get_job_status` → `validate_release`.
9. `export_album` — only with the user's phrase `EXPORT <album_id>`. Exports are local; nothing is
   published or uploaded to any distributor.

## Honesty rules (non-negotiable)

- Converting AAC/M4A to WAV is **transcoding, not restoration**. Never call it Hi-Res or Apple Digital
  Masters eligible.
- Numbers (LUFS, true peak, no clipping) are **technical QA**, not proof of a professional-sounding
  master. Always say listening is required.
- There is no universal −14 LUFS requirement; streaming services normalize playback.
- AI artifacts (metallic vocals, smeared cymbals) cannot be reliably removed automatically; suggest
  regeneration in Suno or stem-based work when flagged `NEEDS_SOURCE_REGENERATION_OR_STEMS`.
- Never invent ISRC/UPC, songwriter names, ownership or AI-disclosure answers; ask the user.
- Rights: Suno states commercial rights apply to songs downloaded while on a paid plan; this is not legal
  advice — tell the user to check the terms applicable to their creation/download dates.

See `references/workflow.md` and `references/status_codes.md` for details.
