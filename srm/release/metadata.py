"""Release metadata model. Unknown values stay UNKNOWN/blank - nothing is invented.

ISRC/UPC are accepted only when supplied by the user and syntactically valid. Songwriter
names, ownership and AI involvement must be entered by a human.
"""

from __future__ import annotations

import re

UNKNOWN = "UNKNOWN"

TRACK_FIELDS = ["title", "artist", "version", "genre", "language", "year", "isrc", "explicit", "lyrics",
                "songwriters", "producers", "performers", "credits_note"]
ALBUM_FIELDS = ["album_title", "album_artist", "release_type", "genre", "year", "upc", "label", "copyright_line",
                "phonographic_line", "language"]
AI_FIELDS = ["ai_tool", "ai_model_version", "ai_generated_vocals", "ai_generated_music", "ai_generated_lyrics",
             "human_contribution_description"]
RIGHTS_FIELDS = ["suno_plan_at_creation", "downloaded_while_on_paid_plan", "user_confirms_commercial_rights",
                 "samples_or_third_party_material", "distributor_ai_disclosure_completed"]
REQUIRED_TRACK = ["title", "artist"]
REQUIRED_ALBUM = ["album_title", "album_artist"]

_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$")
_UPC = re.compile(r"^\d{12,13}$")


class MetadataError(ValueError):
    pass


def normalize_isrc(v: str) -> str:
    s = (v or "").replace("-", "").replace(" ", "").upper()
    if not _ISRC.match(s):
        raise MetadataError(f"invalid ISRC {v!r} (expected CC-XXX-YY-NNNNN)")
    return s


def validate_upc(v: str) -> str:
    s = (v or "").strip()
    if not _UPC.match(s):
        raise MetadataError(f"invalid UPC/EAN {v!r}")
    return s


def clean_track_meta(update: dict) -> dict:
    allowed = set(TRACK_FIELDS) | set(AI_FIELDS) | set(RIGHTS_FIELDS)
    out = {}
    for k, v in update.items():
        if k not in allowed:
            raise MetadataError(f"unknown track metadata field {k!r}")
        if k == "isrc" and v:
            v = normalize_isrc(v)
        if k in ("ai_generated_vocals", "ai_generated_music", "ai_generated_lyrics", "downloaded_while_on_paid_plan",
                 "user_confirms_commercial_rights", "samples_or_third_party_material",
                 "distributor_ai_disclosure_completed", "explicit") and v not in (True, False, None, UNKNOWN):
            raise MetadataError(f"{k} must be true/false/UNKNOWN")
        out[k] = v
    return out


def clean_album_meta(update: dict) -> dict:
    out = {}
    for k, v in update.items():
        if k not in ALBUM_FIELDS:
            raise MetadataError(f"unknown album metadata field {k!r}")
        if k == "upc" and v:
            v = validate_upc(v)
        out[k] = v
    return out


def track_record(track: dict, position: int) -> dict:
    m = track.get("metadata", {}).get("release", {})
    text = (track.get("text") or {}).get("parsed") or {}
    rec = {"track_number": position, "source_title_from": track["metadata"].get("title_source", "filename")}
    for f in TRACK_FIELDS:
        rec[f] = m.get(f, UNKNOWN if f not in ("version", "isrc", "lyrics", "credits_note") else "")
    if rec["title"] in (UNKNOWN, "", None):
        rec["title"] = track["title"]  # file/tag title (not prompt-derived)
    if not rec["lyrics"] and text.get("lyrics"):
        rec["lyrics_candidate_from_text_file"] = True  # shown to user; not auto-adopted
    for f in AI_FIELDS:
        rec[f] = m.get(f, UNKNOWN)
    if rec["ai_tool"] == UNKNOWN and track.get("text"):
        rec["ai_tool_hint"] = "Suno (folder workflow) - confirm"
    if rec["ai_model_version"] == UNKNOWN and text.get("model_version"):
        rec["ai_model_version_hint_from_text"] = text["model_version"]
    for f in RIGHTS_FIELDS:
        rec[f] = m.get(f, UNKNOWN)
    return rec


def unresolved(rec: dict) -> list[str]:
    issues = []
    for f in REQUIRED_TRACK:
        if rec.get(f) in (None, "", UNKNOWN):
            issues.append(f"missing required field {f}")
    for f in ("ai_generated_vocals", "ai_generated_music", "ai_generated_lyrics"):
        if rec.get(f) in (None, UNKNOWN):
            issues.append(f"AI disclosure unresolved: {f}")
    for f in ("downloaded_while_on_paid_plan", "user_confirms_commercial_rights"):
        if rec.get(f) is not True:
            issues.append(f"rights unresolved: {f}={rec.get(f)}")
    if rec.get("songwriters") in (None, "", UNKNOWN):
        issues.append("songwriter credits not entered (never inferred)")
    return issues
