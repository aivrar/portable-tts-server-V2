# TTS Server API Reference — Complete Endpoint Map

All `/api/*` routes require an API token via one of:
- `X-TTS-API-Token: <token>` header
- `Authorization: Bearer <token>` header
- `?token=<token>` query string (used by the GUI's EventSource log stream, which cannot set a header; audio uses the token header)

Token lives at `RUN_DIR/api_token`. Token check is bypassed for `OPTIONS` (CORS preflight).

CORS: gateway allows `http://localhost:{9300, 8300, 9091, 8100}` and their `127.0.0.1` equivalents.

---

## 1. Health & Discovery

### `GET /health`
No auth. Returns `{status, message, loaded_models, worker_count}`.

### `GET /api/live`
Lightweight liveness probe (auth-gated like all other `/api/*` routes). Returns `{ok, status, app}`.

### `GET /api/ready`
- **Query**: `model` (optional, must be known), `timeout` (seconds, 0-600, default 0)
- Without `model`: 200 immediately with worker rollup.
- With `model`: waits up to `timeout` seconds for a ready worker; 200 when ready or 503 on timeout.
- **503 timeout body** (model wait): `{"status": "timeout", "ready": false, "model": "<model>"}`. This is intentionally NOT the generic `{"detail": ...}` error envelope — clients polling this endpoint should branch on the 503 status rather than reading `detail`.

### `GET /api/peers`
No params. Returns peers from the cross-app discovery registry (sanitized — auth tokens stripped).

### `GET /api/models`
No params. Returns the static model catalog: `[{id, name, desc, override, weights_repo, weights_dir, weights_size}, ...]`.

### `GET /api/models/status`
No params. Returns per-model `{workers: [...], loaded: bool}` rollup.

### `GET /api/devices`
No params. Returns detected GPU/CPU devices.

### `GET /api/version`
No params. Returns `{app, version, api_version, python, platform, git_commit}`.

### `GET /api/about`
No params. Full system snapshot: paths, ports, uptime, GPU, worker rollup, registered/loaded models.

### `GET /api/diagnostics`
No params. End-to-end self-check: system binaries, GPU, HF token, install status, worker health, env-var hygiene, disk free.

### `GET /api/disk`
No params. Per-directory size + free space on each mount.

### `GET /api/env`
No params. The subset of env vars the server cares about (HF_HOME, TORCH_HOME, CUDA_VISIBLE_DEVICES, etc.).

### `GET /api/capabilities`
No params. The machine-readable contract intended for peer apps and agents (distinct from the per-model `GET /api/tts/{model}/capabilities` route). Returns:
- `capabilities[]` — supported high-level capability names.
- `status_endpoints[]`, `model_endpoints[]`, `generation_endpoints[]`, `destructive_endpoints[]` — categorized endpoint lists.
- `artifact_roots{}` — `{output, projects, voices, models, cache}` filesystem roots.
- `models{}` — `{registered, loaded}` model lists.
- `loaded_resources.workers` — currently loaded worker rollup.
- `policy{}` — `{prefer_async_submit_for_long_text, reuse_ready_workers_for_batches, unload_workers_after_final_artifacts}` guidance for callers.

---

## 2. Workers

### `GET /api/workers`
No params. Returns `[{worker_id, model, port, device, status, ...}, ...]`.

### `POST /api/workers/spawn`
- **Body** (`SpawnRequest`): `model` (required, must be known), `device` (optional, e.g. `"cuda:0"`), `precision` (optional, `"fp32"|"fp16"|"bf16"` or omit for auto)

### `DELETE /api/workers/{worker_id}`
- **Path**: `worker_id`
- Kills the worker process (graceful HTTP /unload, then SIGTERM, then SIGKILL).

### `POST /api/workers/{worker_id}/unload`
- **Path**: `worker_id`
- Tells the worker to drop its model from VRAM but keep the process alive.

### `POST /api/workers/{worker_id}/restart`
- **Path**: `worker_id`
- Kill + respawn with same model + device.

### `GET /api/workers/{worker_id}/logs`
- **Path**: `worker_id`
- **Query**: `lines` (1-5000, default 200)
- Tails the worker's log file.

### `POST /api/models/{model}/scale`
- **Path**: `model`
- **Body** (`ScaleRequest`): `count` (0-16), `device` (optional)
- Adjusts worker count up or down for one model.

### `POST /api/models/{model}/load`
- **Path**: `model`
- **Query**: `device` (optional)
- Spawns a worker if one isn't already loaded.

### `POST /api/models/{model}/unload`
- **Path**: `model`
- Kills all workers for the model.

---

## 3. Whisper-specific

### `GET /api/whisper`
No params. Returns available sizes, default, loaded sizes, worker IDs.

### `POST /api/whisper/{size}/load`
- **Path**: `size` (tiny|base|small|medium|large)
- Reserves a Whisper worker, loads the requested size, and returns after it is available. Downloads its weights if necessary.

### `POST /api/whisper/{size}/unload`
- **Path**: `size`
- Unloads only the requested size from each Whisper worker. Other cached sizes and worker processes remain available.

---

## 4. Transcription

### `POST /api/transcribe`
- **Body** (`TranscribeRequestBody`):
  - `source` (required, dict): `{kind: "final"|"chunk"|"edit"|"path", job_id, [index|name|path]}`
  - `size` (default `"base"`)
  - `word_timestamps` (default `false`)
  - `language` (optional, ISO 639-1, e.g. `"en"`, `"ja"`)
  - `task` (default `"transcribe"`, or `"translate"` for translate-to-English)

### `POST /api/transcribe/upload`
- **Multipart form**:
  - `file` (required, audio file ≤ 50MB)
  - `size`, `word_timestamps`, `language`, `task` (same defaults as above)

### `POST /api/voices/{filename}/transcribe`
- **Path**: `filename` (must exist in VOICE_DIR)
- **Query**: `size`, `word_timestamps`, `language`, `task`
- Caches plain transcript as `<filename>.txt` sidecar.

---

## 5. Jobs

### `GET /api/jobs`
- **Query**:
  - `model` (filter by model id)
  - `status` (`running` | `completed` | `failed` | `cancelled`)
  - `since`, `until` (ISO-8601 timestamp inclusive filter)
  - `limit` (1-500, default 50)
  - `offset` (default 0)
- Returns `{jobs, total, limit, offset, filters}`.

### `GET /api/jobs/{job_id}`
Returns the full job manifest (parameters, chunks array with text+timing+verification, status, paths).

### `GET /api/jobs/{job_id}/manifest`
Same job manifest plus a directory listing (size + mtime per file).

### `GET /api/jobs/{job_id}/output`
Streams the final assembled audio file.

### `GET /api/jobs/{job_id}/output/info`
Audio metadata for a completed job's final audio (without streaming the bytes). Full shape:
- Top-level: `{job_id, filename, path, format, size_bytes}`.
- On a readable file: also `{sample_rate, channels, frames, duration_sec}`.
- On a non-WAV file soundfile cannot open (e.g. some mp3/m4a): `sample_rate` and `duration_sec` are `null` and a `read_error` string is added.
- Always includes an `artifact` object: `{path, ref, media_type: "audio", mime, duration, dimensions: null, metadata: {job_id, filename, format, size_bytes, sample_rate, channels, frames}}`.

**`tts://` artifact ref scheme**: the `artifact.ref` field is the canonical handle peer apps/agents use to reference a job's output — `tts://jobs/{job_id}/output`. Resolve it by fetching `GET /api/jobs/{job_id}/output` (audio bytes) or this `/output/info` endpoint (metadata).

### `GET /api/jobs/{job_id}/verification`
Per-chunk Whisper verification report + summary counts + average similarity.

### `GET /api/jobs/{job_id}/chunks/{chunk_idx}/audio`
Streams a single chunk's WAV.

### `PUT /api/jobs/{job_id}/chunks/{chunk_idx}`
- **Body** (`ChunkEditRequest`): `text` (1-50000 chars)
- Edits one chunk's text and resets that chunk + all later chunks for recovery.

### `POST /api/jobs/{job_id}/chunks/{chunk_idx}/rerun`
Resets one chunk (and everything after it) and triggers recovery.

### `POST /api/jobs/{job_id}/recover`
Resumes a failed/incomplete job from its last completed chunk.

### `GET /api/jobs/{job_id}/zip`
Streams a ZIP of the entire job directory.

### `GET /api/jobs/{job_id}/stream`
- **Query**: `max_seconds` (60-86400, default 3600)
- SSE stream of `{status, chunks_completed, total_chunks}` deltas. Closes on terminal state, disconnect, or timeout.

### `POST /api/jobs/{job_id}/srt`
- **Body** (`JobSrtRequest`): `words_per_line` (1-20, default 3), `size` (whisper size, default `"base"`)
- Runs Whisper on the final audio, writes `<stem>.srt` and `<stem>_timing.json` into the job dir, returns the full payload inline.

### `GET /api/jobs/{job_id}/srt`
Downloads a previously generated `.srt`. 404 if not yet generated.

### `GET /api/jobs/{job_id}/srt/timing`
Downloads the word-level timing JSON sidecar. 404 if SRT generation has not been run yet. Body shape: `{words: [{word, start, end}, ...], text, language}`.

### `POST /api/jobs/delete`
- **Body** (`DeleteJobsRequest`): `job_ids: [string]`
- Bulk-delete; refuses to delete running jobs.

---

## 6. TTS Pipeline (the main endpoint)

### `POST /api/tts/{model}`
Sync TTS. Blocks until pipeline finishes.

### `POST /api/tts/{model}/submit`
Async TTS. Returns 202 with `{job_id, poll_url, output_url, ...}` immediately.

### `POST /api/tts/{model}/upload`
Multipart variant — accepts a reference audio file directly without base64.

### `POST /api/tts/{model}/cancel`
- **Body** (`CancelRequest`): `job_id` (optional). If omitted, cancels all running jobs for the model.

### `POST /api/tts/{model}/dryrun`
Same body as `/api/tts/{model}`. Returns the chunk plan, save_path resolution, runtime estimate — without occupying a worker or creating a job.

### **Body** (`PipelineTTSRequest`) — accepted by `tts_model`, `tts_model_submit`, and `tts_model_dryrun`:

**Core**
- `text` (required, 1 to `TTS_SERVER_MAX_TEXT_CHARS`)
- `voice` (optional — built-in voice name, reference audio filename, or absolute path)
- `reference_audio` (optional — path, filename in VOICE_DIR, or base64-encoded inline audio ≤ 50MB)
- `reference_audios` (optional JSON list of one to four saved voice filenames or paths under the allowed audio roots, in VibeVoice speaker order)
- `reference_audio_name` (optional — original filename if base64 inline)
- `reference_text` (optional — transcript of reference audio)
- `language` (default `"en"`)
- `mode` (default `"cloned"` — used by XTTS for built-in vs cloned)
- `device` (optional — `"cpu"`, `"cuda"` (canonicalized to `"cuda:0"`), or `"cuda:N"`)
- `worker_id` (optional — select a specific worker matching the model/device; busy workers are queued)
- `output_format` (default `"wav"` — wav|mp3|ogg|flac|m4a)
- `save_path` (optional — output path under PROJECTS_OUTPUT/OUTPUT_DIR; whole job folder lands there)

**External delivery**
- `deliver_to` (optional, absolute path — mutually exclusive with `save_path`. Final audio + SRT/timing get copied here; work dir is purged on success)
- `generate_srt` (default `false`)
- `srt_words_per_line` (1-20, default 3)
- `srt_size` (whisper size, default `"base"`)

**Inference knobs** (all optional, bounds enforced)
- `speed` (engine default; 1.0 when the engine has no override)
- `temperature` (engine-specific tuned default)
- `repetition_penalty` (engine-specific tuned default)
- `top_p` (0-1)
- `top_k` (1-200)
- `cfg_scale` (0-10)
- `exaggeration` (0-2)
- `cfg_weight` (0-1)
- `cfg_alpha` (0.5-3.0 — Voxtral flow-matching guidance scale)
- `waveform_temperature` (0-3)
- `seed` (0-99999)
- `nfe_step` (4-128)
- `pitch` (-50 to 50)
- `volume` (-50 to 50)
- `speaker_idx` (≥ 0)
- `voice_description` (string — natural language voice prompt for Parler or Higgs)

**Post-processing**
- `de_reverb` (default 0.0; opt in only for reverberant output)
- `de_ess` (default 0.0)
- `skip_post_process` (default false)
- `auto_retry` (0-10)

**Verification**
- `verify_whisper` (default false)
- `whisper_model` (size override)
- `tolerance` (default 80.0)

**Audio profile overrides** (all optional, override model defaults)
- `inter_pause_sec`, `front_pad_sec`, `padding_sec`, `trim_db`, `min_silence_ms`, `front_protect_ms`, `end_protect_ms`, `clipping`, `lufs`

### **Multipart form** (`/api/tts/{model}/upload`):
Same scalar fields as above as form fields, plus `reference_audio` as a file upload (multipart) instead of base64. The JSON-only `reference_audios` list is not accepted here.

---

## 7. Per-model TTS info

### `GET /api/tts/{model}/voices`
Built-in voice list + per-model usage notes + extras (e.g. Dia's example syntax).

### `GET /api/tts/{model}/capabilities`
Compact summary: name, capabilities, accepted params, fields, voices, output formats.

### `GET /api/schema/tts`
Full schema for every model (defaults, ranges, capabilities, accepted params).

### `GET /api/schema/tts/{model}`
Same but for one model.

---

## 8. Audio editor

### `GET /api/audio/effects`
No params. Returns the effect manifest + default parameters.

### `POST /api/audio/peaks`
- **Body** (`AudioPeaksRequest`): `source` (dict — see source descriptor below), `buckets` (default 1024)
- Returns waveform min/max bucket data.

### `POST /api/audio/render`
- **Body** (`AudioRenderRequest`):
  - `source` (dict — see below)
  - `edits` (list of effect dicts, default `[]`)
  - `output_name` (optional — under `<job_dir>/edits/`)
  - `output_path` (optional — absolute under safe roots)
  - `output_format` (default `"wav"`)
  - `overwrite` (default false)
- `output_name` and `output_path` are mutually exclusive. A "path" source requires `output_path`.

### `GET /api/audio/edits/{job_id}`
Lists edit files in `<job_dir>/edits/`.

### `GET /api/audio/edits/{job_id}/{filename}`
Streams one edit file.

### `DELETE /api/audio/edits/{job_id}/{filename}`
Deletes one edit file.

### **Source descriptor** (used by transcribe, peaks, render):
- `{kind: "final", job_id}`
- `{kind: "chunk", job_id, index: int}`
- `{kind: "edit", job_id, name: string}`
- `{kind: "path", path: string}` — absolute, must be under VOICE_DIR / OUTPUT_DIR / PROJECTS_OUTPUT

---

## 9. Projects

### `GET /api/projects`
No params. Lists subdirectories under PROJECTS_OUTPUT with job counts and mtime.

### `GET /api/projects/{name}`
Jobs and files inside one project directory.

### `DELETE /api/projects/{name}`
Deletes the project dir and purges every job under it.

### `GET /api/projects/{name}/zip`
Streams a ZIP of the project.

---

## 10. Voices

### `GET /api/voices`
No params. Lists files in VOICE_DIR with their cached transcription sidecar (if present).

### `POST /api/voices/upload`
- **Multipart**: `file` (audio ≤ 50MB)

### `GET /api/voices/{filename}/audio`
Streams the audio file.

### `GET /api/voices/{filename}/info`
Metadata: size, sample_rate, channels, frames, duration, format, transcription.

### `POST /api/voices/{filename}/rename`
- **Body** (`VoiceRenameRequest`): `new_name` (1-128 chars)
- Renames the audio + its `.txt` sidecar.

### `DELETE /api/voices/{filename}`
Deletes voice + sidecar.

### `POST /api/voices/delete`
- **Body**: `filenames: [string]`
- Bulk delete.

### `POST /api/voices/{filename}/transcribe`
See section 4.

---

## 11. Setup / install

### `GET /api/setup/status`
No params. Per-model install status (`not_installed`, `partial`, `packages_only`, `ready`, `installing`) plus `active_installs` showing whether an all-model install is active and which concrete model ids are currently being installed.

### `GET /api/setup/hf-token`
No params. Returns `{saved, masked}`.

### `POST /api/setup/hf-token`
- **Body** (`HFTokenRequest`): `token` (empty string removes it)

### `POST /api/setup/install/{model}`
- **Path**: `model` (or `"all"` to install missing/not-ready models sequentially in the background)
- Returns immediately; progress streams via the log endpoints.

### `POST /api/setup/cancel/{model}`
- **Path**: `model` (or `"all"` to cancel any active setup installs)
- Requests cancellation of active installer subprocesses and returns the stopped process rollup.

### `DELETE /api/setup/{model}`
- **Path**: `model`
- Removes weights + override dir (does NOT pip-uninstall base-venv packages).

---

## 12. Config

### `GET /api/config`
No params. Returns models, defaults, fields, params, param_overrides, tooltips, profiles, override_map, ports, paths.

---

## 13. Logs

### `GET /api/logs/stream`
- **Query**: `since` (timestamp, optional)
- SSE stream of live log records. EventSource-compatible.

### `GET /api/logs/tail`
- **Query**: `file` (one of the persistent log files; default `"server"`), `lines` (1-5000, default 200)
- Returns the last N lines of one of the persistent log files.

---

## 14. Maintenance

### `POST /api/maintenance/gc-jobs`
- **Body** (`GcJobsRequest`): `older_than_hours` (1 to 8760, default 72)
- Deletes jobs older than the cutoff.

### `POST /api/maintenance/clear-cache`
- **Body** (`ClearCacheRequest`): `kinds: [string]` (any of `tmp`, `pip`, `hub`, `datasets`, `torch`, `xdg`, `modules`, `logs`, or `"all"` — default `["tmp"]`)
- Wipes selected cache trees; reports bytes freed per kind. **Does not touch model weights or the venv.**

### `POST /api/maintenance/kill-stale-workers`
No params. Kills workers not in `(ready, busy)` state.

### `POST /api/maintenance/restart-workers`
- **Query**: `model` (optional — restrict to one model)
- Kills all matching workers; next request triggers fresh spawns.

### `POST /api/maintenance/cleanup-temp`
- **Query**: `min_age_minutes` (default 30)
- Sweeps orphaned scratch files (`raw_*.wav`, `assembled_*.wav`, `ref_*.wav`, `tmp*.wav/mp3`) older than the cutoff.

---

## 15. Shutdown / UI

### `POST /api/shutdown`
No params. Graceful: unload all workers → unpublish discovery → tell the bridge
to suppress backend restart and exit → acknowledge the UI → invoke the native
window close. The launcher exit is verified; force-close is used only after
backend cleanup if graceful close is ignored.

### `GET /`
Serves the Web UI (`static/index.html` with the API token injected). Falls back to `/health` if no static UI is built.

### `GET /static/index.html`
Same as `/` (with token injection).

### `/static/*`
Mounted static file server.

---

## Common response patterns

- **Errors**: `{"detail": "string"}` with HTTP status 400/401/404/409/413/500/502/503.
- **Async submit**: 202 with `{status: "submitted", job_id, poll_url, output_url, ...}`.
- **Sync TTS completion**: 200 with `{status: "completed", job_id, filename, saved_to, duration_sec, format, audio_base64?, output_url?}`. With `deliver_to`: includes `delivered_to: {audio, srt?, timing?}`. With `generate_srt`: includes `srt: {srt_path, line_count, word_count, language}`.

## Size & rate limits

Set via env vars:
- `TTS_SERVER_MAX_TEXT_CHARS` (default 100000)
- `TTS_SERVER_MAX_CHUNKS` (default 500)
- `TTS_SERVER_MAX_INLINE_AUDIO_BYTES` (default 25MB — base64 audio in sync response is omitted above this)
- Reference audio uploads capped at 50MB regardless.
