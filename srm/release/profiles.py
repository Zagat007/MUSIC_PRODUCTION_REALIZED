"""Export profiles and distributor rule sets.

Generic export profiles (what files we write) are separate from distributor rule sets
(what a given platform documents). Each distributor rule set records where and when it was
verified; unverifiable sources are marked NOT VERIFIED and must be checked by the user.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

EXPORT_PROFILES = {
    "release_wav": {"label": "Release WAV (24-bit PCM, 44.1/48 kHz native)", "format": "WAV", "codec": "pcm_s24le",
                    "bits": 24, "allowed_rates": [44100, 48000], "channels": 2},
    "compat_wav": {"label": "Compatibility WAV (16-bit PCM, 44.1 kHz)", "format": "WAV", "codec": "pcm_s16le",
                   "bits": 16, "allowed_rates": [44100], "channels": 2},
}
SIDECARS = {
    "preview_mp3": {"label": "MP3 320 kbps preview (from approved master; not a delivery source)", "bitrate": "320k"},
    "flac_archive": {"label": "FLAC lossless archive of the delivered master"},
}

VERIFIED_ON = "2026-11-14"

DISTRIBUTOR_RULES = {
    "generic": {
        "label": "Generic distributor (no platform-specific rules)",
        "accepted": ["WAV PCM 16/24-bit 44.1/48 kHz stereo"],
        "verification": {"status": "N/A", "note": "Generic preset - always check your distributor's current upload form."},
    },
    "apple_music_direct": {
        "label": "Apple Music (direct delivery spec)",
        "accepted_codecs": ["pcm_s16le", "pcm_s24le", "alac", "flac"],
        "accepted_rates": [44100, 48000, 88200, 96000, 176400, 192000],
        "accepted_bits": [16, 24],
        "mono": "REJECTED - dual mono (identical L/R) required when no true stereo exists",
        "apple_digital_masters": ("Separate eligibility workflow: requires a native >=24-bit/44.1 kHz source; "
                                  "upsampling/bit-padding and lossy-derived sources are not allowed. "
                                  "Suno AAC/M4A-derived masters are NOT eligible."),
        "hi_res_lossless": "Requires 24-bit at 88.2 kHz or higher - never applicable to 44.1/48 kHz Suno sources.",
        "verification": {"status": "VERIFIED", "date": VERIFIED_ON,
                         "url": "https://help.apple.com/itc/videoaudioassetguide/en.lproj/static.html",
                         "note": "Apple's direct spec; your distributor (aggregator) may differ."},
    },
    "spotify": {
        "label": "Spotify (loudness guidance)",
        "loudness": ("Playback normalised to -14 LUFS (Normal); Loud -11, Quiet -19. Guidance, not a delivery "
                     "requirement; audio is not altered on upload."),
        "true_peak": "Keep below -1 dBTP; below -2 dBTP for masters louder than -14 LUFS.",
        "verification": {"status": "VERIFIED", "date": VERIFIED_ON,
                         "url": "https://support.spotify.com/ua-uk/artists/article/loudness-normalization/"},
    },
    "yandex_music": {
        "label": "Yandex Music (AI content rules)",
        "ai_disclosure": ("Must disclose whether and how AI was used (e.g. vocals, music, lyrics). Missing, incomplete "
                          "or inaccurate disclosure is a rules violation; undisclosed AI may be labelled."),
        "technical_audio": "NOT VERIFIED - rules page has no audio format specification; use your distributor's spec.",
        "verification": {"status": "VERIFIED (AI rules only)", "date": VERIFIED_ON,
                         "url": "https://www.yandex.ru/support/music/en/content-requirements/rules"},
    },
    "distrokid": {
        "label": "DistroKid",
        "accepted": "NOT VERIFIED - support page returned HTTP 403 to automated access on " + VERIFIED_ON,
        "ai_credits": "NOT VERIFIED - check DistroKid 'AI credits' article before upload.",
        "verification": {"status": "NOT VERIFIED", "date": VERIFIED_ON,
                         "url": "https://support.distrokid.com/hc/en-us/articles/360013647753",
                         "note": "Verify manually; do not equate with Apple's direct-delivery spec."},
    },
}

SUNO_RIGHTS_NOTE = {
    "verified": VERIFIED_ON,
    "url": "https://help.suno.com/en/articles/9601665",
    "summary": ("Suno states commercial-use rights apply to songs downloaded while on a paid plan; commercial-use rights "
                "do not guarantee copyright protection. Free-plan / pre-subscription terms must be checked separately "
                "(https://help.suno.com/en/articles/2425729). This tool does not determine legal ownership."),
}


def load_profiles(project_root: str | Path | None = None) -> dict:
    """Built-ins, optionally overridden by <project>/release_profiles.json (user-editable)."""
    rules = copy.deepcopy(DISTRIBUTOR_RULES)
    if project_root:
        p = Path(project_root) / "release_profiles.json"
        if p.exists():
            user = json.loads(p.read_text("utf-8"))
            for k, v in user.get("distributors", {}).items():
                rules.setdefault(k, {}).update(v)
                rules[k].setdefault("verification", {"status": "USER_DEFINED"})
    return {"export_profiles": EXPORT_PROFILES, "sidecars": SIDECARS, "distributors": rules,
            "suno_rights": SUNO_RIGHTS_NOTE}
