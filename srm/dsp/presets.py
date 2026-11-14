"""Starting profiles. Suggestions to be tuned by measurement and listening - not tonal recipes.

``loudness_range_lufs`` is a *suggested window*, never a forced target; there is no universal
-14 LUFS rule (streaming services normalise playback). ``max_limiter_gr_db`` bounds how much
peak reduction SAFE_MASTER may use to reach a level; if more would be required, the level is
lowered instead of squashing the music.
"""

PRESETS: dict[str, dict] = {
    "generic": {
        "label": "Generic / unknown style",
        "loudness_range_lufs": [-14.0, -10.0], "max_limiter_gr_db": 2.0, "allow_compression": False,
        "protect": ["overall dynamics"], "notes": "Neutral starting point; listen before increasing level.",
    },
    "liquid_dnb": {
        "label": "Liquid drum & bass",
        "loudness_range_lufs": [-11.0, -8.5], "max_limiter_gr_db": 2.5, "allow_compression": False,
        "protect": ["kick/sub separation", "snare transients", "vocal presence"],
        "notes": "Do not high-pass the sub; avoid limiter pumping on the snare. Check 87/174 BPM reading.",
    },
    "ambient_jazz": {
        "label": "Ambient / jazz",
        "loudness_range_lufs": [-18.0, -13.0], "max_limiter_gr_db": 1.0, "allow_compression": False,
        "protect": ["natural dynamics", "imaging", "silence and tails"],
        "notes": "Preserve quiet passages; no compression by default; never gate reverb tails.",
    },
    "acoustic_ballad": {
        "label": "Acoustic / ballad",
        "loudness_range_lufs": [-16.0, -11.0], "max_limiter_gr_db": 1.5, "allow_compression": True,
        "protect": ["voice", "natural attack"],
        "notes": "Gentle (<=1.5:1) compression only if the vocal level swings strongly.",
    },
    "pop_electronic": {
        "label": "Pop / electronic",
        "loudness_range_lufs": [-11.0, -8.0], "max_limiter_gr_db": 3.0, "allow_compression": True,
        "protect": ["punch", "low-end clarity"],
        "notes": "Energy without clipping: prefer slight level reduction over heavy limiting.",
    },
    "hybrid_ethnic": {
        "label": "Hybrid / ethnic",
        "loudness_range_lufs": [-15.0, -11.0], "max_limiter_gr_db": 1.5, "allow_compression": False,
        "protect": ["acoustic instruments", "microdynamics"],
        "notes": "Avoid broadband treatments that damage acoustic instruments.",
    },
    "rock": {
        "label": "Rock / band",
        "loudness_range_lufs": [-11.0, -8.5], "max_limiter_gr_db": 2.5, "allow_compression": True,
        "protect": ["drum transients", "guitar texture"], "notes": "",
    },
    "hiphop": {
        "label": "Hip-hop / trap",
        "loudness_range_lufs": [-11.0, -8.0], "max_limiter_gr_db": 2.5, "allow_compression": False,
        "protect": ["808/sub", "vocal intelligibility"], "notes": "Sub-bass energy is usually intentional.",
    },
}

# Reference band-balance windows (dB relative to total, from measurement module) used ONLY to
# decide whether a deviation is large enough to *suggest* a bounded EQ move.
BAND_REFERENCE = {
    "highmid_2k_6k": (-22.0, -8.0),
    "air_12k_20k": (-45.0, -18.0),
    "bass_40_120": (-14.0, -2.0),
}
