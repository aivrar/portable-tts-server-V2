# Authenticated HTTP API

Every user-visible CLI verb and every GUI button except the unauthenticated health check is this API. The gateway is FastAPI inside `linbox-TTS_Server` on port **8300**. The bridge runs inside that WSL distro on **9300** and proxies `/api/`, `/health`, `/static/`, `/docs`, `/openapi.json` for Windows clients. Prefer **9300** from Windows (that is what `tts.cmd` defaults to). Interactive docs: `http://127.0.0.1:9300/docs`. OpenAPI JSON: `http://127.0.0.1:9300/openapi.json`.

## Token / auth

All `/api/*` routes require a persisted token via **one** of:

- Header `X-TTS-API-Token: <token>` (preferred)
- Header `Authorization: Bearer <token>`
- Query `?token=<token>` (used by the GUI's EventSource log stream, which cannot set headers; do not put this in bookmarks)

The GUI fetches audio with the token header and plays a local blob URL, so media URLs do not need a query token.

`OPTIONS` (CORS preflight) skips the token check. CORS allows `http://localhost` and `http://127.0.0.1` on ports 9300, 8300, 9091, 8100.

**Where the token lives**

- `E:\tts_server\output\run\registry\tts_server.json` → `auth.header` + `auth.token`
- `E:\tts_server\output\run\api_token` (plain file)

The token file is created on first startup and reused across restarts. The discovery registry is removed on clean shutdown and republished at startup. To rotate the token, stop the app, remove `output/run/api_token`, and restart, or set `TTS_API_TOKEN`. A 401 means the supplied token does not match the active token.

**Unauthenticated**

- `GET /health` → `{status, message, loaded_models, worker_count}`

## Discovery registry

Read `E:\tts_server\output\run\registry\tts_server.json` first. Use `endpoints.api` (usually `http://127.0.0.1:9300`) and `auth.token`. Never print the token in logs you will share.

Sanitized peer list: `GET /api/peers` (tokens stripped).

Agent helper that refuses non-API paths and refuses to save outside the app:

```powershell
python E:\tts_server\skills\use-tts-server\scripts\tts_api.py GET /api/devices
python E:\tts_server\skills\use-tts-server\scripts\tts_api.py POST /api/tts/kokoro/dryrun --data '@request.json'
```

## Health, ready, live, about

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | No auth |
| GET | `/api/live` | `{ok, status, app}` |
| GET | `/api/ready?model=kokoro&timeout=60` | Without `model`: 200 immediately with worker rollup. With `model`: wait up to `timeout` (0–600, default 0). 503 body on wait timeout is `{"status":"timeout","ready":false,"model":"..."}` — **not** the generic `{detail}` envelope. Branch on HTTP 503. |
| GET | `/api/version` | app, version, api_version, python, platform, git_commit |
| GET | `/api/about` | paths, ports, uptime, GPU, workers, models |
| GET | `/api/diagnostics` | binaries, GPU, HF token, installs, worker health, env hygiene, disk |
| GET | `/api/disk` | per-directory size + free space per mount |
| GET | `/api/env` | HF_HOME, TORCH_HOME, CUDA_VISIBLE_DEVICES, … |
| GET | `/api/capabilities` | High-level contract for peer apps: capability names, categorized endpoints, `artifact_roots` `{output, projects, voices, models, cache}`, registered/loaded models, worker rollup, `policy` (prefer async for long text, reuse ready workers, unload after final artifacts). Distinct from per-model `/api/tts/{model}/capabilities`. |
| GET | `/api/devices` | GPU/CPU list. Re-query immediately before load. |
| GET | `/api/models` | Static catalog: id, name, desc, override, weights_repo, weights_dir, weights_size |
| GET | `/api/models/status` | `{workers, loaded}` per model |
| GET | `/api/config` | UI config: models, defaults, fields, params, param_overrides, tooltips, profiles, override_map, ports, paths |

## Workers

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/api/workers` | list |
| POST | `/api/workers/spawn` | `{model, device?, precision?}` precision `fp32`\|`fp16`\|`bf16` or omit |
| DELETE | `/api/workers/{worker_id}` | graceful /unload then SIGTERM then SIGKILL |
| POST | `/api/workers/{worker_id}/unload` | drop weights, keep process |
| POST | `/api/workers/{worker_id}/restart` | kill + respawn same model/device |
| GET | `/api/workers/{worker_id}/logs?lines=200` | 1–5000, default 200 |
| POST | `/api/models/{model}/scale` | `{count: 0-16, device?}` |
| POST | `/api/models/{model}/load?device=` | spawn if none |
| POST | `/api/models/{model}/unload` | kill all workers for that model |

VRAM: gateway refreshes nvidia-smi, serializes loads per device, rejects estimates that would consume `TTS_SERVER_GPU_RESERVE_GB` (default 1.5). Always `POST /api/models/{model}/unload` in a finally path when scripting.

## Whisper-specific

| Method | Path |
| --- | --- |
| GET | `/api/whisper` |
| POST | `/api/whisper/{size}/load` |
| POST | `/api/whisper/{size}/unload` |

`size`: tiny \| base \| small \| medium \| large.

## Transcription

`POST /api/transcribe` body:

```json
{
  "source": {"kind": "final", "job_id": "<uuid>"},
  "size": "base",
  "word_timestamps": false,
  "language": "en",
  "task": "transcribe"
}
```

`source.kind`: `final` \| `chunk` (needs `index`) \| `edit` (needs `name`) \| `path` (absolute under voices/output/projects). `task` may be `translate` (to English).

`POST /api/transcribe/upload` — multipart `file` ≤ 50MB plus the same fields.

`POST /api/voices/{filename}/transcribe` — caches `<filename>.txt`.

## TTS pipeline

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/api/tts/{model}` | Sync; blocks until assembled |
| POST | `/api/tts/{model}/submit` | Async; **202** `{status:"submitted", job_id, poll_url, output_url, ...}` |
| POST | `/api/tts/{model}/upload` | Multipart sync; `reference_audio` as a file |
| POST | `/api/tts/{model}/cancel` | `{job_id?}` omitted = cancel all running jobs for the model |
| POST | `/api/tts/{model}/dryrun` | Plan only |
| GET | `/api/tts/{model}/voices` | Built-in list + usage notes + extras |
| GET | `/api/tts/{model}/capabilities` | Compact |
| GET | `/api/schema/tts` | Full schema every model |
| GET | `/api/schema/tts/{model}` | One model |

### PipelineTTSRequest body (sync, submit, dryrun)

**Core**

- `text` (required, 1..`TTS_SERVER_MAX_TEXT_CHARS`)
- `voice` — built-in name, reference filename, or absolute path
- `reference_audio` — path, voices/ filename, or base64 ≤ 50MB
- `reference_audios` — JSON list of one to four saved voice filenames or audio paths in speaker order for VibeVoice; each file must be under `voices/`, `output/`, or `projects_output/`. This field is available on the JSON sync, submit, and dry-run routes, not the multipart upload route.
- `reference_audio_name` — original filename if base64
- `reference_text` — transcript of the reference
- `language` default `"en"`
- `mode` default `"cloned"` (XTTS built-in vs cloned)
- `device` e.g. `"cuda:0"`
- `output_format` default `"wav"` — wav \| mp3 \| ogg \| flac \| m4a
- `save_path` — under PROJECTS_OUTPUT / OUTPUT_DIR; the whole job folder lands there

**External delivery**

- `deliver_to` — absolute path, mutually exclusive with `save_path`. Final audio + SRT/timing copied here; work dir purged on success.
- `generate_srt` default false
- `srt_words_per_line` 1–20, default 3
- `srt_size` Whisper size, default `"base"`

**Inference knobs** (optional; omitted fields become that engine's `MODEL_DEFAULTS`)

`speed`, `temperature`, `repetition_penalty`, `top_p` (0–1), `top_k` (1–200), `cfg_scale` (0–10), `exaggeration` (0–2), `cfg_weight` (0–1), `cfg_alpha` (0.5–3.0, Voxtral), `waveform_temperature` (0–3), `seed` (0–99999), `nfe_step` (4–128), `pitch` (-50..50), `volume` (-50..50), `speaker_idx` (≥0), `voice_description` (Parler / Higgs).

**Post-process**

`de_reverb` default 0.0, `de_ess` default 0.0, `skip_post_process` default false, `auto_retry` 0–10.

**Verification**

`verify_whisper` default false, `whisper_model`, `tolerance` default 80.0.

**Audio profile overrides**

`inter_pause_sec`, `front_pad_sec`, `padding_sec`, `trim_db`, `min_silence_ms`, `front_protect_ms`, `end_protect_ms`, `clipping`, `lufs`.

VibeVoice example for two speakers:

```json
{
  "text": "Speaker 1: Welcome.\nSpeaker 2: Glad to be here.",
  "reference_audios": ["host.wav", "guest.wav"]
}
```

Send this JSON to `POST /api/tts/vibevoice` or `/submit`. The Testing tab also supports ordered saved voices for up to four VibeVoice speakers.

### Success shapes

- Sync 200: `{status:"completed", job_id, filename, saved_to, duration_sec, format, audio_base64?, output_url?}`. With `deliver_to`: `delivered_to: {audio, srt?, timing?}`. With `generate_srt`: `srt: {srt_path, line_count, word_count, language}`.
- Async 202: `{status:"submitted", job_id, poll_url, output_url, ...}`.
- Errors: `{detail: "..."}` with 400/401/404/409/413/500/502/503 except `/api/ready` timeout as noted.

Inline base64 is omitted when the file exceeds `TTS_SERVER_MAX_INLINE_AUDIO_BYTES` (default 25MB). Fetch `/api/jobs/{job_id}/output` instead. Canonical artifact ref: `tts://jobs/{job_id}/output`.

Recommended scripted flow:

1. `GET /api/live`
2. `GET /api/setup/status` — require `ready`
3. `GET /api/devices` — confirm free VRAM vs `MODEL_VRAM_ESTIMATE_GB` + 1.5 GiB
4. `GET /api/schema/tts/{model}` and `/api/tts/{model}/voices`
5. `POST /api/tts/{model}/dryrun`
6. `POST /api/tts/{model}/submit`
7. Poll `GET /api/jobs/{job_id}` until terminal
8. `GET /api/jobs/{job_id}/output` or `/output/info`
9. `POST /api/models/{model}/unload` (and whisper if used)

## Jobs

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/jobs` | Query: `model`, `status` (running\|completed\|failed\|cancelled), `since`, `until` ISO-8601, `limit` 1–500 default 50, `offset`. Returns `{jobs, total, limit, offset, filters}`. |
| GET | `/api/jobs/{job_id}` | Full manifest |
| GET | `/api/jobs/{job_id}/manifest` | Manifest + directory listing (size + mtime) |
| GET | `/api/jobs/{job_id}/output` | Stream final audio |
| GET | `/api/jobs/{job_id}/output/info` | `{job_id, filename, path, format, size_bytes, sample_rate, channels, frames, duration_sec, read_error?, artifact}`. `artifact.ref` is `tts://jobs/{id}/output`. Non-WAV that soundfile cannot open: sample_rate/duration_sec null + `read_error`. |
| GET | `/api/jobs/{job_id}/verification` | Per-chunk Whisper report, counts, average similarity |
| GET | `/api/jobs/{job_id}/chunks/{chunk_idx}/audio` | One chunk WAV |
| PUT | `/api/jobs/{job_id}/chunks/{chunk_idx}` | `{text}` 1–50000 chars; resets this + later chunks |
| POST | `/api/jobs/{job_id}/chunks/{chunk_idx}/rerun` | Re-render from that chunk |
| POST | `/api/jobs/{job_id}/recover` | Resume failed/incomplete |
| GET | `/api/jobs/{job_id}/zip` | Whole job directory |
| GET | `/api/jobs/{job_id}/stream?max_seconds=` | SSE `{status, chunks_completed, total_chunks}`; 60–86400, default 3600; closes on terminal/disconnect/timeout |
| POST | `/api/jobs/{job_id}/srt` | See SRT section |
| GET | `/api/jobs/{job_id}/srt` | Download `.srt` (404 if not generated) |
| GET | `/api/jobs/{job_id}/srt/timing` | `{words:[{word,start,end}], text, language}` |
| POST | `/api/jobs/delete` | `{job_ids:[...]}`; refuses running jobs |

## SRT endpoint body

```json
POST /api/jobs/{job_id}/srt
{"words_per_line": 4, "size": "base", "source_guided": true}
```

`words_per_line` 1–20 default 3; `size` Whisper size default base; `source_guided` default **true**. Source-guided uses Whisper as the acoustic clock while keeping the intended script in the SRT; the timing JSON retains uncorrected ASR words. Set `source_guided: false` for captions of only what Whisper heard. Not forced alignment; not mathematically perfect timing. Writes `<stem>.srt` and `<stem>_timing.json` into the job dir and returns the payload inline.

## Projects

| Method | Path |
| --- | --- |
| GET | `/api/projects` |
| GET | `/api/projects/{name}` |
| DELETE | `/api/projects/{name}` |
| GET | `/api/projects/{name}/zip` |

## Voices

| Method | Path |
| --- | --- |
| GET | `/api/voices` |
| POST | `/api/voices/upload` | multipart `file` ≤ 50MB |
| GET | `/api/voices/{filename}/audio` |
| GET | `/api/voices/{filename}/info` |
| POST | `/api/voices/{filename}/rename` | `{new_name}` 1–128 |
| DELETE | `/api/voices/{filename}` |
| POST | `/api/voices/delete` | `{filenames:[...]}` |
| POST | `/api/voices/{filename}/transcribe` | query size, word_timestamps, language, task |

## Setup / install

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/setup/status` | per-model status + `active_installs` |
| GET | `/api/setup/hf-token` | `{saved, masked}` |
| POST | `/api/setup/hf-token` | `{token}`; empty string removes |
| POST | `/api/setup/install/{model}` | or `all`; returns immediately |
| POST | `/api/setup/cancel/{model}` | or `all` |
| DELETE | `/api/setup/{model}` | weights + override dir |

## Audio editor

| Method | Path |
| --- | --- |
| GET | `/api/audio/effects` | manifest + defaults |
| POST | `/api/audio/peaks` | `{source, buckets?}` default 1024 |
| POST | `/api/audio/render` | see below |
| GET | `/api/audio/edits/{job_id}` |
| GET | `/api/audio/edits/{job_id}/{filename}` |
| DELETE | `/api/audio/edits/{job_id}/{filename}` |

Source descriptor (transcribe, peaks, render):

```json
{"kind": "final", "job_id": "<UUID>"}
{"kind": "chunk", "job_id": "<UUID>", "index": 0}
{"kind": "edit",  "job_id": "<UUID>", "name": "edit_v01.wav"}
{"kind": "path",  "path": "/mnt/e/tts_server/voices/me.wav"}
```

Render body:

```json
{
  "source": {"kind": "final", "job_id": "..."},
  "edits": [{"type": "gain", "params": {"db": 3}}],
  "output_name": "podcast_master",
  "output_format": "wav",
  "overwrite": false
}
```

`output_name` and `output_path` are mutually exclusive. `path` sources require `output_path`. Range ops (`cut`, `trim`, `silence`, `fade_in`, `fade_out`) take `start_sec` / `end_sec` and run before full-track effects.

## Logs

| Method | Path |
| --- | --- |
| GET | `/api/logs/stream?since=` | SSE |
| GET | `/api/logs/tail?file=server&lines=200` | file: server \| bridge \| setup \| startup; lines 1–5000 |

## Maintenance

| Method | Path | Body |
| --- | --- | --- |
| POST | `/api/maintenance/gc-jobs` | `{older_than_hours}` 1–8760, default 72 |
| POST | `/api/maintenance/clear-cache` | `{kinds: ["tmp"]}` kinds: tmp, pip, hub, datasets, torch, xdg, modules, logs, all. Also home/xdg-config/xdg-data/xdg-state in some skill examples. Does not touch weights or venv. |
| POST | `/api/maintenance/kill-stale-workers` | workers not in (ready, busy) |
| POST | `/api/maintenance/restart-workers?model=` | optional model filter |
| POST | `/api/maintenance/cleanup-temp?min_age_minutes=30` | orphaned `raw_*.wav`, `assembled_*.wav`, `ref_*.wav`, `tmp*.wav/mp3` |

## Shutdown / UI

| Method | Path |
| --- | --- |
| POST | `/api/shutdown` | graceful full stop |
| GET | `/` | Web UI with token injected; falls back to `/health` if no static UI |
| GET | `/static/index.html` | same |
| GET | `/static/*` | CSS/JS |

## Size limits

- `TTS_SERVER_MAX_TEXT_CHARS` default 100000
- `TTS_SERVER_MAX_CHUNKS` default 500
- `TTS_SERVER_MAX_INLINE_AUDIO_BYTES` default 25MB
- Reference uploads 50MB

## Related pages

- [Windows CLI (`tts.cmd`)](windows-cli.md)
- [Engines catalog](engines-catalog.md)
- [Jobs and projects](jobs-and-projects.md)
- [SRT and Whisper](srt-and-whisper.md)
- [Audio editor usage](audio-editor-usage.md)
