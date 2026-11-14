# Release profiles

Export profiles (what is written) are separate from distributor rule sets (what platforms document).
`srm profiles --project <p>` prints both with verification status. Override/add distributor rules in
`<project>/release_profiles.json` (`{"distributors": {"name": {...}}}`); user entries are marked
`USER_DEFINED`.

| Profile | Output |
|---|---|
| `release_wav` | WAV PCM s24le, native 44.1/48 kHz (others converted once with soxr), stereo |
| `compat_wav` | WAV PCM s16le, 44.1 kHz, TPDF dither once (true peak re-measured, may FAIL → re-render at 44.1) |
| sidecar `preview_mp3` | MP3 320 kbps from the delivered master (preview only) |
| sidecar `flac_archive` | FLAC of the delivered master, verified sample-identical to the WAV |

## Verification status (checked 2026-11-14)

| Source | Status | Key facts used |
|---|---|---|
| Apple Video & Audio Asset Guide | VERIFIED | WAV/ALAC/FLAC; 44.1–192 kHz; 16/24-bit; mono rejected → dual mono. Apple Digital Masters: native ≥24-bit/44.1 kHz source, no upsampling/bit-padding, not lossy-derived → **Suno AAC sources are not eligible**. Hi-Res Lossless needs ≥88.2 kHz. |
| Spotify loudness normalization | VERIFIED | −14 LUFS is playback normalization (Loud −11 / Quiet −19), not a delivery requirement; TP < −1 dBTP, < −2 dBTP for masters louder than −14 LUFS. |
| Yandex Music content rules | VERIFIED (AI rules only) | Must disclose AI use (vocals/music/lyrics); incomplete/inaccurate disclosure is a violation. No audio spec on that page. |
| Suno commercial-use article | VERIFIED | Rights for songs downloaded while on a paid plan; no copyright guarantee. Pre-subscription article not checked. |
| DistroKid formats / AI credits | **NOT VERIFIED** | HTTP 403 to automated access. Check manually. |
| FFmpeg `loudnorm` docs | not used | Loudness normalisation is done by measured static gain + verified limiter, not `loudnorm`. |

Rights/AI checklist fields per track: `ai_tool`, `ai_model_version`, `ai_generated_vocals/music/lyrics`,
`human_contribution_description`, `suno_plan_at_creation`, `downloaded_while_on_paid_plan`,
`user_confirms_commercial_rights`, `samples_or_third_party_material`, `distributor_ai_disclosure_completed`.
All default to `UNKNOWN` and must be answered by the user.
