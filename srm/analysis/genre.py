"""Genre/style estimation.

Baseline: a license-safe, transparent heuristic over measured features (tempo, low-end
share, dynamics, brightness). It outputs a *ranked list of hypotheses with LOW confidence*
and the evidence used - never a fact. Prompt keywords are compared but kept separate.

Optional adapter: Essentia/Discogs models are AGPL/CC-BY-NC-SA licensed and are NOT bundled.
``essentia_capability()`` only reports availability; see docs/DSP_DECISIONS.md.
"""

from __future__ import annotations

import importlib.util

STYLES = {
    "liquid_dnb": "Liquid drum & bass",
    "ambient_jazz": "Ambient / jazz (dynamic, sparse)",
    "acoustic_ballad": "Acoustic / ballad",
    "pop_electronic": "Pop / electronic",
    "hybrid_ethnic": "Hybrid / ethnic",
    "rock": "Rock / band",
    "hiphop": "Hip-hop / trap",
}

PROMPT_KEYWORDS = {
    "liquid_dnb": ["drum and bass", "drum & bass", "dnb", "d&b", "liquid", "jungle", "neurofunk", "драм"],
    "ambient_jazz": ["ambient", "jazz", "chill", "lounge", "downtempo", "эмбиент", "джаз"],
    "acoustic_ballad": ["acoustic", "ballad", "folk", "piano ballad", "singer-songwriter", "акуст", "баллад"],
    "pop_electronic": ["pop", "edm", "house", "synth", "electro", "dance", "techno", "trance", "поп"],
    "hybrid_ethnic": ["ethnic", "world", "oriental", "folk fusion", "duduk", "balalaika", "этно", "народ"],
    "rock": ["rock", "metal", "punk", "grunge", "guitar", "рок", "метал"],
    "hiphop": ["hip hop", "hip-hop", "rap", "trap", "boom bap", "drill", "рэп", "хип"],
}


def essentia_capability() -> dict:
    present = importlib.util.find_spec("essentia") is not None
    return {
        "name": "essentia_discogs_classifier",
        "status": "UNAVAILABLE" if not present else "INSTALLED_NOT_ENABLED",
        "reason": ("Not installed. Essentia is AGPL-3.0 and Discogs-EffNet weights are CC BY-NC-SA; "
                   "not bundled. Heuristic fallback is used.") if not present else
                  "Library present but adapter disabled pending license review (AGPL / non-commercial weights).",
    }


def prompt_styles(text: str) -> list[str]:
    t = (text or "").lower()
    return [k for k, words in PROMPT_KEYWORDS.items() if any(w in t for w in words)]


def estimate_style(features: dict, prompt_text: str | None = None) -> dict:
    tempo = features.get("tempo", {}) or {}
    bpm = tempo.get("bpm")
    bands = (features.get("spectral", {}) or {}).get("band_energy_db_rel", {}) or {}
    lra = (features.get("loudness", {}) or {}).get("lra_lu")
    crest = (features.get("dynamics", {}) or {}).get("median_short_window_crest_db")
    centroid = (features.get("spectral", {}) or {}).get("spectral_centroid_hz")
    bass = bands.get("bass_40_120")
    sub = bands.get("sub_20_40")
    cands = [b for b in [bpm] + [c["bpm"] for c in tempo.get("candidates", [])] if b]

    def near(lo, hi):
        return any(lo <= b <= hi for b in cands)

    s = {k: 0.0 for k in STYLES}
    ev: dict[str, list[str]] = {k: [] for k in STYLES}
    if near(160, 180):
        s["liquid_dnb"] += 2.0
        ev["liquid_dnb"].append("tempo candidate 160-180 BPM (or its half-time)")
    if near(80, 100):
        s["hiphop"] += 1.0
        ev["hiphop"].append("tempo candidate 80-100 BPM")
    if near(115, 132):
        s["pop_electronic"] += 1.5
        ev["pop_electronic"].append("tempo candidate 115-132 BPM")
    if near(100, 150):
        s["rock"] += 0.5
    if bass is not None and bass > -6:
        for k in ("liquid_dnb", "hiphop", "pop_electronic"):
            s[k] += 1.0
            ev[k].append(f"strong bass band share ({bass} dB)")
    if sub is not None and sub > -12:
        s["hiphop"] += 0.5
        s["liquid_dnb"] += 0.5
    if lra is not None and lra > 9:
        for k in ("ambient_jazz", "acoustic_ballad"):
            s[k] += 1.5
            ev[k].append(f"wide loudness range (LRA {lra} LU)")
    if crest is not None and crest > 14:
        for k in ("ambient_jazz", "acoustic_ballad", "hybrid_ethnic"):
            s[k] += 1.0
            ev[k].append(f"high short-term crest ({crest} dB)")
    if crest is not None and crest < 9:
        for k in ("pop_electronic", "rock", "hiphop"):
            s[k] += 0.8
            ev[k].append(f"dense/limited dynamics (crest {crest} dB)")
    if centroid is not None and centroid > 3000:
        s["rock"] += 0.5
        s["pop_electronic"] += 0.3
    if bass is not None and bass < -10:
        s["acoustic_ballad"] += 0.8
        s["ambient_jazz"] += 0.5
    s["hybrid_ethnic"] += 0.2  # never zero: hard to detect from simple features
    total = sum(s.values()) or 1.0
    ranked = sorted(s.items(), key=lambda kv: kv[1], reverse=True)
    ranked_out = [{"style": k, "label": STYLES[k], "score": round(v / total, 3), "evidence": ev[k]}
                  for k, v in ranked]
    pstyles = prompt_styles(prompt_text or "")
    agreement = None
    if pstyles:
        top3 = [r["style"] for r in ranked_out[:3]]
        agreement = "CONSISTENT" if any(p in top3 for p in pstyles) else "DIFFERS_FROM_PROMPT"
    mixed = len(ranked_out) > 1 and ranked_out[1]["score"] > 0.75 * ranked_out[0]["score"]
    return {
        "method": "heuristic_features_v1",
        "confidence": "LOW",
        "mixed_or_uncertain": bool(mixed),
        "ranked": ranked_out,
        "prompt_declared_styles": pstyles,
        "prompt_vs_audio": agreement,
        "optional_model": essentia_capability(),
        "disclaimer": ("Heuristic estimate from simple measurements, not a classification fact. "
                       "Prompt text describes intent, not verified audio content. User override wins."),
    }
