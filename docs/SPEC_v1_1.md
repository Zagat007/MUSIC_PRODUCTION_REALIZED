# SUNO RELEASE MASTER
## Full implementation brief for Claude Code
Version: 1.1 (ZERO-COST EDITION) | Date: 2026-11-14

# YOUR ROLE AND OUTPUT CONTRACT

You are Claude Code acting as a senior audio-DSP engineer, mastering workflow architect, Python desktop engineer, release-delivery QA engineer, and OpenAI MCP/plugin integration engineer. **Build a working application, not a concept document, pseudocode, mock interface, or generic chatbot.** It must process real audio locally and expose safe, actionable tools to ChatGPT.

Name: **Suno Release Master**.

Objective: turn a large collection of songs generated with Suno (versions from 4.5 onward), commonly exported as `.m4a`, accompanied by occasional `.txt` song prompts and lyrics, into curated, quality-controlled release packages for distributors serving Apple Music/iTunes, Yandex Music, Spotify, and other services. This includes trustworthy audio analysis, conservative optional music restoration, genre-aware mastering, lossless delivery export, album management, and a ChatGPT MCP plugin with an optional small Skill.

Primary OS: Windows 10/11 x64. Local offline-first design. An optional secure remote connection to ChatGPT must not make cloud uploads mandatory. All substantive DSP happens in the local processing engine, never in the LLM itself. Prefer a local GUI with a reusable CLI and Python API. If existing dependencies are incompatible with Windows, implement a reliable fallback instead of pretending they work.

Work in Russian in README/user interface if practical; code, tests, API identifiers, and machine-readable schemas may be English. Preserve UTF-8 Unicode filenames and Russian text. Do not silently substitute fabricated audio analyses or fake processing.

# 0. HARD BUDGET CONSTRAINT: ZERO ADDITIONAL COST

**Absolute requirement:** the user cannot pay for new services, licenses, APIs, hosting, domains, subscriptions, credits, or model inference. The local audio processing application must be genuinely useful **without any additional paid product or subscription**. This constraint overrides all other optional suggestions in the brief.

- Ship a **fully functional offline-first Windows application** with no login, no payment card, no account, no subscription, no paid external API, and no mandatory cloud/remote server. Use only free-to-use software and properly licensed open-source components for the shipped core; maintain a dependency/license manifest and explain any GPL/LGPL obligations before distribution.
- Require **no paid AI model or API** (including OpenAI, Anthropic, paid audio restoration endpoints, commercial mastering APIs, paid genre detectors, paid speech-to-text services or GPU rental). Optional model-based modules are allowed ONLY when model weights can be used and obtained at no additional cost under an appropriate license, operate fully on user hardware, and are not needed for the baseline feature set. Never accept "free trial" or "free credits" as a permanent zero-cost solution.
- Start with existing local CPU capability; automatically leverage local GPU only when compatible and without requiring new hardware or services. Bound RAM, disk and CPU use; include CPU-only processing and clear warnings for intensive operations.
- Provide free/accessible DSP and signal inspection independent of pretrained models: FFmpeg/ffprobe, NumPy/SciPy, soundfile, suitable lawful DSP libraries, and real verification. Avoid dependency choices that make a commercial license necessary for the target distribution, and do not silently assume "open source" means no obligations.
- **ChatGPT MCP integration must remain optional** and must not block or degrade the local software. Do not assume the user's ChatGPT subscription supports custom MCP or that it is free to enable. No required VPS, registered domain, paid tunnel, hosted database, or cloud storage. If a secure connection cannot be established without extra spending, provide working local CLI/GUI plus installable Skill instructions and document MCP as "AVAILABLE WHEN PLAN/PERMISSIONS/SECURE CONNECTIVITY ALLOW". No insecure public tunnel merely to claim success.
- No remote upload of source audio, stems, lyrics, prompts or metadata without explicit opt-in. Never make unapproved network requests. Cache optional downloads locally, show exactly what is downloaded, and support disabled networking.
- The software must not falsely promise that local automated mastering or AI-defect repair achieves studio-grade results. Retain listening review and truthful feature/quality limitations, especially for lossy Suno AAC sources.
- Budget gate for **every milestone**: confirm **zero new charges** are required. If a technology requires payment, replace it with a no-cost alternative or mark that optional feature unavailable; do not request payment or ask for credit card credentials.

**Acceptance test:** install on Windows without entering payment information; disconnect network; import an M4A folder, analyze one song, convert it to an authentic PCM WAV, run technical validation, export a release folder, and inspect results. These core operations MUST work without any payment service or remote API. The optional mastering workflow must also function locally and offline using freely usable DSP; do not assert subjective quality without listening tests.

# 1. MANDATORY ENGINEERING PRINCIPLES

1. **Protect sources:** never alter, delete, overwrite, or rename originals. Use a read-only ingest model; write unique versioned output paths; preserve provenance, SHA-256, source path, codec and generation metadata. Cancellation and crashes must not corrupt source or existing outputs.
2. **Do not fabricate fidelity:** AAC-in-M4A to WAV is transcoding, not loss restoration. AI restoration cannot guarantee recovery of information missing from the mix. Do not claim a converted or upsampled file qualifies for Apple Digital Masters or Hi-Res. Offer an explicit warning when a lossy source is used. Prefer native/lossless WAV, FLAC or true lossless ALAC when available; identify M4A codec (AAC vs ALAC) using ffprobe, never extension alone.
3. **Mastering is optional:** Suno output may already be heavily processed. The default path must be analyze-only, with non-destructive, conservative mastering and a way to export without touching the sound. Do not add compression, EQ, enhancement, or denoise merely to demonstrate that software did something.
4. **No destructive automatic AI artifact removal:** a detected candidate is not guaranteed to be a defect. Show timestamps and evidence, offer original/processed A-B listening, avoid harm to vocals, cymbals, transients and intentional effects. When confidence is low, flag for manual review. Model-based repair must be opt-in, reversible, and tested against input.
5. **Distinguish technical QA from aesthetic approval:** valid codec, measured peaks and lack of clipping do not establish a professional-sounding master. Report `TECHNICAL_PASS`, `NEEDS_LISTENING_REVIEW`, `FAILED` separately. Never claim subjective professional mastering is objectively PASS based on metrics alone.
6. **No universal -14 LUFS requirement:** streaming services may normalize playback. Provide genre- and dynamics-aware selectable loudness plans; prioritize punch and preservation of the composition. Set defensible true-peak ceilings (initial default -1.0 dBTP, optionally -2 dBTP for hotter lossy delivery previews); measure the rendered files and codec previews instead of trusting preset values.
7. **Compliance is not invented:** user-selectable distributor profiles; verify official distributor specs at time of implementation. Separate generic WAV preset from distributor-specific requirements. Provide rights and AI-disclosure checklists, not automatic legal approval.
8. **Batch operations must be restartable and auditable:** deterministic configs, retry/resume by track and stage, safe concurrency, durable logs, minimal repeated DSP, ETA and progress without false completion states.
9. **A plugin/Skill cannot by itself access arbitrary Windows folders from ChatGPT:** construct a genuine local processor and MCP backend, with a documented secure bridge to ChatGPT, plus optional installable Skill containing instructions. Provide fallback to GUI/CLI when MCP authorization or connectivity is unavailable.
10. **Do not assume all model weights are legal to ship:** audit licenses of FFmpeg builds, DSP libraries, pretrained models, and optional proprietary tools. Never bundle downloaded ML weights without checking licenses and reproducibility.

# 2. USER WORKFLOWS

A. User points GUI/CLI at a top-level music folder. Recursively discover M4A, WAV, FLAC, ALAC/M4A, MP3 and prompt/lyrics TXT/MD files. Present release/album candidates according to folder structure; allow manually moving or assigning tracks to albums without moving the originals. Detect duplicate and alternate mixes by robust file hash, optional perceptual fingerprint, and naming; never silently discard duplicates.

B. Read likely adjacent TXT prompts, lyrics and metadata. Match by exact normalized basename first, then constrained fuzzy matching with a confidence indicator and human confirmation for ambiguous matches. Keep prompt text as descriptive intent, not proof of what the audio actually contains. Preserve separate fields: title, lyrics, style prompt, model/version if provided, source date if known, genre tags, language, vocalist/source declarations, album, track order, rights notes. Do not assume unknown metadata.

C. Analyze each track and generate an inspection report, with audio measurement, likely genre/style, candidate sonic issues and critical time ranges. Show linked evidence and confidence. A text prompt about liquid drum-and-bass should be cross-checked against actual rhythm; explicitly represent half-time/double-time tempo ambiguity (e.g., 87 vs 174 BPM).

D. Offer three concrete workflows: `CONVERT_ONLY` (bit-transparent where possible, no discretionary processing); `SAFE_MASTER` (conservative genre-aware processing); `RESTORE_AND_MASTER` (repair suggestions and approval checkpoints before applying uncertain edits). Also allow `ANALYZE_ONLY`. `RESTORE_AND_MASTER` must not run blindly on all tracks.

E. Allow audition at matched perceptual loudness: source, conversion-only, repair candidate, master candidate, and final encoded preview. Provide waveform, spectrum, spectrogram, selectable A-B comparison, gain compensation, clipping/issue markers and playback. Optionally offer suggested segment ranges. Quality enhancement is rejected if it audibly damages music; the application must retain the original branch.

F. Export an album as a distributable directory with ordered WAV masters, optional MP3 320 kbps previews, optional FLAC archival copies, album metadata CSV/JSON, per-track QC JSON/HTML, album QA summary, original-source/provenance log, and explicitly unresolved warnings. Do not automatically publish or distribute tracks. Export a separate cover-art checklist; no cover-art generation is required.

G. Let ChatGPT request `scan -> analyze -> propose settings -> preview -> user approves -> render -> validate -> export`, inspect precise results, and download reports or approved outputs through secure tool responses. The ChatGPT plugin must be thin orchestration, never a second DSP implementation.

# 3. MODULES AND IMPLEMENTATION

Recommended stack: Python 3.11/3.12 for core audio pipeline and API; NumPy, SciPy, soundfile and FFmpeg/ffprobe via safe subprocess invocation; `pyloudnorm` or a cross-validated libebur128 implementation for metering; librosa or other maintained signal analysis libraries for basic rhythm/spectral features. PySide6 for Windows desktop GUI (check licensing). SQLite with migrations for projects, job queue, processing versions, track associations, approvals and QC results; JSON export of every effective processing configuration. Use typed dataclasses/Pydantic models, pytest, ruff, type checking on core modules. Consider FastAPI for a local service. Prefer the official, currently supported MCP Python SDK for the ChatGPT integration; verify its API against official docs and pin tested versions.

Use FFmpeg with fixed, validated argument arrays; no shell interpolation and no arbitrary client-provided shell commands. Surface dependency versions and `ffmpeg -version` output. Check installation requirements and relevant licenses. For high-quality resampling use a tested high-quality resampler (e.g., FFmpeg soxr where available), only when conversion is actually necessary. Internal intermediate audio may be float32/float64; export signed PCM with deliberate final quantization. Apply dither once if a true bit-depth reduction needs it, especially to 16-bit; avoid gratuitous dither/resampling loops.

Optional genre model: Essentia/Discogs or compatible pretrained multi-label music-classification model, but only as an optional adapter after checking current Windows compatibility and AGPL/commercial licensing. There must be a functioning license-safe basic fallback without the model. Classifier should output a ranked list, confidence, and explain its limits; user overrides always win. Never label a classifier's guess as fact. Genre metadata and mastering preset mapping must be editable.

Optional source separation and repair: if original stems from Suno are available, offer a safe import/relink process; avoid degrading the original stereo mix. Alternative separation modules (e.g., Demucs, MDX or other legally usable models) may be optional, behind a capability check and sound-quality warning. Demucs' original upstream is not actively maintained; verify maintained options and weights rather than installing it blindly. General speech-denoising models such as DeepFilterNet should NOT be run on complete songs by default. Proprietary processors such as iZotope RX may be documented as optional external/manual interventions; do not claim you can invoke an API they do not expose.

Proposed code structure:

```
suno-release-master/
  pyproject.toml
  README.md
  CHANGELOG.md
  LICENSE-THIRD-PARTY.md
  app/
    core/           # data models, project manager, SQLite, jobs, immutable versions
    ingest/         # scanning, codec detection, file matching, provenance
    analysis/       # loudness, peaks, phase, dynamics, genre, defect candidates
    dsp/            # safe conversion, restoration adapters, EQ/compression/limiting, dither
    release/        # distributor profiles, metadata, packaging, validation
    service/        # authenticated local HTTP API, long-running job worker
    mcp/            # ChatGPT plugin tool definitions, schemas and safe access
    gui/            # PySide6 application
    cli/            # command-line client
  plugin/
    skill/          # optional ChatGPT SKILL.md + minimal references
    manifest/       # applicable plugin packaging and install notes
  tests/
    unit/
    integration/
    audio_fixtures/
    qa/
  docs/
    ARCHITECTURE.md
    DSP_DECISIONS.md
    AUDIO_VALIDATION.md
    RELEASE_PROFILES.md
    OPENAI_INTEGRATION.md
    USER_GUIDE_RU.md
    LIMITATIONS.md
  scripts/
    setup_windows.ps1
    test_windows.ps1
    build_windows.ps1
```

Choose a simpler structure if it improves maintainability, but keep the DSP engine independent of GUI and MCP.

# 4. AUDIO INGEST AND MEASUREMENTS

Implement robust ffprobe + decoder verification. Record: true container and codec; codec bit rate and AAC vs ALAC; input sampling rate, channel count/layout, duration, bit depth where meaningful; file hash; detected silence and clipped start/end; sample peak, reconstructed true peak via validated oversampling, integrated LUFS, momentary and short-term LUFS charts, LRA, dynamic crest factor, low/high spectral energy, spectral balance, DC offset, channel peak imbalance, stereo correlation, mid-side width and potential mono incompatibility. Flag channels or digital silence that are unexpectedly absent. Confirm all tracks actually decode; do not accept a file merely because its extension says WAV.

Detect (as candidates, with timestamps and confidence): hard sample clipping; likely clicks/pops; obvious dropouts; abrupt leading/trailing cuts; abnormal isolated sub-bass; possible high-frequency hiss/harshness; strong phase/mono cancellation; excessive brickwall limiting; suspicious burst distortion; possible pre-echo or musical smearing. Be explicit that metallic vocals, synthetic timbre and embedded AI reconstruction problems cannot be reliably identified/repaired by simple numerical tests and require listening review or better stems.

If heavy models aren't installed, show `UNAVAILABLE` for those capabilities with documented manual alternatives. A failure to detect does not imply that a defect is absent.

Prompt-assisted analysis: safely parse adjacent TXT/MD files (UTF-8 with fallback and encoding errors handled), extract optional language/style/instruments/mood/lyrics, compare to audio tag predictions. Never overwrite filename/album or rights fields with speculative prompt extraction. Support text with special characters and large lyric blocks. Allow different languages and bilingual metadata.

# 5. MASTERING SIGNAL CHAIN

Create a modular non-destructive chain with per-stage bypass, versioned settings, and parameter bounds. Suggested ordering, adapted only when applicable: source decode -> technical fixes and selected repair -> corrective high-pass/DC removal where justified -> gentle tonal EQ -> transparent dynamics control as required -> optional stereo management with mono check -> validated true-peak-safe limiting -> final-rate/bit-depth export -> independent QA. Processing stages must preserve time alignment as much as practicable and include latency compensation.

For each proposed EQ/compression/limiter action, show `problem -> evidence -> proposed remedy -> risk -> settings -> before/after measurements`; do not call an opaque external model that returns an unexplained "pro-master" result. In audio-only mode, limit defaults to conservative adjustments. Do not flatten dynamics or remove natural reverb just to meet an average profile.

Offer starting profiles to be tuned by measurements and human listening: liquid DnB (preserve kick and sub-bass separation, snare transients and vocal presence), ambient/jazz (preserve natural dynamics, imaging and silence), acoustic/ballads (preserve voice and natural attack), pop/electronic (energy without unnecessary clipping), hybrid/ethnic music (avoid aggressive broadband treatments that destroy acoustic instruments). Presets are suggestions, not automatic guarantees or fixed tonal EQ curves. Manual per-track overrides, album-wide targets, dry-run, and per-stage bypass are required.

No force-fit to -14 LUFS. Default output ceiling -1.0 dBTP with cautious settings, and allow more headroom when preview codec or very hot masters require it. Measure at the actual final sample rate with suitable oversampling; validate a real true-peak limiter rather than assuming a sample-peak limiter prevents intersample overs. Check preview encode headroom and revise if peaks/codec overs occur. State any DSP limitations explicitly.

Album mastering: measure all album tracks together; respect intended loudness contrast, quiet interludes and dynamic arcs. Offer an album cohesion report (relative perceived loudness, brightness, bass balance and stereo presentation), and suggest adjustments without enforcing identical integrated LUFS per track. Support cross-track continuous playback and track gaps.

# 6. MUSIC RESTORATION / AI ARTIFACTS

Implement a structured and testable artifact registry:
- digital click/pop and short impulsive defect -> local candidate detector and limited interpolation with protective transient discrimination;
- isolated damaged segment -> spectral display and, where implemented, short-region repair with A-B comparison;
- clipped waveform -> cautious declip candidate with confidence and validation; no promise to restore lost original peaks;
- harsh/high-frequency ringing or unnatural hiss -> mild targeted controls rather than destructive full-song denoise;
- metallic or watery vocal, phasey reverb, smudged cymbal, mangled transient -> listening flag and manual/stem-based option; no dishonest "one-click AI artifact elimination";
- low-quality lossy AAC pre-echo / removed bandwidth -> mark source limitation; never fabricate native high resolution.

Use an explicit severity scale (`NONE`, `MINOR`, `MODERATE`, `SEVERE`, `UNCERTAIN`) independent of confidence, and retain a reproducible list of applied edits and time regions. Automatic repair is permitted ONLY for high-confidence, localized technical defects whose proposed fix passes objective regression checks; everything else needs explicit approval. Include bypass and undo/versioning for every repair. An automated repair must not introduce new clicks, shorten audio unexpectedly, or alter undamaged regions without flagging it.

Provide a `NEEDS_SOURCE_REGENERATION_OR_STEMS` status for defects the tool cannot fix safely. Never remove music intentionally containing distortion, glitch or lo-fi texture just because a classifier flags it.

# 7. EXPORT / DISTRIBUTOR PROFILES

Default `RELEASE_WAV`: stereo WAV signed PCM 24-bit with native 44.1 or 48 kHz if compatible; if another source rate is encountered, use a distributor-supported rate and log actual high-quality conversion. Never invent sample-rate/bit-depth information absent from the source, and do not badge as Hi-Res. `COMPAT_WAV`: 16-bit PCM stereo 44.1 kHz only if user needs that profile. `PREVIEW_MP3`: 320 kbps stereo MP3 encoded from approved master only; not the delivery source unless distributor explicitly requests. Optional lossless FLAC archival. If source is mono, handle as a conscious, labelled stereo export if necessary, not secretly as spatial enhancement.

Record which distributor's current audio rules were verified; keep profiles configurable by platform/distributor and do not blindly equate Apple's direct delivery spec with DistroKid or another distributor's upload form. Treat Apple Digital Masters as a separate eligibility workflow, not a toggle for files sourced from lossy AAC or padded bit depths. Include a machine-readable metadata manifest for artist/title/album/track number/version/genre/year/language/lyrics credits/AI involvement/ISRC if supplied, with blank or `UNKNOWN` for unknown fields. The product must never invent ISRC/UPC, legal ownership, or songwriter names.

Package layout example:

```
RELEASES/Album_Name/v001/
  WAV_MASTER/01_Track_Title.wav
  MP3_PREVIEW/01_Track_Title.mp3
  FLAC_ARCHIVE/             # optional
  QA/01_Track_Title.json
  QA/01_Track_Title.html
  album_metadata.csv
  album_metadata.json
  rights_ai_disclosure_checklist.json
  release_validation.json
  album_qa.html
  processing_manifest.json
```

Checks for release export: WAV decodes successfully; correct PCM/bit-depth/rate/channels; checksum; no unintended silence/cut/truncation; no invalid paths or duplicated names; final integrated LUFS/LRA/TP measured and compared with chosen policy; codec-preview check and potential overs; manual listening signoff state; metadata and track count/order; unresolved rights or AI-disclosure fields clearly flagged. Export can still be created as `DRAFT_NOT_RELEASE_READY` if a user explicitly requests, but never label it approved.

# 8. GUI AND CLI

GUI: dark, functional, non-decorative desktop interface with Album Library / Track Analysis / Repair / Mastering / Release Validation / Settings. Show selected files and matched prompts, export format, full meter results, trustworthy progress with failed items, audio player with waveform and relevant markers, original vs proposed master A-B loudness-matched playback, editable per-track processing chain, album coherence overview and report viewer. Support batch select, queue pause/cancel/resume, project persistence and output destination picker; avoid mandatory cloud accounts. If advanced wave UI is not feasible initially, implement a reliable basic player and inspection table, then iterate.

CLI must work without GUI and use same core engine:

```
srm scan "D:/Music/Suno" --project "D:/Music/Projects/collection"
srm analyze --project "D:/Music/Projects/collection" --album "Album A"
srm propose --project "..." --album "Album A" --mode safe_master
srm preview --project "..." --track-id <id>
srm approve --project "..." --track-id <id> --version <v>
srm render --project "..." --album "Album A" --approved-only
srm validate --project "..." --album "Album A"
srm export --project "..." --album "Album A" --profile release_wav
```

Implement the real options and document them; don't simply copy this example and assert nonexistent commands work.

# 9. CHATGPT MCP PLUGIN + OPTIONAL SKILL

Keep processing in the local app. Expose typed, authenticated MCP tools, with clear descriptions, validation, idempotency keys, error schemas and user confirmation for final writes/exports:

- `health_and_capabilities`: processor version, engines/models available, backend status;
- `list_projects`, `list_albums`, `list_tracks`;
- `scan_library` (restricted to pre-authorized library roots, no arbitrary absolute paths from the model);
- `analyze_track`, `analyze_album`;
- `get_analysis_report` with measurements, timelines, confidence and unknowns;
- `suggest_mastering` returning a proposal, not applying it;
- `create_preview` for candidate repair/master;
- `approve_processing_version` requiring explicit user-approved version;
- `start_batch_render`, `get_job_status`, `cancel_job`;
- `validate_release`;
- `export_album` requiring user confirmation and selected profile;
- `get_release_manifest` and `get_artifact_download` (short-lived scoped URL or platform-supported file object).

MCP must not expose a general shell command executor, unrestricted filesystem browsing, deletion tools, or arbitrary external URLs. Implement access control, rate limits, secret storage, audit logs, path validation, port binding defaults, appropriate authorization and explicit user consent for network file transfer. Prefer showing the model summaries and selected short audition clips rather than uploading entire music libraries. For small user-selected uploaded files, optionally support the current ChatGPT plugin file-handling APIs when available; feature detect and don't rely on them for the bulk batch workflow.

Document ChatGPT connection options based on **current official OpenAI docs**. ChatGPT currently connects to remote MCP HTTPS endpoints or supported secure MCP tunnels, not directly to an arbitrary localhost STDIO endpoint. The app must remain useful if the user cannot create a custom MCP integration, lacks workspace admin permissions, or is using mobile where setup is not supported. Provide both secure tunnel and separately hosted gateway designs, with clear threat models and limitations. Do not demand that the user publish any plugin to the public directory.

Package an optional compact ChatGPT Skill (`SKILL.md`, `agents/openai.yaml`, short references). It should instruct ChatGPT how to use MCP tools to analyze, propose, preview, await approval, render and verify; must never imply it itself processes massive local Windows files. Skill packaging should follow official ChatGPT conventions. The Skill is a companion, not the audio engine.

# 10. QA, TESTS, AND ACCEPTANCE GATES

Build unit + integration + audio-regression tests. Include generated synthetic fixtures (not copyrighted third-party recordings) for silence, impulses, phase inversion, DC offset, clipped sine, true-peak intersample overs, stereo/mono, 16/24-bit PCM, AAC M4A vs ALAC M4A, unusual filenames (Unicode/Cyrillic), corrupt files, 44.1/48 kHz, large directories and repeated reprocessing. Include acoustic fixture(s) available with permission for listening tests. Do not pretend synthetic signal tests prove good musical mastering.

Concrete acceptance gates:
1. A batch of at least 100 representative fixture entries can be discovered and analyzed, with correct resume after a controlled interruption.
2. The input file hashes remain identical before and after all operations, including failure/cancellation.
3. AAC-in-M4A is identified as lossy and explicitly warned; ALAC-in-M4A is distinguished.
4. WAV export codec, sample rate, bit depth, channel count and duration are externally probed from actual exported bytes; no header spoofing.
5. At least one real end-to-end CLI path and one GUI path run against fixtures.
6. Reports contain pre- and post-processing measurements, exact processing configurations, source provenance, and per-track errors.
7. The true-peak measurement is independently cross-checked; final file levels remain within selected export profile tolerances; real-encoded MP3/AAC previews are analyzed when requested.
8. No auto-repair runs on uncertain regions or silently changes untouched sections. Every destructive-sounding step has bypass, history and an A-B path.
9. The genre/style tool supports uncertain or mixed classification and human override; prompt files are matched correctly or flagged ambiguous.
10. Album export creates ordered files and truthful metadata, with `DRAFT_NOT_RELEASE_READY` if rights or listening approval remains unresolved.
11. MCP calls actually reach the same processing backend; they are not mocked at final delivery; unauthorized paths/requests fail closed.
12. Real Windows installation steps and an executable Windows build are provided where the build environment supports them. If unable to compile on the current OS, honestly mark Windows binary `NOT VERIFIED` and provide tested local build scripts; do not assert Windows compatibility without testing.
13. CI tests pass with logged commands and exit codes. Audio QA and subjective listening signoff must be reported separately, never fabricated.
14. Gracefully recover from missing model weights, missing FFmpeg, GPU unavailable, partial files, text encoding issues, read-only directories and low disk space.
15. Document output provenance, reproducibility and all licensing decisions.
16. Run the zero-cost, offline, no-login installation and real audio processing acceptance test; ensure no paid service, API key, payment method, or hosted backend is needed for core functionality.

# 11. DEVELOPMENT ORDER / DELIVERABLES

Implement in sequential milestones, with working artifacts and test reports at each stage, not as one giant untested code dump:

M0: inspect current repository/environment; verify documented API standards and dependencies; record decisions, legal/license constraints, **zero-additional-cost dependency audit**, known unknowns and executable plan.
M1: ingest, source analysis, lossless conversion and immutable versions; CLI and reports; unit/integration tests.
M2: reliable mastering chain, true-peak metering/validation, safe presets, A-B and audio regression; tests.
M3: candidate artifact detection, localized repair previews, cautious optional separation/stem support with real adapter capability detection and tests.
M4: album/project organization, prompt-to-audio association, release manifests and distributor-specific configurable export profiles; tests.
M5: Windows GUI, actual local jobs, resume/cancel, progress, A-B listening; test and package.
M6: secured MCP plugin integration and optional installable ChatGPT Skill; end-to-end integration test and setup manual.
M7: full regression, security/license audit, 100-file batch dry-run, real export inspection, final fixes and deliverable verification.

For each milestone: implement code, run available tests, inspect actual artifacts, fix root causes, rerun affected tests plus integration regressions, and commit/checkpoint only when relevant tests pass. Do not move forward based solely on a happy-path demo. If a capability is not practical/reliable, label it explicitly `UNAVAILABLE` or `REQUIRES_MANUAL_REVIEW`; never invent success. Do not pause for approval between non-critical milestones. Ask only if truly blocked by missing credentials/hardware/authority; everything else gets a safe default plus documented override.

Final deliverables:
- Complete editable repository; lockfile/pinned dependencies; Windows setup/build/test scripts; functional CLI; functional local processing backend; GUI; source-independent test data; source licenses document.
- A practical Windows distribution (`.exe`/installer) if build environment supports verified packaging, otherwise tested build instructions and honest limitations.
- Test logs, QA samples generated from fixtures, result checksums, real ffprobe/ffmpeg validation output, and a concise PASS/FAIL/NOT VERIFIED table.
- Russian user guide: how to import albums, choose source, analyze, select safe vs restoration mode, preview, approve, export and validate before distributor submission.
- MCP plugin server with tools, setup and authentication instructions, plus a separately installable Skill bundle if technically appropriate.
- A final explicit statement of what genuinely works, what is limited/experimental and what still requires a human to listen.

# 12. EXTERNAL AUTHORITATIVE REFERENCES TO CHECK DURING DEVELOPMENT

- OpenAI plugins documentation: https://developers.openai.com/plugins
- OpenAI MCP server tutorial: https://developers.openai.com/plugins/build/app-quickstart
- OpenAI plugin file reference: https://developers.openai.com/plugins/reference
- ChatGPT custom MCP connection rules: https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
- ChatGPT Skills: https://help.openai.com/en/articles/20001066-skills-in-chatgpt
- Apple source audio + Apple Digital Masters restrictions: https://help.apple.com/itc/videoaudioassetguide/en.lproj/static.html
- Spotify loudness normalization and mastering tips: https://support.spotify.com/ua-uk/artists/article/loudness-normalization/
- FFmpeg filter documentation, `loudnorm`: https://ffmpeg.org/ffmpeg-filters.html
- Suno commercial-use rights (read the terms applicable to creation/download dates): https://help.suno.com/en/articles/9601665
- Suno pre-subscription tracks: https://help.suno.com/en/articles/2425729
- Suno downloads and formats: https://help.suno.com/en/articles/13876865
- Yandex AI-content requirements: https://www.yandex.ru/support/music/en/content-requirements/rules
- DistroKid audio accepted formats: https://support.distrokid.com/hc/en-us/articles/360013647753-What-Audio-File-Formats-Can-I-Upload
- DistroKid AI credits: https://support.distrokid.com/hc/en-us/articles/50784235803411-What-Are-AI-Credits
- Essentia models (license caution): https://essentia.upf.edu/models.html
- Essentia licensing: https://essentia.upf.edu/documentation.html
- Demucs maintenance note: https://github.com/facebookresearch/demucs
- DeepFilterNet speech use, not full-mix default: https://github.com/Rikorose/DeepFilterNet

These citations are starting points, not an excuse to assume documentation never changes. If browser access is unavailable, use the given requirements and document external verification as `NOT VERIFIED` rather than claiming to have checked.

# START IMPLEMENTATION NOW

Begin by inspecting your working directory and the available OS/build tools. Create the actual repository and run M0-M1 before producing a status summary. After M1 is implemented and verified, proceed in order through the remaining milestones as available in this execution. Keep progress in a local `IMPLEMENTATION_LOG.md` containing milestone, files changed, exact tests, results, defects, fixes and the next action. Do not stop after writing a plan or README. Only claim completion for artifacts you have actually built and tested.
