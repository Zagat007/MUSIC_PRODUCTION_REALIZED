# Workflow reference

| Step | Tool | Writes? | Needs user phrase |
|---|---|---|---|
| Check backend | `health_and_capabilities` | no | – |
| Browse | `list_projects`, `list_albums`, `list_tracks` | no | – |
| Rescan authorized root | `scan_library(root_id)` | DB only | – |
| Analyze | `analyze_track`, `analyze_album` + `get_job_status` | analysis cache/reports | – |
| Read results | `get_analysis_report(detail=summary|full)` | no | – |
| Propose | `suggest_mastering` | stores PROPOSED version | – |
| Render + audition | `create_preview(version_no)` | render + preview clips | – |
| Decide | `approve_processing_version` | approval record | `APPROVE <id> v<n>` / `REJECT …` / `SIGNOFF …` |
| Batch | `start_batch_render`, `cancel_job` | renders | – |
| Validate | `validate_release` | no | – |
| Export | `export_album` | local package | `EXPORT <album_id>` |
| Files | `get_release_manifest`, `get_artifact_download` | no (signed 10-min links) | – |
| Album art | `get_album_artwork` | no (signed cover-image link) | – |
| Description | `set_album_description` | album metadata | – |
| New cover | `generate_album_cover` | new cover version + derived MP3 tags | – |

Cover generation makes local abstract graphics guided by description keywords, not arbitrary neural
illustrations. The desktop also accepts image imports and earlier variants. New exports carry the chosen
JPEG in each MP3; finished exports and original audio remain intact.

Loudness policies: `preset_window` (move into a style window only if outside it; limiting capped),
`preserve` (no level change except peak safety), `target` (explicit LUFS). Default ceiling −1.0 dBTP;
consider −2.0 dBTP for loud masters destined for lossy streaming.
