"""Prompt / lyrics text files: robust decoding, field extraction and audio association.

Prompt text is *declared intent*, never proof of audio content. Speculative extraction
never overwrites title/album/rights fields chosen by the user.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

MAX_TEXT_BYTES = 2 * 1024 * 1024


def read_text(path: str | Path) -> tuple[str, str, list[str]]:
    """Return (text, encoding, warnings). Never raises on bad encodings."""
    data = Path(path).read_bytes()[:MAX_TEXT_BYTES]
    warnings: list[str] = []
    if Path(path).stat().st_size > MAX_TEXT_BYTES:
        warnings.append(f"truncated to {MAX_TEXT_BYTES} bytes")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "utf-16", warnings
    for enc in ("utf-8-sig", "cp1251"):
        try:
            txt = data.decode(enc)
            if enc == "cp1251":
                warnings.append("not valid UTF-8; decoded as Windows-1251 (verify)")
            return txt, enc, warnings
        except UnicodeDecodeError:
            continue
    warnings.append("undecodable bytes replaced")
    return data.decode("utf-8", "replace"), "utf-8-replace", warnings


_FIELD = re.compile(r"^\s*(title|name|style|style of music|styles|genre|tags|model|version|lyrics|prompt|"
                    r"название|стиль|жанр|текст|модель)\s*[:：]\s*(.*)$", re.I)
_SECTION = re.compile(r"^\s*\[(verse|chorus|bridge|intro|outro|pre-chorus|hook|куплет|припев|бридж)[^\]]*\]", re.I)
_MODEL = re.compile(r"\b(?:suno\s*)?v(?:ersion)?\s*([3-9](?:\.\d)?)\b", re.I)


def detect_language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "UNKNOWN"
    cyr = sum(1 for c in letters if "CYRILLIC" in unicodedata.name(c, ""))
    lat = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    total = len(letters)
    if cyr / total > 0.3 and lat / total > 0.3:
        return "MIXED(ru,en?)"
    if cyr / total > 0.6:
        return "ru?"
    if lat / total > 0.6:
        return "latin-script (en?)"
    return "UNKNOWN"


def parse_prompt_text(text: str) -> dict:
    fields: dict[str, str] = {}
    lyrics_lines: list[str] = []
    in_lyrics = False
    for line in text.splitlines():
        m = _FIELD.match(line)
        if m and not in_lyrics:
            key = m.group(1).lower()
            val = m.group(2).strip()
            key = {"name": "title", "название": "title", "стиль": "style", "style of music": "style",
                   "styles": "style", "жанр": "genre", "tags": "style", "текст": "lyrics", "модель": "model",
                   "version": "model", "prompt": "style"}.get(key, key)
            if key == "lyrics":
                in_lyrics = True
                if val:
                    lyrics_lines.append(val)
            else:
                fields.setdefault(key, val)
            continue
        if _SECTION.match(line):
            in_lyrics = True
        if in_lyrics:
            lyrics_lines.append(line)
    lyrics = "\n".join(lyrics_lines).strip()
    if not lyrics and not fields:
        # Unstructured: treat short text as style prompt, long multi-line as lyrics
        stripped = text.strip()
        if stripped.count("\n") >= 4:
            lyrics = stripped
        else:
            fields["style"] = stripped
    model = fields.get("model")
    if not model:
        m = _MODEL.search(text[:500])
        model = f"v{m.group(1)}" if m else None
    return {
        "declared_title": fields.get("title") or None,
        "style_prompt": fields.get("style") or None,
        "declared_genre": fields.get("genre") or None,
        "model_version": model or None,
        "lyrics": lyrics or None,
        "language_guess": detect_language(lyrics or text),
        "char_count": len(text),
        "note": "Declared intent from text file; not verified against audio.",
    }


_VARIANT = re.compile(r"(\s*[\(\[]\s*\d+\s*[\)\]]|[\s_-]+(v|ver|version|take|alt|mix)\s*\d+|\s+copy)$", re.I)
_LYR_SUFFIX = re.compile(r"[\s_.-]+(lyrics|prompt|text|style|текст|промпт)$", re.I)


def norm_base(name: str) -> str:
    s = unicodedata.normalize("NFKC", Path(name).stem).casefold()
    s = _LYR_SUFFIX.sub("", s)
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_title(name: str) -> str:
    s = norm_base(name)
    prev = None
    while prev != s:
        prev = s
        s = _VARIANT.sub("", s).strip()
    return s


def match_texts(audio_paths: list[Path], text_paths: list[Path]) -> dict[Path, dict]:
    """Associate each audio file with at most one text file.

    Exact normalized basename in the same folder -> MATCHED_EXACT (1.0).
    Exact normalized title (variant suffix removed) in same folder -> MATCHED_VARIANT (0.9).
    Constrained fuzzy (same folder, ratio >= 0.82, clear margin) -> NEEDS_CONFIRMATION.
    Otherwise AMBIGUOUS (several close) or NONE.
    """
    by_dir: dict[Path, list[Path]] = {}
    for t in text_paths:
        by_dir.setdefault(t.parent, []).append(t)
    out: dict[Path, dict] = {}
    for a in audio_paths:
        texts = by_dir.get(a.parent, [])
        if not texts:
            out[a] = {"status": "NONE", "confidence": 0.0, "text": None, "alternatives": []}
            continue
        nb = norm_base(a.name)
        exact = [t for t in texts if norm_base(t.name) == nb]
        if len(exact) == 1:
            out[a] = {"status": "MATCHED_EXACT", "confidence": 1.0, "text": exact[0], "alternatives": []}
            continue
        if len(exact) > 1:
            out[a] = {"status": "AMBIGUOUS", "confidence": 0.5, "text": None,
                      "alternatives": [str(t) for t in exact]}
            continue
        nt = norm_title(a.name)
        var = [t for t in texts if norm_title(t.name) == nt]
        if len(var) == 1:
            out[a] = {"status": "MATCHED_VARIANT", "confidence": 0.9, "text": var[0], "alternatives": []}
            continue
        scored = sorted(((difflib.SequenceMatcher(None, nt, norm_title(t.name)).ratio(), t) for t in texts),
                        key=lambda p: -p[0])
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best[0] >= 0.82 and best[0] - second >= 0.08:
            out[a] = {"status": "NEEDS_CONFIRMATION", "confidence": round(best[0] * 0.8, 3), "text": best[1],
                      "alternatives": [str(t) for s, t in scored[1:3] if s > 0.6]}
        elif best[0] >= 0.82:
            out[a] = {"status": "AMBIGUOUS", "confidence": round(best[0] * 0.5, 3), "text": None,
                      "alternatives": [str(t) for s, t in scored[:3] if s > 0.6]}
        else:
            out[a] = {"status": "NONE", "confidence": 0.0, "text": None, "alternatives": []}
    return out
