# Limitations (honest list)

## Requires a human
* **Subjective quality.** No metric here shows that a master "sounds professional". Every version needs
  loudness-matched listening; sign-off is recorded separately and never inferred.
* **Perceptual AI artifacts** (metallic/watery vocals, phasey reverb, smeared cymbals, garbled syllables,
  pre-echo) are not detected or repaired; always listed as `REQUIRES_LISTENING_REVIEW` /
  `NEEDS_SOURCE_REGENERATION_OR_STEMS`.
* Rights, ownership, songwriter credits and AI disclosure answers.

## Detector limits
* Click detector is conservative: it rejects events followed by sustained high-frequency energy, so a
  click **masked by a hi-hat/snare burst is missed** (demonstrated by a fixture test).
* Clipping detection needs ≥3 identical-level samples at the file maximum and a plateau above −6 dBFS.
* Encoder-lowpass/bandwidth estimate is coarse (60 dB-below-midband rule) and can report full bandwidth
  for AAC files whose encoder retained HF noise; do not use it as proof of source quality.
* Tempo: autocorrelation estimate; syncopated patterns can lock to ×2/3 or ×3/4 periods — alternatives are
  always listed. No beat grid.
* Style: simple heuristic, always LOW confidence; no ML model shipped.
* True peak uses 4× oversampling (BS.1770); can under-read up to ~0.14 dB near 16 kHz (limiter keeps 0.1 dB margin).

## Repair limits
* Repairs only short local defects (≤3 ms clicks, ≤2 ms clipped runs, fades). Declip cannot restore lost
  peaks; widespread clipping is not offered. No dropout filling, no denoise, no spectral repair.
* Source separation is `UNAVAILABLE`; stems can be imported and checked (sum vs mix) but are not remixed.

## Other
* `compat_wav` resampling 48→44.1 kHz can raise true peak; the validator measures it and may FAIL the track
  (re-render at 44.1 kHz in that case) — no automatic re-limiting at export.
* Multichannel (>2 ch) sources are not downmixed automatically.
* MP3 preview needs an FFmpeg build with libmp3lame; FFmpeg is not bundled (licensing).
* Windows: built and launched on Windows 11 x64 only; Windows 10 **NOT VERIFIED**. No installer (.msi),
  only PyInstaller onedir folders; executables are unsigned (SmartScreen may warn).
* Offline acceptance: the application contains no network code outside the optional MCP server (code audit),
  but a run with the network adapter physically disabled was **NOT VERIFIED** here.
* Live ChatGPT connection **NOT VERIFIED** (needs the user's account and a public HTTPS URL).
