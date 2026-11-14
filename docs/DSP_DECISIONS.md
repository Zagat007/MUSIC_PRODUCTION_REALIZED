# DSP decisions

## Measurement
* **Loudness**: ITU-R BS.1770-4 K-weighting for any sample rate (libebur128 analog prototype), 400 ms
  blocks / 100 ms hop, absolute −70 LUFS and relative −10 LU gates; short-term 3 s; LRA per EBU Tech 3342
  (−20 LU relative gate, 10th–95th percentile). Verified: 1 kHz −23 dBFS stereo sine → −23.0 LUFS at
  44.1/48/96 kHz; ±0.1 LU vs pyloudnorm; ±0.15 LU vs FFmpeg `ebur128` on codec files.
* **True peak**: 4× oversampling below 96 kHz (2× below 192 kHz), Kaiser(7) windowed sinc with 24 taps per
  phase (benchmarked equal to 64 taps; 2.2× faster). Exact on an analytic fs/4 test (−6.021 vs −6.0206
  dBTP); within 0.3 dB of FFmpeg. Known: a 4× grid can under-read by up to ~0.14 dB for content near
  16 kHz → the limiter uses an internal 0.1 dB margin.
* **Tempo**: spectral-flux onset envelope + autocorrelation + log-Gaussian prior (120 BPM, 1 octave);
  reports top distinct periodicities and half/double readings, flags `octave_ambiguous`.
* **Style**: transparent feature heuristic, always `LOW` confidence, ranked, with evidence; user override wins.

## Conversion (CONVERT_ONLY)
* Lossless integer source at an allowed native rate with ≤ target bits → **bit-exact copy**, verified
  sample-for-sample after writing (16-bit → 24-bit is zero padding, reported as "no added resolution").
* Otherwise: one decode, one resample only if needed (FFmpeg soxr precision 28), one TPDF dither at the final
  quantization. Decoded AAC overs (> 0 dBFS) → `overs_policy` trim (static gain, logged) / clip / fail.
* Rates: keep 44.1/48 kHz; 88.2/176.4 → 44.1; others → 48 kHz. Mono → labelled dual-mono stereo.

## Mastering chain
Fixed order `dc_hpf → eq → compressor → stereo → gain → limiter`; all parameters bounded and validated
(out-of-range values are rejected, not clamped). EQ = RBJ biquads (minimum phase, causal; no added latency,
frequency-dependent phase shift). Compressor = linked, RMS-detected, soft-knee, ratio ≤ 3:1, block-smoothed.
Stereo stage off by default; bass-mono removes only the low band of the side signal.

**True-peak limiter**: per-sample oversampled peak envelope → required gain → centred sliding minimum
(2L−1) → exponential release (block domain) → causal box average of the gain *deficit* (L samples). This
construction guarantees gain ≤ required gain at every sample; the result is re-measured and the pass
repeated with a lower target if any over remains (≤ 6 iterations). Offline look-ahead ⇒ zero latency and
identical sample count. Regions that need no limiting are **bit-identical** (deficits < 1e-12 and release
residue < 1e-9 snapped — far below one 24-bit LSB). Regression tests: hostile signals (+3 dBTP intersample
tone, hard-clipped tones, +8 dB overdriven music) at −1 and −2 dBTP ceilings, full-scale file start.

**Level planning**: no universal −14 LUFS. Policies `preset_window` (move only if outside the style's
suggested window), `preserve`, `target`, `album_offset`. When raising level would need more than the preset's
max limiter reduction, the gain is capped instead (protects transients).

## Repair
LSAR (Janssen) interpolation on the residual after removing a zero-phase low-pass trend (needed: AR(32)
cannot bridge long bass periods). Each edit must pass: outside-region and other-channel bit-identity,
unchanged length, no click re-detected in real context, HF-energy ratio ≤ 3× context, peak within the local
envelope (declip: must exceed the clip level, ≤ +6 dB). Widespread clipping is never offered for repair.

## Dither
TPDF ±1 LSB, deterministic seed per (track, version); applied once at final quantization; never on the
bit-exact path.
