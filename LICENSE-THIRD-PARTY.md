# Third-party components and licensing decisions

Audit date: 2026-11-14, from installed package metadata (`importlib.metadata`), not from memory.
All components are free of charge. No paid service, API, model or account is required.

## Runtime (core)

| Component | Version | License | Notes |
|---|---|---|---|
| Python | 3.11 | PSF | |
| NumPy | 2.4.6 | BSD-3-Clause (+0BSD/MIT/Zlib/CC0 parts) | |
| SciPy | 1.17.1 | BSD-3-Clause | |
| Pillow | 12.3.0 | MIT-CMU | Local image normalization and procedural artwork. Retain the wheel's license notices, including bundled image-codec notices. No paid model or API. |
| soundfile (python) | 0.14.0 | BSD-3-Clause | |
| libsndfile (bundled in soundfile wheel) | 1.2.2 | **LGPL-2.1+** | Shipped as a separate DLL (dynamic linking) → LGPL satisfied by keeping it replaceable and providing this notice + source link (https://github.com/libsndfile/libsndfile). |
| pyloudnorm | 0.2.0 | MIT | Used only in tests as an independent cross-check. |

## GUI

| PySide6 / shiboken6 | 6.12.0 | LGPL-3.0 (or GPL) | Used under **LGPL-3.0**: dynamic linking, Qt DLLs shipped unmodified and replaceable in the PyInstaller *onedir* build (not onefile). Provide Qt source link (https://download.qt.io/) and this notice when distributing. |

## MCP bridge (optional)

| mcp / mcp-types | 2.3.0 | MIT | Official MCP Python SDK. |
| starlette, uvicorn, sse-starlette, httpx2 | various | BSD-3-Clause | |
| pydantic, anyio, pyjwt, jsonschema, cffi | various | MIT / MIT-0 | |
| opentelemetry-api, python-multipart | various | Apache-2.0 | |
| pywin32 | 312 | PSF | |

## Build-time only

| PyInstaller | 6.22.3 | GPLv2+ **with bootloader exception** | Exception permits distributing the generated executables under any license. |

## External tool — FFmpeg (NOT bundled)

The development machine's FFmpeg 8.1.1 (gyan.dev "full" build) is configured with `--enable-gpl
--enable-version3` → **GPL-3.0**. Bundling it would make the distributed package subject to GPL
obligations (source offer for FFmpeg and its GPL libraries). Decision: **do not bundle**; the
application locates a user-installed FFmpeg (PATH, `SRM_FFMPEG`/`SRM_FFPROBE`, or `tools\ffmpeg\`).
Used features: ffprobe, native AAC/ALAC/FLAC decoders, `aresample` with libsoxr (LGPL), libmp3lame
(LGPL) for MP3 previews. An LGPL-only FFmpeg build with libsoxr and libmp3lame is sufficient if a
redistributable bundle is wanted later. FFmpeg is invoked as a separate process with fixed argument lists.

## Not included (by decision)

| Component | Reason |
|---|---|
| Essentia + Discogs-EffNet models | Essentia is AGPL-3.0; model weights CC BY-NC-SA (non-commercial). Not bundled; heuristic style fallback. Capability reported `UNAVAILABLE`. |
| Demucs / other separation weights | Upstream Demucs unmaintained; weights/licences need review per model. Not installed; stem import used instead. |
| DeepFilterNet | Speech denoiser; not appropriate for full-mix music by policy. |
| iZotope RX or other commercial tools | Paid; only documented as an optional manual step. |
| librosa / numba | Not needed; tempo/onset implemented with SciPy. |

No ML weights are downloaded or shipped. The application makes **no network requests**.
