---
name: use-tts-server
description: Operate the Portable TTS Server V2 entirely through its authenticated HTTP API. Use when an agent needs to discover TTS engines or voices, select a live GPU, generate or download speech, clone a voice, create word-timed SRT subtitles, inspect or recover jobs, edit/rerun chunks, or unload models and reclaim memory in its portable app folder.
---

# Use TTS Server

Discover the local API from this copy's registry. Keep artifacts and state inside
the portable app folder; never launch commands in a default WSL distribution.
Paths below use `E:\tts_server` as an example; substitute the actual app root.

## Start with discovery

1. Read `E:\tts_server\output\run\registry\tts_server.json`.
2. Take the API URL and auth header/token from that file without printing the
   token.
3. Call `GET /api/live`, then `GET /api/setup/status`, `GET /api/devices`, and
   `GET /api/workers`.
4. Call `GET /api/schema/tts/{model}` before constructing a request. Its
   `usage`, `example`, parameters, and capability flags explain engine-specific
   speaker/style syntax. Call `GET /api/tts/{model}/voices` for the current
   voice catalog.

If the registry or API is absent, start the app with
`E:\tts_server\Start-TTSServer.ps1`. Do not invoke `TTSServer.exe --help`; the
executable is a GUI launcher, not a harmless CLI command.

## Select and run an engine

1. Require the engine's setup status to be `ready`. Install only one explicit
   model through `POST /api/setup/install/{model}` when installation is part of
   the user's request. Never call the `all` installer implicitly.
2. Re-read `GET /api/devices` immediately before loading. Treat free VRAM as
   dynamic because other apps may load or unload models at any time. Keep at
   least 2 GiB and preferably 20% free after the model's advertised estimate.
   The gateway also refreshes `nvidia-smi` immediately before a load, serializes
   loads per GPU, and rejects estimates that would consume its safety reserve.
3. Load one engine with `POST /api/models/{model}/load?device=...&wait=false`.
   Poll `GET /api/models/status`; a background failure is reported in
   `models[model].loads[].error`.
4. Preview complex requests with `POST /api/tts/{model}/dryrun`. Inspect
   `effective_params`, chunk count, and resolved output path.
5. Submit with `POST /api/tts/{model}/submit`; poll
   `GET /api/jobs/{job_id}` until a terminal status.
6. Resolve the canonical `tts://jobs/{job_id}/output` artifact through
   `GET /api/jobs/{job_id}/output` or `/output/info`.
7. Always unload in a `finally` path with
   `POST /api/models/{model}/unload`. Confirm `GET /api/workers` has no worker
   owned by the completed probe. Unload performs targeted app-file cache
   eviction without flushing caches belonging to other Linbox apps.

Use short, sequential requests. Do not run parallel synthesis, stress tests, or
long text unless the user explicitly asks. Stop when host free RAM falls below
12 GiB or CPU remains above 85% for three samples.

## Generate subtitles and verify speech

For completed audio, call `POST /api/jobs/{job_id}/srt` with
`{"size":"base","words_per_line":4,"source_guided":true}`. Source-guided
mode uses Whisper as the acoustic clock while preserving the intended script;
the timing JSON retains the uncorrected ASR words and text. Set
`source_guided:false` when captions must describe only what Whisper heard.
Both modes write `.srt` plus word-timing JSON. Unload Whisper immediately
afterward through `POST /api/models/whisper/unload`.

For clean speech, leave `de_reverb` omitted or set it to `0`; enable it only
for audibly reverberant output.

## Recover instead of restarting

- Inspect `GET /api/jobs/{job_id}/manifest` and `/verification`.
- Correct text with `PUT /api/jobs/{job_id}/chunks/{chunk_idx}`.
- Rerun one chunk with `POST /api/jobs/{job_id}/chunks/{chunk_idx}/rerun`.
- Resume the first incomplete chunk with `POST /api/jobs/{job_id}/recover`.
- Download a portable project with `GET /api/jobs/{job_id}/zip`.

Read [references/api.md](references/api.md) for compact request examples. Read
[references/models.md](references/models.md) when selecting an engine or using
speaker/style syntax. Read [references/safety.md](references/safety.md) before
installing, loading a large model, or running more than one job.

Use `scripts/tts_api.py` when a shell agent needs authenticated JSON or binary
API access without exposing the token on its command line. The helper refuses
non-API paths and refuses to save responses outside this app.
