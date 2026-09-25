# TTS Server architecture and operating contract

This is the current source-of-truth map for the TTS Server. It describes the
post-refactor application as it exists now; historical audit reports and
pre-fix source copies are not part of the application.

## Boundaries and storage

The host location is the current portable folder on a local Windows drive.
Its WSL name is `TTS-Server-V2-<path hash>` and the registered backing disk
must be `<app>/wsl/ext4.vhdx`. Legacy `linbox-TTS_Server` is accepted only
when already registered to this exact disk. Startup validates containment.

Linux-private state lives under `/opt/tts_server` inside that VHDX:

- `venv/`: shared Python runtime
- `overrides/`: isolated engine-specific dependency trees
- `repos/`: pinned upstream source required by a few engines
- `data/`: model weights and Hugging Face data
- `cache/`: app-owned caches, temporary files, and synthetic home/XDG paths

Windows-visible user state remains in the app tree:

- `output/`: jobs, logs, registry, and runtime metadata
- `projects_output/`: named persistent projects
- `voices/`: reference audio
- `secrets/`: local Hugging Face token
- `cache/`: WebView2 profile data created by the Windows launcher

The app deliberately replaces WSL's inherited `PATH`, home, XDG, package, and
model cache variables. Missing Linux dependencies must fail visibly instead of
falling through to `C:` or another Linbox app.

## Supported startup path

Use `Start-TTSServer.cmd` or `Start-TTSServer.ps1`. The PowerShell preflight
verifies the WSL registration before starting `TTSServer.exe`.

The C# native host starts `bridge.py`, which supervises the FastAPI gateway on default port 8300. `bridge.py` proxies the
Windows WebView on port 9300. The bridge has a path allowlist and the gateway
requires a persisted local `X-TTS-API-Token` for `/api/*`. The token is published in
the app-owned discovery registry and written to `output/run/api_token` for
local tools.

`launcher/` contains the C# native host source. Release dependencies live in
`runtime/`: self-contained launcher, Windows Python, WebView2, and the clean
`linux-rootfs.tar.gz` used for first import. The live VHD is the installed state.
`tools/configure_runtime.py` refreshes host paths before every launch.
`tools/build_portable.py` builds a fresh isolated release image.

## Source map

- `server/tts_api_server.py`: API gateway, orchestration, jobs, setup, SRT, and
  maintenance endpoints
- `server/tts_worker.py`: one-model inference worker and model adapters
- `server/config.py`: storage contract, environment containment, model catalog,
  capabilities, parameters, and estimates
- `server/worker_manager.py` and `worker_registry.py`: process lifecycle, GPU
  selection, load serialization, and targeted file-cache reclaim
- `server/job_manager.py`: atomic manifests, persistent indexing, recovery, and
  retention
- `server/text_utils.py`: general and engine-aware text chunking
- `server/audio_processing.py`, `audio_assembler.py`, and `audio_editor.py`:
  post-processing, final assembly/conversion, and non-destructive edits
- `server/install_model.sh`: staged engine installs and selected/pinned weight
  downloads
- `server/static/`: the complete WebView UI; every script is loaded by
  `server/static/index.html`
- `tts.py` and `tts.cmd`: the Windows CLI over the authenticated HTTP API
- `skills/use-tts-server/`: the agent-facing operational skill

## Worker and memory model

The gateway stays light. Each loaded TTS engine or Whisper runs in its own
process, and unload terminates the full worker process group. GPU availability
is refreshed immediately before a load, loads are serialized per device, and a
VRAM reserve is enforced.

Cleanup is app-scoped. The gateway trims its own allocator and uses
`posix_fadvise` only on files belonging to the unloaded model. The application
does not expose VM-wide `drop_caches` or memory compaction because the WSL
instance can contain unrelated running Linbox services.

## Models and dependencies

`MODEL_SETUP` and `MODEL_OVERRIDE_MAP` in `server/config.py` are authoritative.
Large or conflicting runtimes are isolated in named override directories.
Installers build replacements in staging directories and swap them into place
only after success. Weight downloads are limited to the files required for
inference where upstream repositories contain training or duplicate assets.

Model removal deletes the model's declared weights and override. A shared
override, such as Coqui for Bark/XTTS/VITS, remains while another registered
engine still needs it.

## Jobs, projects, and subtitles

Jobs use atomic JSON manifests. Temporary jobs are subject to retention;
named projects are persistent until explicitly deleted. Startup indexes both
locations and can recover from the first incomplete or failed chunk.

SRT generation uses Whisper word timing. Source-guided mode preserves the
intended script while using recognized speech as the acoustic clock; it is not
forced alignment and should not be described as mathematically perfect timing.
ASR-only mode captions only what Whisper recognized.

## Generated material

The following are disposable and must not be treated as source:

- `__pycache__/`, `*.pyc`, and `.pytest_cache/`
- `.playwright-mcp/` captures
- agent scratch folders and pre-fix backups
- files under `output/run/` other than as live runtime diagnostics

Do not delete `output/`, `projects_output/`, `voices/`, model data, overrides,
or the VHDX merely because they are generated: they contain current user or
installed-engine state.
