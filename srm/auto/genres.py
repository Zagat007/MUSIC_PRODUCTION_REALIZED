"""Genre mastering recipes for the author's own styles + the genre-specific processing stages.

Every recipe is a set of *targets*, not fixed moves: the engine measures each song and only moves it
part of the way towards the recipe (tone dead zone, measured compression, width limited by the stereo
the song already has, loudness limited by how much the limiter may squash).

Recipe keys
  label / short / desc    names for the window
  lufs, squash            target loudness and how many dB of loudness the limiter may take
  hpf, low_f, mono        rumble high-pass, low-shelf frequency of the tone EQ, bass-mono frequency
  tone                    reference tonal balance (dB per octave vs 500-2000 Hz):
                          low 40-120, mud 200-500, presence 2-5 k, air 8-16 k
  mb                      3-band compression {band: (ratio, mean gain reduction dB)} or None per band
                          bands: low < 120 Hz < mid < 5 kHz < high
  glue                    full-band bus compression (ratio, peak gain reduction dB) or None
  sat                     warm parallel saturation of the 120 Hz - 5 kHz band (0 = off, 0.3 = rich)
  punch                   transient emphasis in dB at the attacks (0 = off)
  width                   stereo width of 300 Hz - 8 kHz (1.0 = as is), never past a safe side/mid ratio
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

from srm.dsp.chain import _block_gain_smooth, stage_compressor

GENRES: dict[str, dict] = {
    "liquid_dnb": {
        "label": "Liquid Drum & Bass", "short": "Liquid DnB",
        "desc": "Мелодичный атмосферный DnB, 170–177 BPM. Плотный саб, быстрый панч ударных, чистый воздушный верх.",
        "lufs": -8.5, "squash": 1.6, "hpf": 24, "low_f": 70, "mono": 130,
        "tone": {"low": 11.0, "mud": 1.0, "presence": -2.5, "air": -4.0},
        "mb": {"low": (3.0, 2.0), "mid": (1.8, 1.0), "high": (2.0, 1.0)},
        "glue": (1.6, 1.2), "sat": 0.10, "punch": 2.0, "width": 1.12},
    "noir_jazz_dnb": {
        "label": "Noir Romantic Jazz × Liquid DnB", "short": "Noir Jazz × DnB",
        "desc": "Ночной романтический джаз на энергичном ритме. Тёплая середина (саксофон, рояль), "
                "собранный бас, ударные с панчем, но без перекомпрессии.",
        "lufs": -9.5, "squash": 1.3, "hpf": 26, "low_f": 75, "mono": 120,
        "tone": {"low": 10.5, "mud": 2.0, "presence": -3.0, "air": -4.5},
        "mb": {"low": (2.5, 1.5), "mid": (1.5, 0.8), "high": (1.6, 0.6)},
        "glue": (1.5, 1.0), "sat": 0.22, "punch": 1.5, "width": 1.08},
    "dark_jazz": {
        "label": "Dark Jazz / Cinematic Noir Jazz", "short": "Dark Jazz",
        "desc": "Мрачный чувственный джаз. Живая динамика, тёплая ламповая окраска, мягкий тёмный верх, "
                "громкость как у джазовых релизов.",
        "lufs": -12.5, "squash": 0.6, "hpf": 28, "low_f": 90, "mono": 90,
        "tone": {"low": 8.5, "mud": 2.5, "presence": -3.5, "air": -6.0},
        "mb": {"low": (1.8, 1.0), "mid": None, "high": (1.5, 0.5)},
        "glue": (1.3, 0.7), "sat": 0.28, "punch": 0.0, "width": 1.05},
    "romani_dnb": {
        "label": "Romani Folk × Liquid DnB", "short": "Romani × DnB",
        "desc": "Цыганские романсы, скрипка, гитара и драм-н-бейс. Скрипка без резкости, живая гитара, "
                "плотный ритм.",
        "lufs": -9.5, "squash": 1.3, "hpf": 26, "low_f": 75, "mono": 120,
        "tone": {"low": 10.0, "mud": 1.5, "presence": -2.0, "air": -4.5},
        "mb": {"low": (2.5, 1.5), "mid": (1.6, 0.8), "high": (1.8, 1.0)},
        "glue": (1.5, 1.0), "sat": 0.15, "punch": 1.6, "width": 1.08},
    "triphop_rnb": {
        "label": "Cinematic Trip-Hop / Dark Alternative R&B", "short": "Trip-Hop / Dark R&B",
        "desc": "Глубокий бас, медленный грув, низкий мужской вокал. Много низа, тёплая плотная середина, "
                "мягкий верх.",
        "lufs": -10.0, "squash": 1.2, "hpf": 22, "low_f": 60, "mono": 110,
        "tone": {"low": 12.5, "mud": 2.5, "presence": -3.0, "air": -5.5},
        "mb": {"low": (2.2, 1.5), "mid": (1.6, 1.0), "high": (1.5, 0.6)},
        "glue": (1.8, 1.4), "sat": 0.30, "punch": 1.0, "width": 1.10},
    "deep_house": {
        "label": "Melodic Deep House", "short": "Deep House",
        "desc": "Глубокий эмоциональный хаус. Ровный упругий кик и бас, широкие синтезаторы, клубная громкость.",
        "lufs": -8.5, "squash": 1.5, "hpf": 25, "low_f": 65, "mono": 120,
        "tone": {"low": 11.5, "mud": 1.5, "presence": -3.0, "air": -4.5},
        "mb": {"low": (3.0, 2.0), "mid": (1.6, 0.8), "high": (1.6, 0.8)},
        "glue": (2.0, 1.5), "sat": 0.12, "punch": 1.5, "width": 1.12},
    "dark_trance": {
        "label": "Dark Trance / Deep Ritual Trance", "short": "Dark Trance",
        "desc": "Гипнотическая электроника. Плотный ровный низ, широкое пространство, самая громкая подача.",
        "lufs": -8.0, "squash": 1.8, "hpf": 25, "low_f": 65, "mono": 130,
        "tone": {"low": 11.5, "mud": 1.0, "presence": -2.5, "air": -4.0},
        "mb": {"low": (3.0, 2.2), "mid": (1.8, 1.0), "high": (1.8, 1.0)},
        "glue": (2.0, 1.6), "sat": 0.15, "punch": 1.2, "width": 1.18},
    "cinematic_dnb": {
        "label": "Dark Cinematic Melodic DnB", "short": "Cinematic DnB",
        "desc": "Драматичный DnB с оркестром. Мощные удары, широкий оркестр, бас под контролем.",
        "lufs": -9.0, "squash": 1.4, "hpf": 24, "low_f": 70, "mono": 130,
        "tone": {"low": 11.0, "mud": 1.5, "presence": -2.5, "air": -4.5},
        "mb": {"low": (2.8, 1.8), "mid": (1.6, 0.8), "high": (1.8, 0.8)},
        "glue": (1.6, 1.2), "sat": 0.10, "punch": 1.8, "width": 1.15},
    "ethnic_house": {
        "label": "Ethnic Deep House / Mongolian Folk Fusion", "short": "Ethnic Deep House",
        "desc": "Горловое пение, моринхур, этнические инструменты на хаус-ритме. Бережно к обертонам голоса, "
                "плотный кик, тёплая середина.",
        "lufs": -9.0, "squash": 1.4, "hpf": 25, "low_f": 65, "mono": 120,
        "tone": {"low": 11.0, "mud": 2.0, "presence": -2.5, "air": -5.0},
        "mb": {"low": (2.8, 1.8), "mid": (1.5, 0.6), "high": (1.6, 0.8)},
        "glue": (1.8, 1.2), "sat": 0.15, "punch": 1.3, "width": 1.10},
    "whale_jazz": {
        "label": "Experimental Cinematic Jazz / Whale Jazz", "short": "Whale Jazz",
        "desc": "Экспериментальный атмосферный джаз. Максимум динамики и простора, глубокий низ не режется, "
                "самая широкая стереокартина.",
        "lufs": -14.0, "squash": 0.4, "hpf": 20, "low_f": 90, "mono": 80,
        "tone": {"low": 8.5, "mud": 2.0, "presence": -3.5, "air": -5.0},
        "mb": {"low": (1.5, 0.6), "mid": None, "high": None},
        "glue": None, "sat": 0.18, "punch": 0.0, "width": 1.20},
}

GENERIC = {
    "label": "Универсальный", "short": "Универсальный",
    "desc": "Нейтральный мастеринг, если песня не подходит ни под один жанр.",
    "lufs": -10.0, "squash": 1.2, "hpf": 28, "low_f": 90, "mono": 110,
    "tone": {"low": 10.0, "mud": 2.0, "presence": -2.5, "air": -5.0},
    "mb": None, "glue": (1.5, 1.2), "sat": 0.08, "punch": 0.8, "width": 1.05}

# Audio heuristics (srm.analysis.genre) know broader families; map them to the closest genre.
FAMILY_TO_GENRE = {"liquid_dnb": "liquid_dnb", "ambient_jazz": "dark_jazz", "hybrid_ethnic": "ethnic_house",
                   "pop_electronic": "deep_house", "hiphop": "triphop_rnb"}


def genre_from_prompt(text: str) -> str | None:
    """The author's genre named in the Suno style text / concept notes, or None."""
    t = (text or "").casefold()
    if not t:
        return None

    def has(*words: str) -> bool:
        return any(w in t for w in words)

    dnb = has("drum and bass", "drum & bass", "drum'n'bass", "drum n bass", "dnb", "d&b", "драм-н-бейс",
              "драм н бейс", "драмнбейс", "liquid")
    jazz = has("jazz", "джаз")
    if has("whale", "китов", "кит "):
        return "whale_jazz"
    if has("mongol", "монгол", "throat sing", "горлов", "morin", "моринхур", "khoomei", "хоомей"):
        return "ethnic_house"
    if has("romani", "gypsy", "цыган"):
        return "romani_dnb"
    if has("trip-hop", "trip hop", "triphop", "трип-хоп", "трип хоп", "r&b", "rnb"):
        return "triphop_rnb"
    if has("trance", " транс", "транс "):
        return "dark_trance"
    if has("deep house", "melodic house", "дип-хаус", "дип хаус"):
        return "deep_house"
    if dnb:
        if jazz or has("noir", "нуар"):
            return "noir_jazz_dnb"
        if has("cinematic", "orchestr", "оркестр", "кинематограф"):
            return "cinematic_dnb"
        return "liquid_dnb"
    if jazz or has("noir", "нуар"):
        return "dark_jazz"
    return None


# ------------------------------------------------------------------ filters
def _lp(x: np.ndarray, f: float, fs: int) -> np.ndarray:
    sos = butter(2, f, btype="lowpass", fs=fs, output="sos")
    return sosfilt(sos, sosfilt(sos, x, axis=0), axis=0)  # LR4


def _hp(x: np.ndarray, f: float, fs: int) -> np.ndarray:
    sos = butter(2, f, btype="highpass", fs=fs, output="sos")
    return sosfilt(sos, sosfilt(sos, x, axis=0), axis=0)


def split3(x: np.ndarray, fs: int, f1: float = 120.0, f2: float = 5000.0):
    """Three bands that add back up to exactly the input (mid is the remainder)."""
    low = _lp(x, f1, fs)
    rest = x - low
    high = _hp(rest, f2, fs)
    return low, rest - high, high


# ------------------------------------------------------------------ stages
BAND_NAMES = {"low": "бас", "mid": "середина", "high": "верх"}
BAND_TIMES = {"low": (12.0, 140.0), "mid": (20.0, 180.0), "high": (5.0, 80.0)}  # attack, release ms


def _rms_db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(x ** 2) + 1e-20))


def multiband(x: np.ndarray, fs: int, prof: dict) -> tuple[np.ndarray, list[str]]:
    """Each band compressed to a measured average gain reduction (thresholds found per song)."""
    spec = prof.get("mb")
    if not spec or not any(spec.values()):
        return x, []
    bands = dict(zip(("low", "mid", "high"), split3(x, fs)))
    done = []
    for name, band in bands.items():
        if not spec.get(name) or _rms_db(band) < -70:
            continue
        ratio, target = spec[name]
        att, rel = BAND_TIMES[name]
        thr = float(np.clip(_rms_db(band) + 9.0, -60.0, -1.0))
        y, info = band, {"mean_gr_db": 0.0}
        for _ in range(4):
            y, info = stage_compressor(band, fs, {"threshold_db": thr, "ratio": ratio, "attack_ms": att,
                                                  "release_ms": rel, "knee_db": 6.0})
            err = info["mean_gr_db"] - target
            if abs(err) < 0.25:
                break
            thr = float(np.clip(thr + err * 2.5, -60.0, -1.0))
        if info["mean_gr_db"] >= 0.2:
            # make-up: give back the average reduction so the band balance stays where the tone EQ put it
            bands[name] = y * 10 ** (info["mean_gr_db"] / 20)
            done.append(f"{BAND_NAMES[name]} {ratio}:1 −{info['mean_gr_db']:.1f} дБ")
    if not done:
        return x, []
    return bands["low"] + bands["mid"] + bands["high"], ["Многополосная компрессия: " + ", ".join(done)]


def saturate(x: np.ndarray, fs: int, amount: float) -> tuple[np.ndarray, list[str]]:
    """Warm parallel saturation of 120 Hz - 5 kHz: soft asymmetric curve (2nd + 3rd harmonics),
    level independent (the drive follows the song's own peaks), unity small-signal gain."""
    if amount <= 0:
        return x, []
    low, mid, high = split3(x, fs)
    s = float(np.percentile(np.abs(mid), 99.9)) + 1e-9
    d, b = 1.6, 0.15  # drive, bias (asymmetry -> even harmonics)
    u = mid / s
    sat = (np.tanh(d * (u + b)) - np.tanh(d * b)) / (d * (1 - np.tanh(d * b) ** 2))
    sat -= sat.mean(axis=0)  # the bias would add DC
    mid2 = mid + amount * (sat * s - mid)
    mid2 *= np.sqrt(np.mean(mid ** 2) / (np.mean(mid2 ** 2) + 1e-20))  # same loudness as before
    word = "лёгкая" if amount < 0.15 else "тёплая" if amount < 0.25 else "насыщенная"
    return low + mid2 + high, [f"Аналоговая окраска середины: {word} ({amount * 100:.0f}%)"]


def punch(x: np.ndarray, fs: int, amount_db: float) -> tuple[np.ndarray, list[str]]:
    """Transient shaper: lifts the attacks (fast envelope above the slow one) by up to ``amount_db``."""
    if amount_db <= 0:
        return x, []
    block = 32
    n = x.shape[0]
    nb = int(np.ceil(n / block))
    pad = nb * block - n
    xx = np.pad(x, ((0, pad), (0, 0))) if pad else x
    lvl = 10 * np.log10((xx.reshape(nb, block, -1) ** 2).mean(axis=(1, 2)) + 1e-12)
    c = lambda ms: np.exp(-block / (ms / 1000 * fs))  # noqa: E731
    fast = -_block_gain_smooth(-lvl, c(0.5), c(40.0))
    slow = -_block_gain_smooth(-lvl, c(25.0), c(150.0))
    diff = np.clip(fast - slow, 0.0, 9.0)
    diff[lvl < lvl.max() - 45] = 0.0  # leave silence and fades alone
    g_db = amount_db * diff / 9.0
    g_db = -_block_gain_smooth(-g_db, c(0.3), c(30.0))  # smooth: no clicks
    centers = (np.arange(nb) + 0.5) * block
    g = 10 ** (np.interp(np.arange(n), centers, g_db) / 20)
    y = x * g[:, None]
    y *= np.sqrt(np.mean(x ** 2) / (np.mean(y ** 2) + 1e-20))
    share = float((diff > 4.5).mean() * 100)  # clear attacks only
    return y, [f"Панч ударных: атаки подняты до +{amount_db:.1f} дБ (чёткие атаки — {share:.0f}% времени)"]


MAX_SIDE_RATIO = 0.55  # side/mid energy ratio above this starts to sound phasey and collapses in mono


def widen(x: np.ndarray, fs: int, width: float) -> tuple[np.ndarray, list[str]]:
    """Stereo width of 300 Hz - 8 kHz only (bass stays mono, highs stay clean after the cleanup),
    limited so the song never gets wider than a safe side/mid ratio."""
    if x.shape[1] != 2 or abs(width - 1.0) < 0.01:
        return x, []
    mid = 0.5 * (x[:, 0] + x[:, 1])
    side = 0.5 * (x[:, 0] - x[:, 1])
    band = _lp(_hp(side, 300.0, fs), 8000.0, fs)
    mband = _lp(_hp(mid, 300.0, fs), 8000.0, fs)
    sm, ss = np.sqrt(np.mean(mband ** 2)) + 1e-12, np.sqrt(np.mean(band ** 2))
    ratio = ss / sm
    if ratio < 1e-3:
        return x, ["Песня почти моно — стерео не расширялось"]
    if width > 1.0:
        width = min(width, max(1.0, MAX_SIDE_RATIO / ratio))
        if width < 1.01:
            return x, ["Стерео уже широкое — расширение не нужно"]
    side = side + (width - 1.0) * band
    return np.stack([mid + side, mid - side], axis=1), [
        f"Стереопанорама 300 Гц–8 кГц: {'шире' if width > 1 else 'уже'} на {abs(width - 1) * 100:.0f}%"]
