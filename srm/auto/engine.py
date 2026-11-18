"""One-click release: AI-artifact cleanup + style-aware smart mastering + release export.

Pipeline for every track (the source file is only read, never changed):

1. decode to 44.1 kHz stereo float
2. analyse (loudness, spectrum, tempo, defects) and detect the style from the audio plus any
   prompt text found next to the song (Suno "style of music" text, КОНЦЕПЦИЯ.txt, ...)
3. AI-artifact cleanup
   * isolated clicks repaired by interpolation (existing, regression-checked repair)
   * rumble / DC removed (style-dependent high-pass)
   * static "metallic" resonances: narrow peaks that stay in the long-term spectrum the whole song
   * dynamic harshness (2.5-6 kHz) and sibilance (5.5-10 kHz) tamed only in the moments they jump out
   * "swirly" high-frequency stereo noise: excess side-channel energy above ~9 kHz
4. smart mastering by the author's genre recipe (srm.auto.genres; chosen by a button or detected)
   * tonal balance moved part of the way towards the style's reference curve (dead zone, bounded)
   * 3-band compression and glue compression, each to a measured amount of gain reduction
   * warm parallel saturation of the mids, transient punch, stereo width limited by the song's own width
   * bass made mono below the style's frequency
   * level to the style's target loudness through a true-peak limiter; if the limiter would
     squash the song more than the style allows, the level is lowered instead
5. export WAV 44.1 kHz / 24-bit with TPDF dither, true peak <= -1 dBTP, and a plain report
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt, welch

from srm.analysis.analyze import analyze_audio
from srm.analysis.genre import STYLES, prompt_styles
from srm.analysis.loudness import integrated_lufs
from srm.analysis.truepeak import db, peak_envelope
from srm.auto.genres import FAMILY_TO_GENRE, GENERIC, GENRES, genre_from_prompt, multiband, punch, saturate, widen
from srm.dsp.chain import _block_gain_smooth, rbj_sos, stage_compressor, stage_stereo, true_peak_limit
from srm.dsp.quantize import write_float
from srm.dsp.repair import apply_repair_plan, plan_from_candidates
from srm.ingest.decode import decode
from srm.ingest.probe import probe
from srm.ingest.textmatch import match_texts, read_text

AUDIO_EXT = {".wav", ".wave", ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".aif", ".aiff"}
RATE = 44100
BITS = 24
CEILING_DBTP = -1.0
OUT_DIR = "РЕЛИЗ"

Progress = Callable[[float, str], None]

# Tonal reference: energy per octave of each region relative to the 500-2000 Hz region (dB),
# calibrated on real Suno releases (generic = median of a 13-track album) plus style offsets.
# low 40-120 Hz, mud 200-500 Hz, presence 2-5 kHz, air 8-16 kHz.
# The author's 10 genres (srm.auto.genres, Whale Jazz always last) + a neutral fallback first.
PROFILES: dict[str, dict] = {"generic": GENERIC, **GENRES}
STYLE_CHOICES = ["auto"] + list(PROFILES)
CLEANUP = {"soft": 0.6, "normal": 1.0, "strong": 1.4}
CLEANUP_LABELS = {"soft": "Мягкая", "normal": "Обычная", "strong": "Сильная"}
REGIONS = {"low": (40, 120), "mud": (200, 500), "ref": (500, 2000), "presence": (2000, 5000), "air": (8000, 16000)}


def style_label(key: str) -> str:
    return PROFILES.get(key, {}).get("label", key)


# ------------------------------------------------------------------ inputs
def list_audio(paths: list[str | Path]) -> list[Path]:
    """Audio files from files and folders (folders: only their own files, the РЕЛИЗ folder is skipped)."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted((f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXT),
                          key=lambda f: f.name.casefold())
        elif p.is_file() and p.suffix.lower() in AUDIO_EXT:
            out.append(p)
    seen, uniq = set(), []
    for f in out:
        if f.parent.name != OUT_DIR and f.resolve() not in seen:
            seen.add(f.resolve())
            uniq.append(f)
    return uniq


def prompt_text_for(path: Path) -> str:
    """Prompt / style text next to the song: its own .txt first, then album-wide notes."""
    texts = [t for t in path.parent.glob("*.txt") if t.is_file()]
    if not texts:
        return ""
    parts = []
    try:
        m = match_texts([path], texts).get(path) or {}
        if m.get("text") and m.get("status") in ("MATCHED_EXACT", "MATCHED_VARIANT", "NEEDS_CONFIRMATION"):
            parts.append(read_text(m["text"])[0])
    except Exception:
        pass
    for t in texts:
        n = t.stem.casefold()
        if any(w in n for w in ("концеп", "concept", "стиль", "style", "prompt", "промпт", "жанр", "genre")):
            try:
                parts.append(read_text(t)[0][:20000])
            except Exception:
                pass
    return "\n".join(parts)


# ------------------------------------------------------------------ style
def detect_style(meas: dict, prompt: str) -> dict:
    """Audio heuristics + prompt keywords. Prompt words are strong evidence, audio decides otherwise."""
    ranked = (meas.get("style") or {}).get("ranked") or []
    score = {r["style"]: float(r["score"]) for r in ranked}
    declared = prompt_styles(prompt)
    # count keyword hits so that the dominant declared style wins over passing mentions
    text = (prompt or "").lower()
    from srm.analysis.genre import PROMPT_KEYWORDS

    hits = {k: sum(text.count(w) for w in PROMPT_KEYWORDS[k]) for k in declared}
    for k, h in hits.items():
        score[k] = score.get(k, 0.0) + 0.35 + 0.08 * min(h, 5)
    order = sorted(score.items(), key=lambda kv: -kv[1])
    named = genre_from_prompt(prompt)
    if named:  # the author's own genre named in the Suno style / concept text: trust it
        return {"style": named, "guess": named, "confidence": "ВЫСОКАЯ", "source": "жанр из описания",
                "declared": [named], "guess_label": GENRES[named]["label"]}
    if not order:
        return {"style": "generic", "guess": "generic", "confidence": "НИЗКАЯ", "source": "нет данных",
                "declared": declared, "guess_label": "—"}
    top, s1 = order[0]
    s2 = order[1][1] if len(order) > 1 else 0.0
    if declared and top in declared:
        conf, source = ("ВЫСОКАЯ" if top in [r["style"] for r in ranked[:3]] else "СРЕДНЯЯ"), "звук + описание"
    elif s1 - s2 > 0.08:
        conf, source = "СРЕДНЯЯ", "по звуку"
    else:
        conf, source = "НИЗКАЯ", "по звуку"
    genre = FAMILY_TO_GENRE.get(top)
    style = genre if genre and conf != "НИЗКАЯ" else "generic"
    return {"style": style, "guess": genre or "generic", "confidence": conf, "source": source,
            "declared": declared, "guess_label": PROFILES[genre]["label"] if genre else STYLES.get(top, top)}


# ------------------------------------------------------------------ helpers
def _ms(x: np.ndarray) -> np.ndarray:
    return np.stack([(x[:, 0] + x[:, 1]) * 0.5, (x[:, 0] - x[:, 1]) * 0.5], axis=1)


def _lr(m: np.ndarray) -> np.ndarray:
    return np.stack([m[:, 0] + m[:, 1], m[:, 0] - m[:, 1]], axis=1)


def region_levels(x: np.ndarray, fs: int) -> dict:
    """Energy per octave of each region, dB relative to the 500-2000 Hz region."""
    mono = x.mean(axis=1)
    f, p = welch(mono, fs=fs, nperseg=8192, noverlap=4096)
    out = {}
    for k, (lo, hi) in REGIONS.items():
        sel = (f >= lo) & (f < min(hi, fs / 2))
        out[k] = 10 * np.log10(max(p[sel].sum() * (f[1] - f[0]), 1e-20) / np.log2(hi / lo))
    ref = out.pop("ref")
    return {k: round(float(v - ref), 2) for k, v in out.items()}


# ------------------------------------------------------------------ cleanup (STFT, mid/side)
N_FFT, HOP = 4096, 1024
_WIN = np.sqrt(np.hanning(N_FFT + 1)[:-1])  # sqrt-Hann analysis + synthesis, 75 % overlap
_OLA = (_WIN ** 2).sum() / HOP


def _frames(n: int) -> np.ndarray:
    return np.arange(0, n + N_FFT, HOP)


def _band(freqs: np.ndarray, lo: float, hi: float, edge: float = 0.25) -> np.ndarray:
    """Smooth band shape 0..1 (raised-cosine edges, ``edge`` octaves wide)."""
    lf = np.log2(np.maximum(freqs, 1.0))
    a, b = np.log2(lo), np.log2(hi)
    w = np.clip(np.minimum((lf - (a - edge)) / edge, ((b + edge) - lf) / edge), 0, 1)
    return 0.5 - 0.5 * np.cos(np.pi * w)


def _log_smooth(v: np.ndarray, frac: float) -> np.ndarray:
    """Moving average whose width grows with frequency (``frac`` of the bin index each side)."""
    c = np.concatenate([[0.0], np.cumsum(v)])
    k = np.arange(v.size)
    h = np.maximum(3, (k * frac).astype(int))
    lo, hi = np.maximum(0, k - h), np.minimum(v.size, k + h + 1)
    return (c[hi] - c[lo]) / (hi - lo)


def cleanup(x: np.ndarray, fs: int, strength: float = 1.0, progress: Progress | None = None,
            stats: dict | None = None) -> tuple[np.ndarray, list[str]]:
    """Remove typical AI-generation artifacts. Returns (audio, list of human-readable actions).

    ``stats`` (optional) is filled with the counts of what is fixed as soon as it is known (before
    the audio is rewritten): harsh / sibilance moments, resonances, stereo swirl."""
    n = x.shape[0]
    ms = _ms(x)
    xp = np.pad(ms, ((N_FFT, N_FFT), (0, 0)))
    starts = _frames(n)
    freqs = np.fft.rfftfreq(N_FFT, 1 / fs)
    nb = freqs.size
    bands = {"ref": _band(freqs, 500, 2000), "harsh": _band(freqs, 2500, 6000), "sib": _band(freqs, 5500, 10000)}
    hf = freqs >= 9000
    # ---- pass 1: per-frame band energies + long-term spectra
    E = {k: np.empty(starts.size) for k in bands}
    ltas_m = np.zeros(nb)
    ltas_s = np.zeros(nb)
    CH = 256
    for i in range(0, starts.size, CH):
        idx = starts[i:i + CH, None] + np.arange(N_FFT)
        F = np.fft.rfft(xp[idx] * _WIN[None, :, None], axis=1)
        P = (F.real ** 2 + F.imag ** 2)
        ltas_m += P[:, :, 0].sum(axis=0)
        ltas_s += P[:, :, 1].sum(axis=0)
        for k, w in bands.items():
            E[k][i:i + CH] = P[:, :, 0] @ w
        if progress:
            progress(0.45 * (i + CH) / starts.size, "Ищем артефакты ИИ")
    actions: list[str] = []
    Edb = {k: 10 * np.log10(v + 1e-12) for k, v in E.items()}
    loud = Edb["ref"] > Edb["ref"].max() - 35  # ignore silence / fades

    def dyn_gain(name: str, thr: float, ratio: float, maxcut: float, pct: float) -> np.ndarray:
        rel = Edb[name] - Edb["ref"]
        if loud.sum() < 20:
            return np.zeros(starts.size)
        base = np.percentile(rel[loud], pct)  # typical upper level of this song: only real spikes are cut
        cut = -np.clip((rel - base - thr) * ratio, 0, maxcut * strength)
        cut[~loud] = 0.0
        return _block_gain_smooth(cut, 0.2, 0.85)

    g_harsh = dyn_gain("harsh", 2.0, 0.6, 4.0, 88)
    g_sib = dyn_gain("sib", 2.0, 0.6, 5.0, 90)
    for g, what in ((g_harsh, "Приглушена резкость 2,5–6 кГц"), (g_sib, "Приглушены свист и шипящие 5,5–10 кГц")):
        active = (g < -1.0).mean() * 100
        if active > 0.5:
            actions.append(f"{what} в моментах, где они выпирают ({active:.0f}% времени, до {-g.min():.1f} дБ)")
    # ---- static resonances ("metallic" ringing that stays the whole song)
    lt = 10 * np.log10(ltas_m + 1e-20)
    excess = lt - _log_smooth(lt, 0.06)
    zone = (freqs >= 700) & (freqs <= 10000)
    stat = np.where(zone, -np.clip((excess - 2.5) * 0.8, 0, 3.5 * strength), 0.0)
    stat = np.minimum(0.0, np.convolve(stat, np.ones(5) / 5, mode="same"))
    is_min = (stat[1:-1] < -1.0) & (stat[1:-1] < stat[:-2]) & (stat[1:-1] <= stat[2:])
    f_list = sorted({round(f / 100) / 10 for f in freqs[1:-1][is_min]})
    peaks = len(f_list)
    if peaks:
        actions.append(f"Убраны металлические призвуки — постоянные резонансы ({peaks} шт., "
                       f"{', '.join(f'{f:.1f}' for f in f_list[:5])} кГц), до {-stat.min():.1f} дБ")
    # ---- side-channel HF "swirl"
    side_hf = 10 * np.log10(ltas_s[hf].sum() + 1e-20) - 10 * np.log10(ltas_m[hf].sum() + 1e-20)
    side_cut = float(np.clip((side_hf + 7.0) * 0.8, 0, 4.0 * strength))
    side_shape = -side_cut * _band(freqs, 9000, fs / 2, edge=0.5)
    if side_cut > 0.5:
        actions.append(f"Убрано «шуршание» и плавающий шум в стереоканале на высоких частотах (−{side_cut:.1f} дБ)")
    if stats is not None:
        def runs(g: np.ndarray) -> int:  # separate moments, not frames
            on = (g < -1.0).astype(np.int8)
            return int(np.count_nonzero(np.diff(np.concatenate([[0], on])) == 1))
        stats.update({"harsh": runs(g_harsh), "sibilance": runs(g_sib), "resonances": peaks,
                      "swirl": int(side_cut > 0.5)})
    # ---- pass 2: apply
    g_sib_shape = bands["sib"] / max(bands["sib"].max(), 1e-9)
    g_harsh_shape = bands["harsh"] / max(bands["harsh"].max(), 1e-9)
    out = np.zeros_like(xp)
    for i in range(0, starts.size, CH):
        sl = slice(i, i + CH)
        idx = starts[sl, None] + np.arange(N_FFT)
        F = np.fft.rfft(xp[idx] * _WIN[None, :, None], axis=1)
        gdb = g_harsh[sl, None] * g_harsh_shape[None] + g_sib[sl, None] * g_sib_shape[None] + stat[None]
        gm = 10 ** (gdb / 20)
        F[:, :, 0] *= gm
        F[:, :, 1] *= gm * 10 ** (side_shape / 20)[None]
        y = np.fft.irfft(F, n=N_FFT, axis=1) * _WIN[None, :, None] / _OLA
        for j, s in enumerate(starts[sl]):
            out[s:s + N_FFT] += y[j]
        if progress:
            progress(0.45 + 0.55 * (i + CH) / starts.size, "Убираем артефакты ИИ")
    res = _lr(out[N_FFT:N_FFT + n])
    return res, actions


# ------------------------------------------------------------------ mastering
def tonal_eq(x: np.ndarray, fs: int, prof: dict, lowpass_hz: float | None) -> tuple[np.ndarray, list[str], dict]:
    lv = region_levels(x, fs)
    plan = {"low": ("low_shelf", float(prof.get("low_f", 90)), 0.7), "mud": ("peak", 300.0, 0.8), "presence": ("peak", 3200.0, 0.8),
            "air": ("high_shelf", 10000.0, 0.7)}
    names = {"low": "бас", "mud": "«грязь» 200–500 Гц", "presence": "разборчивость 2–5 кГц", "air": "воздух 8–16 кГц"}
    sos, acts, moves = [], [], {}
    for k, (kind, f0, q) in plan.items():
        dev = prof["tone"][k] - lv[k]  # >0: need more
        if abs(dev) <= 2.0:
            continue
        g = float(np.clip((abs(dev) - 2.0) * 0.5, 0, 2.5)) * np.sign(dev)
        if k == "air" and g > 0 and lowpass_hz and lowpass_hz < 17000:
            continue  # never boost what a lossy encoder already cut off: it would only add noise
        if abs(g) < 0.4:
            continue
        g = round(g, 1)
        sos.append(rbj_sos(kind, f0, fs, g, q))
        moves[k] = g
        acts.append(f"{'Добавлен' if g > 0 else 'Убран'} {names[k]}: {g:+.1f} дБ")
    if sos:
        x = sosfilt(np.concatenate(sos), x, axis=0)
    return x, acts, {"before": lv, "moves": moves}


def glue(x: np.ndarray, fs: int, prof: dict) -> tuple[np.ndarray, list[str]]:
    if not prof["glue"]:
        return x, []
    ratio, target = prof["glue"]
    il = integrated_lufs(x, fs) or -20.0
    thr = float(np.clip(il + 6.0, -40.0, -1.0))
    y, info = x, {}
    for _ in range(5):
        p = {"threshold_db": thr, "ratio": ratio, "attack_ms": 30.0, "release_ms": 200.0, "knee_db": 6.0}
        y, info = stage_compressor(x, fs, p)
        err = info["mean_gr_db"] - target * 0.5  # mean over the whole song is about half of the peak amount
        if abs(err) < 0.2:
            break
        thr = float(np.clip(thr + err * 5.0, -40.0, -1.0))
    if info.get("max_gr_db", 0) < 0.3:
        return x, []
    return y, [f"Мягкая компрессия «склейка» {ratio}:1 (в среднем −{info['mean_gr_db']:.1f} дБ)"]


def loudness_and_limit(x: np.ndarray, fs: int, prof: dict, progress: Progress | None = None
                       ) -> tuple[np.ndarray, list[str], dict]:
    target, budget = prof["lufs"], prof["squash"]
    il = integrated_lufs(x, fs)
    if il is None:
        y, info = true_peak_limit(x, fs, CEILING_DBTP)
        return y, ["Песня почти тихая — громкость не менялась"], {"lufs": None}
    gain = target - il
    lowered = False
    env0 = peak_envelope(x, fs)  # measured once; the limiter input is only this audio times a gain
    out_il = il
    for it in range(6):  # fast search without output verification
        if progress:
            progress(0.15 + it / 8, "Подбираем громкость")
        k = 10 ** (gain / 20)
        y, _ = true_peak_limit(x * k, fs, CEILING_DBTP, env=env0 * k, verify=False)
        out_il = integrated_lufs(y, fs) or target
        squash = (il + gain) - out_il  # loudness the limiter took away
        if squash > budget + 0.15:
            gain -= (squash - budget) * 1.2
            lowered = True
            continue
        err = target - out_il
        if abs(err) < 0.3 or (lowered and err > 0):
            break
        gain += err
    if progress:
        progress(0.9, "Финальный лимитер с проверкой пиков")
    k = 10 ** (gain / 20)
    y, info = true_peak_limit(x * k, fs, CEILING_DBTP, env=env0 * k)
    out_il = integrated_lufs(y, fs) or out_il
    acts = [f"Громкость {out_il:.1f} LUFS (цель стиля {target:.1f}), пики не выше {CEILING_DBTP:.0f} dBTP"]
    if lowered and out_il < target - 0.5:
        acts.append("Громкость оставлена ниже цели, чтобы не задавить удары и динамику")
    return y, acts, {"lufs": out_il, "limiter": info}


# ------------------------------------------------------------------ one track
FORMATS = {"wav": "WAV 24 бит (для релиза)", "mp3": "MP3 320 кбит/с", "both": "WAV + MP3"}
FIX_NAMES = {"clicks": "щелчки", "harsh": "резкость", "sibilance": "шипящие", "resonances": "металлич. призвуки",
             "swirl": "шум в стерео"}


def fixes_total(f: dict) -> int:
    return int(sum(v for k, v in f.items() if k in FIX_NAMES))


def fixes_text(f: dict) -> str:
    parts = [f"{FIX_NAMES[k]} {f[k]}" for k in FIX_NAMES if f.get(k)]
    return ", ".join(parts) if parts else "дефектов не найдено"


def _out_path(src: Path, out_dir: Path, exts: tuple[str, ...] = (".wav",)) -> Path:
    """Path without extension-clash: never overwrites any of the files of this release."""
    stem, i = src.stem, 2
    while any((out_dir / f"{stem}{e}").exists() for e in exts):
        stem = f"{src.stem} ({i})"
        i += 1
    return out_dir / f"{stem}{exts[0]}"


def write_mp3(wav: Path, mp3: Path, title: str) -> None:
    from srm.core.tools import ffmpeg_input, get_tools, run

    t = get_tools()
    if not t.has_libmp3lame:
        raise RuntimeError("В установленном FFmpeg нет MP3-кодировщика (libmp3lame) — выберите WAV")
    run([t.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", ffmpeg_input(wav), "-c:a", "libmp3lame", "-b:a", "320k",
         "-map_metadata", "-1", "-metadata", f"title={title}", "-id3v2_version", "3", ffmpeg_input(mp3)],
        check=True)


def _load(src: Path):
    pr = probe(src)
    if not pr.ok:
        raise RuntimeError(f"Файл не читается как аудио: {pr.error or 'неизвестный формат'}")
    dec = decode(src, sample_rate=pr.sample_rate, channels=2, target_rate=RATE)
    return pr, dec.audio.astype(np.float64), dec.sample_rate


def analyze_track(src: str | Path, progress: Progress | None = None) -> dict:
    """Decode + measure + style guess (no processing). The result can be passed to ``make_release``."""
    src = Path(src)
    if progress:
        progress(0.0, "Читаем файл")
    pr, x, fs = _load(src)
    prompt = prompt_text_for(src)
    stop = threading.Event()
    if progress:
        progress(0.12, "Слушаем песню")
        expected = 2.0 + 0.07 * x.shape[0] / fs  # measured: about 18 s for a 4-minute song
        t0 = time.monotonic()

        def tick():  # the measurement is one long call: show its expected progress smoothly
            while not stop.wait(0.25):
                progress(0.12 + 0.83 * min(0.97, (time.monotonic() - t0) / expected), "Слушаем песню")
        threading.Thread(target=tick, daemon=True).start()
    try:
        meas = analyze_audio(x, fs, prompt_text=prompt, with_defects=True, timelines=False)
    finally:
        stop.set()
    det = detect_style(meas, prompt)
    if progress:
        progress(1.0, "Стиль определён")
    return {"path": str(src), "meas": meas, "det": det, "lossy": bool(pr.is_lossy),
            "duration_s": meas.get("duration_s"), "lufs": (meas.get("loudness") or {}).get("integrated_lufs")}


def album_style(analyses: list[dict]) -> dict:
    """One style for songs of one album, so they sound like one record (same loudness, same tone)."""
    ok = [a for a in analyses if a and a.get("det")]
    if not ok:
        return {"style": "generic", "confidence": "НИЗКАЯ", "votes": {}}
    sure = [a["det"]["style"] for a in ok if a["det"]["confidence"] != "НИЗКАЯ" and a["det"]["declared"]]
    votes: dict[str, int] = {}
    for a in ok:
        votes[a["det"]["guess"]] = votes.get(a["det"]["guess"], 0) + 1
    if sure:
        top = max(set(sure), key=sure.count)
        return {"style": top, "confidence": "ВЫСОКАЯ", "votes": votes, "source": "описание + звук"}
    top, n = max(votes.items(), key=lambda kv: kv[1])
    if top in PROFILES and n * 2 >= len(ok):
        return {"style": top, "confidence": "СРЕДНЯЯ", "votes": votes, "source": f"{n} из {len(ok)} песен"}
    return {"style": "generic", "confidence": "НИЗКАЯ", "votes": votes, "source": "стили песен разные"}


def make_release(src: str | Path, out_dir: str | Path | None = None, *, style: str = "auto", cleanup_level: str = "normal",
                 progress: Progress | None = None, analysis: dict | None = None, auto_style: str | None = None,
                 fmt: str = "wav", on_fixes: Callable[[dict], None] | None = None) -> dict:
    """Process one song into a release-ready WAV. Returns a report dict.

    ``fmt``: 'wav' (44.1 kHz / 24-bit), 'mp3' (320 kbit/s) or 'both'. ``on_fixes`` gets the running
    counts of the fixed defects (clicks first, then the AI-artifact counts) for a live display.
    ``style``: 'auto' or a PROFILES key. With 'auto', ``auto_style`` (e.g. the album style) is used when
    given, otherwise the song's own guess.
    """
    t0 = time.monotonic()
    src = Path(src)
    out_dir = Path(out_dir) if out_dir else src.parent / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    def step(a: float, b: float):
        return (lambda f, text: progress(a + (b - a) * max(0.0, min(1.0, f)), text)) if progress else None

    def say(f: float, text: str):
        if progress:
            progress(f, text)

    say(0.0, "Читаем файл")
    pr, x, fs = _load(src)
    if analysis is None:
        say(0.06, "Анализируем звук и стиль")
        prompt = prompt_text_for(src)
        meas = analyze_audio(x, fs, prompt_text=prompt, with_defects=True, timelines=False)
        det = detect_style(meas, prompt)
    else:
        meas, det = analysis["meas"], analysis["det"]
    chosen = (auto_style or det["style"]) if style == "auto" else style
    prof = PROFILES[chosen]
    actions: list[str] = []
    lowpass = (meas.get("spectral") or {}).get("possible_encoder_lowpass_hz")
    before = {"lufs": (meas.get("loudness") or {}).get("integrated_lufs"),
              "true_peak": (meas.get("peaks") or {}).get("true_peak_dbtp")}

    fixes: dict = {"clicks": 0}
    # clicks
    say(0.14, "Чиним щелчки")
    cands = [c for c in (meas.get("defects") or {}).get("candidates", []) if c["type"] == "click_pop"]
    if cands:
        plan = plan_from_candidates(cands, [])
        for e in plan["edits"]:
            e["decision"] = "AUTO_ACCEPTED" if e["auto_eligible"] else "SKIP"
        x, log = apply_repair_plan(x, fs, plan)
        fixed = sum(1 for r in log if r.get("applied"))
        fixes["clicks"] = fixed
        if fixed:
            actions.append(f"Исправлено щелчков: {fixed}")
    if on_fixes:
        on_fixes(dict(fixes))
    # rumble
    x = sosfilt(butter(2, prof["hpf"], btype="highpass", fs=fs, output="sos"), x, axis=0)
    actions.append(f"Убран инфранизкий гул ниже {prof['hpf']} Гц")
    # AI artifacts
    stats: dict = {}
    cl_step = step(0.18, 0.55)

    def cl_progress(f: float, text: str):  # the counts are known halfway: show them right away
        if stats and "harsh" not in fixes:
            fixes.update(stats)
            if on_fixes:
                on_fixes(dict(fixes))
        if cl_step:
            cl_step(f, text if "harsh" not in fixes else f"{text} · найдено {fixes_total(fixes)}")
    x, acts = cleanup(x, fs, CLEANUP.get(cleanup_level, 1.0), cl_progress, stats)
    fixes.update(stats)
    if on_fixes:
        on_fixes(dict(fixes))
    actions += acts or ["Заметных артефактов ИИ не найдено"]
    # mastering
    say(0.56, "Выравниваем тембр под стиль")
    x, acts, tone = tonal_eq(x, fs, prof, lowpass)
    actions += acts
    say(0.59, "Многополосная компрессия")
    x, acts = multiband(x, fs, prof)
    actions += acts
    say(0.62, "Склейка микса")
    x, acts = glue(x, fs, prof)
    actions += acts
    say(0.63, "Аналоговая окраска")
    x, acts = saturate(x, fs, prof.get("sat", 0.0))
    actions += acts
    say(0.64, "Панч ударных")
    x, acts = punch(x, fs, prof.get("punch", 0.0))
    actions += acts
    say(0.65, "Стереопанорама")
    x, acts = widen(x, fs, prof.get("width", 1.0))
    actions += acts
    x, _ = stage_stereo(x, fs, {"width": 1.0, "bass_mono_below_hz": float(prof["mono"])})
    actions.append(f"Бас собран в центр ниже {prof['mono']} Гц (звучит одинаково на телефоне и в клубе)")
    x, acts, lim = loudness_and_limit(x, fs, prof, step(0.66, 0.92))
    actions += acts
    # export
    fmt = fmt if fmt in FORMATS else "wav"
    say(0.93, "Сохраняем " + FORMATS[fmt])
    exts = {"wav": (".wav",), "mp3": (".mp3",), "both": (".wav", ".mp3")}[fmt]
    first = _out_path(src, out_dir, exts)
    wav = first.with_suffix(".wav")
    outputs = []
    if fmt == "mp3":
        wav = out_dir / f".{first.stem}.tmp.wav"
    write_float(wav, x, fs, BITS, dither="tpdf", clip_policy="allow")
    if fmt != "mp3":
        outputs.append(wav)
    if fmt in ("mp3", "both"):
        say(0.97, "Кодируем MP3 320 кбит/с")
        mp3 = first.with_suffix(".mp3")
        try:
            write_mp3(wav, mp3, src.stem)
        finally:
            if fmt == "mp3":
                wav.unlink(missing_ok=True)
        outputs.append(mp3)
    out = outputs[0]
    tp = (lim.get("limiter") or {}).get("final_true_peak_dbtp")
    if tp is None:
        tp = round(db(peak_envelope(x, fs).max()), 2)
    say(1.0, "Готово")
    return {
        "source": str(src), "output": str(out), "outputs": [str(o) for o in outputs], "name": src.stem,
        "fixes": fixes, "fixes_total": fixes_total(fixes),
        "style": chosen, "style_label": prof["label"], "auto_style": det, "style_set_by_user": style != "auto",
        "cleanup": cleanup_level, "actions": actions, "tone": tone,
        "before": before, "after": {"lufs": None if lim["lufs"] is None else round(lim["lufs"], 1), "true_peak": tp},
        "lossy_source": bool(pr.is_lossy), "lowpass_hz": lowpass, "seconds": round(time.monotonic() - t0, 1),
        "format": {"wav": f"WAV {fs // 1000}.{fs % 1000 // 100} кГц / {BITS} бит", "mp3": "MP3 320 кбит/с",
                   "both": f"WAV {fs // 1000}.{fs % 1000 // 100} кГц / {BITS} бит + MP3 320 кбит/с"}[fmt],
    }


def write_report(results: list[dict], out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    lines = ["ОТЧЁТ О ПОДГОТОВКЕ К РЕЛИЗУ", time.strftime("%d.%m.%Y %H:%M"), ""]
    for r in results:
        if r.get("error"):
            lines += [f"■ {r['name']} — ОШИБКА: {r['error']}", ""]
            continue
        a = r["auto_style"]
        st = r["style_label"] + (" (выбран вручную)" if r["style_set_by_user"] else
                                 f" (определён автоматически: {a['source']}, уверенность {a['confidence'].lower()})")
        files = ", ".join(Path(o).name for o in r.get("outputs") or [r["output"]])
        lines += [f"■ {r['name']}", f"  Файлы: {files}  ·  {r['format']}", f"  Стиль: {st}",
                  f"  Убрано дефектов: {r.get('fixes_total', 0)} ({fixes_text(r.get('fixes') or {})})",
                  f"  Громкость: было {r['before']['lufs']} LUFS → стало {r['after']['lufs']} LUFS; "
                  f"пик {r['after']['true_peak']} dBTP"]
        if r["lossy_source"]:
            lines.append("  Внимание: исходник сжатый (MP3/M4A). Для лучшего качества скачайте из Suno WAV.")
        lines += [f"  • {a}" for a in r["actions"]] + [""]
    p = out_dir / "ОТЧЁТ.txt"
    p.write_text("\n".join(lines), "utf-8")
    return p
