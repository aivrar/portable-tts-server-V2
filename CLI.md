# TTS Server CLI & API guide

This document covers the full CLI/API surface for controlling the TTS server
from another local app (the orchestrator) or from the command line.

## Discovery & auth

The TTS server publishes a discovery file on startup so the bundled CLI and
approved peer apps can find it without hard-coding ports. The file stays in
the current portable TTS app folder and is never published to Windows AppData.

**Registry location**

| Context | Path |
| ------- | ---- |
| Windows | `E:\tts_server\output\run\registry\tts_server.json` |
| WSL2 | `/mnt/e/tts_server/output/run/registry/tts_server.json` |

These are two views of the same NTFS file. The server sets
`APPHUB_REGISTRY_DIR` to this directory; the bundled `tts.py` resolves it
relative to its own app folder. A controlled test can override the CLI path
with `TTS_REGISTRY_PATH`.

**Registry file contents**

```json
{
  "name": "tts_server",
  "version": "1.0.0",
  "pid": 12345,
  "started_at": "2026-05-05T07:30:00Z",
  "app_dir": "E:\\tts_server",
  "endpoints": {
    "api": "http://127.0.0.1:9300",
    "gateway": "http://127.0.0.1:8300",
    "openapi": "http://127.0.0.1:9300/openapi.json",
    "docs": "http://127.0.0.1:9300/docs"
  },
  "auth": {
    "header": "X-TTS-API-Token",
    "token": "..."
  },
  "extra": {
    "models": ["bark", "kokoro", "xtts", "..."],
    "default_device": "cuda:0"
  }
}
```

The file is created on startup, replaced atomically, and removed on clean
shutdown. A Windows reader does not apply `OpenProcess` to the recorded PID
because WSL PIDs belong to a different PID namespace.

**For the orchestrator (Python)**

```python
import json, requests
reg = json.load(open(
    r"E:\tts_server\output\run\registry\tts_server.json"))
url   = reg["endpoints"]["api"]
token = reg["auth"]["token"]
r = requests.post(f"{url}/api/tts/kokoro",
                  headers={"X-TTS-API-Token": token},
                  json={"text": "hello world", "save_path": "demo/hello"})
```

**Or skip the bookkeeping and shell out to the CLI**

```python
import subprocess
subprocess.check_call([
    r"E:\tts_server\tts.cmd", "tts", "kokoro",
    "--text", "hello world",
    "--save-path", "demo/hello",
])
```

The CLI auto-discovers URL + token from the registry. The orchestrator
never has to know about either.

## CLI install

The CLI is at `E:\tts_server\tts.py` (Python script) with a Windows wrapper
at `E:\tts_server\tts.cmd`. Stdlib only — no pip install needed. The complete release includes Windows Python under `runtime/python`; paths here use `E:\tts_server` as an example. Run the
`.cmd` from cmd/PowerShell, or the `.py` directly.

**Discovery order for `--url` and `--token`** (highest priority first):
1. CLI flags: `--url http://...`, `--token ...`
2. Env vars: `TTS_API_URL`, `TTS_API_TOKEN`
3. Discovery registry file
4. Local file: `<app_dir>/output/run/api_token` (token only; URL defaults)
5. Defaults: `http://127.0.0.1:9300` (bridge)

**Output**: human-readable text by default. Pass `--json` (anywhere on the
command line, before or after the verb) for machine-readable JSON.

If you need to pass the literal string `--json`, `--url`, or `--token` as an
argument value, use `=` form (`--text=--json`) or place a `--` sentinel
before it to stop flag-hoisting (`tts kokoro -- --text "--json starts here"`).

**Exit codes**: `0` success, `1` HTTP error from server, `2` server
unreachable / discovery failed, `3` wait-ready timeout, `4` install/job
wait timeout, `5` job ended in failed state, `130` interrupted (Ctrl-C).

## Common workflows

### One-shot generate, save audio to file

```cmd
tts.cmd tts kokoro --text "hello world" --out hello.wav
```

### Async submit + poll (orchestrator pattern)

```cmd
:: returns job_id immediately
tts.cmd --json tts xtts --text "..." --voice "Daisy Studious" ^
        --save-path projects/foo/intro --async > submit.json

:: extract job_id, poll until done
for /f "tokens=*" %i in ('jq -r .job_id submit.json') do tts.cmd jobs wait %i

:: download finished audio (already saved if --save-path was given)
tts.cmd jobs output <job_id> --out final.wav
```

### Wait for the server (and a model) to come up

```cmd
:: block until server up and a kokoro worker is loaded; exit 3 on timeout
tts.cmd wait-ready --model kokoro --timeout 60
```

### Install a model and wait

```cmd
tts.cmd install xtts --wait --timeout 600
```

### Upload a reference voice and use it

```cmd
tts.cmd voices upload C:\path\to\my-voice.wav
tts.cmd tts xtts --text "..." --ref C:\path\to\my-voice.wav --out out.wav
```

### Stream live logs (filter to one model)

```cmd
tts.cmd logs follow --filter "XTTS"
```

### Batch generation from a JSON spec

`batch.json`:
```json
{
  "model": "kokoro",
  "wait": false,
  "jobs": [
    {"text": "First line", "save_path": "myproj/line_001"},
    {"text": "Second line", "save_path": "myproj/line_002"},
    {"text": "Third line", "voice": "af_bella", "save_path": "myproj/line_003"}
  ]
}
```

```cmd
tts.cmd --json batch batch.json
```

Returns one result per job (each with its async `job_id`). Poll each with
`tts.cmd jobs wait`.

### Discover what other local apps are running

```cmd
tts.cmd peers --local         :: from filesystem (no server roundtrip)
tts.cmd peers                 :: via /api/peers endpoint
```

### Inspect a model's full param schema before submitting

```cmd
tts.cmd schema xtts                :: human-readable: defaults, ranges, capabilities
tts.cmd --json schema xtts > xtts-schema.json
tts.cmd capabilities edge          :: compact: which params/voices does Edge accept?
```

### Plan a TTS request without running inference

```cmd
:: How many chunks would this text become? Where would it save?
tts.cmd dryrun xtts --text-file long.txt --save-path myproj/scene_01

:: Then submit asynchronously
tts.cmd --json tts xtts --text-file long.txt --save-path myproj/scene_01 --async
```

### Stream live progress for one job

```cmd
:: Returns text lines like "[running] chunks=5/12" as the pipeline advances.
tts.cmd jobs stream <job_id>
```

### Self-check the server (and infrastructure)

```cmd
tts.cmd diagnose            :: ffmpeg, GPU, HF token, model installs, disk free
tts.cmd disk                :: per-directory size + mount free space
tts.cmd env                 :: are caches actually pointing inside /opt/tts_server?
tts.cmd logs tail --lines 500
```

### Maintenance from the outside (no SSH needed)

```cmd
:: Trim job history older than 7 days
tts.cmd maintenance gc-jobs --older-than 168

:: Clear the HF blob cache and pip wheels (model weights are NOT deleted)
tts.cmd maintenance clear-cache --kind hub --kind pip

:: Stuck worker stuck in 'loading'? Sweep them.
tts.cmd maintenance kill-stale

:: Force every model to re-load on next request (handy after package updates)
tts.cmd maintenance restart-workers
```

### Bundle a job or project for handoff

```cmd
tts.cmd jobs zip <job_id> --out my-job.zip
tts.cmd project zip myproject --out myproject.zip
tts.cmd jobs manifest <job_id>          :: full job.json + file listing
```

### Apply effects to a generated job (the audio editor)

```cmd
:: List available effects + their default parameters
tts.cmd audio effects

:: Render the final file of a job through a chain. Each --effect takes
:: TYPE[:k=v,k=v]. Defaults from `audio effects` apply for omitted params.
tts.cmd audio render final:<job_id> ^
  --effect gain:db=3 ^
  --effect eq_highpass:cutoff_hz=120 ^
  --effect compressor:threshold_db=-18,ratio=4 ^
  --effect normalize_lufs:target_lufs=-16 ^
  --output-name "podcast_master" --format mp3

:: Same idea, but the chain comes from a JSON file (one entry per effect).
tts.cmd audio render chunk:<job_id>:5 --chain-file chain.json --format flac

:: Render a free file outside the job system. Source must be under
:: VOICE_DIR / OUTPUT_DIR / PROJECTS_OUTPUT and output_path under those too.
:: Windows paths are auto-translated to /mnt/<drive>/ so the WSL server
:: can read them — pass them as you'd write them in Explorer.
tts.cmd audio render path:E:\tts_server\voices\me.wav ^
  --effect pitch:semitones=-2 ^
  --output-path E:\tts_server\projects_output\me_lower.wav

:: List, fetch and delete edits stored under <job_dir>/edits/
tts.cmd audio edits <job_id>
tts.cmd audio edit-get <job_id> podcast_master.mp3 --out C:\out\final.mp3
tts.cmd audio edit-delete <job_id> podcast_master.mp3
```

A `chain.json` file is a list of `{type, params}` objects, e.g.:

```json
[
  {"type": "de_reverb", "params": {"strength": 0.5}},
  {"type": "eq_shelf_high", "params": {"cutoff_hz": 4000, "gain_db": 2}},
  {"type": "compressor", "params": {"threshold_db": -18, "ratio": 4}},
  {"type": "normalize_lufs", "params": {"target_lufs": -16}}
]
```

Range ops (`cut`, `trim`, `silence`, `fade_in`, `fade_out`) accept
`start_sec` / `end_sec` in seconds and run before any full-track effects
in the chain. Effect names + parameter keys match `audio effects`.

## Verb reference

```
tts.cmd <verb> [args]   (run with --help on any verb)

Discovery / status
  health                  Server reachable? loaded models? worker count?
  wait-ready              Block until server (and optional --model) ready
  config [key]            Server config (whole config or one dotted key)
  models [--installed --loaded]
                          List models with install + load status
  devices                 GPUs / CPU + active workers per device
  status [--watch]        Combined install + worker view (auto-refresh w/ --watch)
  token                   Print auto-discovered API token
  peers [--local]         List apps in the discovery registry

Install / removal
  install <model|all> [--wait --timeout 3600]
  remove <model>
  hf-token get | set [token|-]

Workers
  workers list
  workers spawn <model> [--device cuda:1] [--precision fp16]
  workers kill <worker_id>
  workers scale <model> <count> [--device]
  model load <model> [--device]
  model unload <model>

Voices (reference audio files)
  voices list
  voices upload <file>
  voices delete <name>
  voices transcribe <name> [--size base]
  voices download <name> [--out FILE]

Whisper
  whisper info | load <size> | unload <size>

TTS generation
  tts <model> [--text "..." | --text-file F | <stdin>]
              [--out FILE]              # save audio locally (else inline base64)
              [--save-path SUBDIR/NAME] # server-side save under projects_output/
              [--format wav|mp3|ogg|flac|m4a]
              [--ref FILE] [--voice NAME] [--reference-text "..."]
              [--language en] [--device cuda:1]
              [--speed 1.0] [--temperature 0.7] [--seed N]
              [--top-p ...] [--top-k ...] [--cfg-scale ...]
              [--exaggeration ...] [--cfg-weight ...]
              [--nfe-step ...] [--pitch ...] [--volume ...]
              [--de-reverb 0.7] [--de-ess 0.0]
              [--verify-whisper] [--whisper-model base] [--tolerance 80]
              [--auto-retry 3] [--no-postprocess]
              [--async]                  # don't block, just return job_id
              [--request-timeout 1800]

Jobs
  jobs list [--status running|completed|failed] [--limit N]
  jobs get <id>
  jobs chunks <id>
  jobs output <id> [--out FILE]          # download final assembled audio
  jobs chunk-audio <id> <idx> [--out FILE]
  jobs cancel <id>
  jobs recover <id>                      # resume failed/incomplete job
  jobs delete <id...>
  jobs edit-chunk <id> <idx> --text "..."
  jobs wait <id> [--timeout 1800] [--interval 2]
  jobs manifest <id>                     # full job.json + file listing
  jobs zip <id> [--out FILE]             # download whole job dir as .zip
  jobs stream <id> [--timeout 1800]      # SSE stream of progress
  jobs rerun-chunk <id> <idx>            # re-render a single chunk

Projects
  projects                               # list folders under projects_output/
  project get <name>                     # files + jobs in one project
  project zip <name> [--out FILE]        # whole project as .zip
  project delete <name> --yes            # irreversible

Logs
  logs follow [--filter REGEX] [--reconnect]
  logs tail [--lines N] [--file server|bridge|setup|startup]

Batch
  batch <file.json>       # see "Batch generation" above

Audio editor (post-process effects)
  audio effects                          # list effects + default params
  audio render <source> [--effect TYPE:k=v,...] ...
                                         # apply a chain, save the result
       <source>: final:JOB | chunk:JOB:N | edit:JOB:NAME | path:/abs/file.wav
       --effect           repeatable (e.g. --effect gain:db=3)
       --chain-file FILE  load chain from JSON
       --output-name NAME save under <job_dir>/edits/<NAME>.<format>
       --output-path PATH save to absolute path (under VOICE/OUTPUT/PROJECTS roots)
       --format wav|flac|ogg|mp3   (default wav; ignored when --output-path given)
       --overwrite        allow overwriting existing output
       --timeout 600      HTTP timeout in seconds
  audio edits <job_id>                   # list edits saved for a job
  audio edit-get <job_id> <name> [--out FILE | -]
  audio edit-delete <job_id> <name>
  audio peaks <source> [--buckets N]     # waveform envelope buckets (use --json)

Discovery / introspection
  version                  Print CLI + server version
  about                    Server summary: paths, ports, uptime, GPU
  openapi [--out FILE]     Fetch the FastAPI OpenAPI document
  schema [model]           Per-model param schema (or summary table)
  capabilities <model>     Compact capability summary (alias: caps)

Diagnostics
  diagnose                 Run server self-check (binaries, GPU, HF token, models)
  disk                     Disk usage by directory + mount free space
  env                      Show TTS-relevant environment variables

Maintenance
  maintenance gc-jobs [--older-than HOURS]
  maintenance clear-cache --kind tmp|pip|hub|datasets|torch|xdg|modules|logs|all
  maintenance kill-stale
  maintenance restart-workers [--model X]
  maintenance cleanup-temp

Voices (extras)
  voices info <name>                     # size, sample rate, duration, transcription
  voices rename <old> <new>              # rename voice (preserves transcription)

One-shot helpers
  dryrun <model> --text "..." [--save-path ...]   # plan-only; no inference
  preview <model> [--text ...] [--out FILE]       # canned-text quick test
  open <app|voices|output|projects|models|logs|run>   # Explorer (Windows)
```

## Full REST API surface

The bridge proxies everything under `/api/` from port 9300 to the gateway on
8300. All `/api/*` calls require the persisted `X-TTS-API-Token`. New paths
added in the latest expansion are marked with **★**.

### Discovery / health

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, loaded models, worker count |
| GET | `/api/ready?model=X&timeout=N` | Long-poll until a worker for X is ready |
| GET | `/api/peers` | Live discovery registry (auth tokens stripped) |
| GET | **★ `/api/version`** | App version, API version, git commit, Python, platform |
| GET | **★ `/api/about`** | Uptime, paths, ports, GPU, worker rollup, model count |
| GET | `/openapi.json` | Full OpenAPI spec (auto-generated by FastAPI) |

### Schema / capabilities (★ all new)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/schema/tts` | Schema for every model (params, defaults, ranges, caps) |
| GET | `/api/schema/tts/{model}` | Schema for one model |
| GET | `/api/tts/{model}/capabilities` | Compact capabilities + voice list |

### Diagnostics (★ all new)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/diagnostics` | Binaries, GPU, HF token, cache hygiene, disk, models, workers |
| GET | `/api/disk` | Per-directory size + mount free space |
| GET | `/api/env` | TTS-relevant environment variables |

### Models / install / setup

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/models` | All registered models + metadata |
| GET | `/api/models/status` | Worker-side load status per model |
| GET | `/api/setup/status` | Install status per model (heuristic) |
| POST | `/api/setup/install/{model}` | Install one model (or `all`) |
| DELETE | `/api/setup/{model}` | Remove model weights + override packages |
| GET | `/api/setup/hf-token` | Whether an HF token is saved (masked) |
| POST | `/api/setup/hf-token` | Save / remove the HF token |
| GET | `/api/config` | Full UI-oriented config (paths, defaults, profiles) |
| GET | `/api/devices` | GPUs + CPU + workers per device |

### Workers

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/workers` | All workers and their status |
| POST | `/api/workers/spawn` | Spawn one worker `{model, device?, precision?}` |
| DELETE | `/api/workers/{worker_id}` | Kill one worker |
| POST | `/api/models/{model}/scale` | Scale to N workers `{count, device?}` |
| POST | `/api/models/{model}/load` | Spawn a worker if none exists |
| POST | `/api/models/{model}/unload` | Kill all workers for that model |

### Whisper

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/whisper` | Available sizes + currently loaded |
| POST | `/api/whisper/{size}/load` | Spawn a whisper worker for that size |
| POST | `/api/whisper/{size}/unload` | Unload only the requested Whisper size |

### TTS generation

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/tts/{model}` | Sync TTS — blocks until pipeline completes |
| POST | `/api/tts/{model}/submit` | Async — returns `job_id` immediately |
| POST | `/api/tts/{model}/upload` | Multipart sync TTS (avoids base64 ref bloat) |
| POST | `/api/tts/{model}/cancel` | Cancel one job (or all jobs for model) |
| POST | **★ `/api/tts/{model}/dryrun`** | Plan chunks + estimate runtime without inference |
| GET | `/api/tts/{model}/voices` | Built-in voices the model supports |

### Jobs

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/jobs` | List all known jobs |
| GET | `/api/jobs/{id}` | Job state + chunks |
| GET | `/api/jobs/{id}/output` | Download final audio |
| GET | `/api/jobs/{id}/chunks/{idx}/audio` | Download one chunk WAV |
| PUT | `/api/jobs/{id}/chunks/{idx}` | Edit chunk text + reset trailing chunks |
| POST | `/api/jobs/{id}/recover` | Resume failed/incomplete job |
| POST | `/api/jobs/delete` | Bulk delete `{job_ids: []}` |
| GET | **★ `/api/jobs/{id}/manifest`** | Full job.json + dir listing |
| GET | **★ `/api/jobs/{id}/zip`** | Whole job dir as .zip |
| GET | **★ `/api/jobs/{id}/stream`** | SSE progress events for one job |
| POST | **★ `/api/jobs/{id}/chunks/{idx}/rerun`** | Re-render one chunk |

### Projects

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/projects` | List projects under `projects_output/` |
| GET | **★ `/api/projects/{name}`** | Files + jobs inside one project |
| GET | **★ `/api/projects/{name}/zip`** | Whole project as .zip |
| DELETE | **★ `/api/projects/{name}`** | Delete project + jobs (irreversible) |

### Voices

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/voices` | List reference voice files |
| POST | `/api/voices/upload` | Upload one voice (multipart) |
| GET | `/api/voices/{filename}/audio` | Stream voice audio |
| DELETE | `/api/voices/{filename}` | Delete one voice + sidecar transcription |
| POST | `/api/voices/delete` | Bulk delete `{filenames: []}` |
| POST | `/api/voices/{filename}/transcribe` | Whisper transcribe + cache as sidecar |
| GET | **★ `/api/voices/{filename}/info`** | Detailed metadata: SR, channels, duration |
| POST | **★ `/api/voices/{filename}/rename`** | Rename voice + sidecar |

### Audio editor (★ all new)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/audio/effects` | Effect manifest + default parameters |
| POST | `/api/audio/peaks` | Waveform min/max + RMS buckets for canvas rendering |
| POST | `/api/audio/render` | Apply an edit chain, save to `<job_dir>/edits/` or a free `output_path` |
| GET | `/api/audio/edits/{job_id}` | List edits for a job |
| GET | `/api/audio/edits/{job_id}/{filename}` | Download one edit file |
| DELETE | `/api/audio/edits/{job_id}/{filename}` | Delete one edit file |

**Source descriptor** — used by `peaks` and `render`:

```json
{ "kind": "final", "job_id": "<UUID>" }
{ "kind": "chunk", "job_id": "<UUID>", "index": 0 }
{ "kind": "edit",  "job_id": "<UUID>", "name": "edit_v01.wav" }
{ "kind": "path",  "path": "/abs/file.wav" }    // under VOICE/OUTPUT/PROJECTS
```

**Render request** body:

```json
{
  "source":        { "kind": "...", "...": "..." },
  "edits":         [ { "type": "gain", "params": { "db": 3 } } ],
  "output_name":   "optional, no extension — saved under <job_dir>/edits/",
  "output_path":   "optional, absolute file path — overrides output_name",
  "output_format": "wav | flac | ogg | mp3   (ignored when output_path is given)",
  "overwrite":     false
}
```

`output_name` and `output_path` are mutually exclusive. Job-bound sources
default to auto-versioned names under `<job_dir>/edits/` if neither is
given. `path:` sources require `output_path`.

**Effect chain** — a list of `{type, params}`. Range ops (`cut`, `trim`,
`silence`, `fade_in`, `fade_out`) take `start_sec` and `end_sec`. Full-track
effects (`gain`, `tempo`, `pitch`, `eq_highpass/lowpass/shelf_low/shelf_high`,
`reverb`, `echo`, `compressor`, `noise_gate`, `de_reverb`, `de_ess`,
`normalize_lufs`, `normalize_peak`) take their own params — call
`/api/audio/effects` for the canonical list with defaults.

### Logs

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/logs/stream` | SSE live log stream |
| GET | **★ `/api/logs/tail?file=server&lines=200`** | Read last N lines of one persistent log file |

### Maintenance (★ all new)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/maintenance/gc-jobs` | Delete jobs older than N hours `{older_than_hours}` |
| POST | `/api/maintenance/clear-cache` | Wipe selected cache trees `{kinds: [...]}`. Does **not** touch model weights or venv. |
| POST | `/api/maintenance/kill-stale-workers` | Kill workers not in (ready, busy) |
| POST | `/api/maintenance/restart-workers?model=X` | Kill all workers (next request will respawn) |
| POST | `/api/maintenance/cleanup-temp` | Remove orphaned temp WAVs |

`clear-cache` accepts these kinds: `tmp`, `pip`, `hub`, `datasets`, `torch`,
`xdg`, `modules`, `logs`, or `all`. Models live under `data/` and the venv
under `venv/` — those are managed by the `/api/setup/*` endpoints.

### Original endpoints summarized below for completeness

| Method | Path                                    | Purpose                                              |
| ------ | --------------------------------------- | ---------------------------------------------------- |
| POST   | `/api/tts/{model}/submit`               | Async TTS — return `job_id` immediately, run in bg   |
| GET    | `/api/ready?model=X&timeout=N`          | Long-poll until a worker for model X is ready        |
| GET    | `/api/projects`                         | List subdirs under `projects_output/`                |
| GET    | `/api/jobs/{id}/chunks/{idx}/audio`     | Stream a single chunk WAV                            |
| GET    | `/api/peers`                            | Live discovery registry entries (auth tokens stripped) |

Every existing endpoint remains — see the FastAPI docs at
`http://127.0.0.1:9300/docs` once the server is running.

## Bridge whitelist note

The bridge proxy at port 9300 limits the paths it forwards
(`_ALLOWED_PREFIXES = ("/api/", "/health", "/static/", "/docs", "/openapi.json")`).
All new endpoints fall under `/api/` so no bridge changes were required.

## Troubleshooting

- **`error: server unreachable at http://127.0.0.1:9300`** — the TTS server
  isn't running. Start it via `TTSServer.exe`. The CLI doesn't auto-launch
  it (you don't want the orchestrator to silently spawn a heavy process).
- **`HTTP 401 Missing or invalid API token`** — the registry token is stale
  (the server restarted and rewrote it). Re-read the registry file or
  re-run via the CLI which auto-discovers.
- **`tts.cmd: 'python' not found`** — restore `runtime/python` from the complete release. For source-only use, install Python from python.org or the
  Microsoft Store. The wrapper tries `py`, `python`, then `python3`.
- **`discovery.publish` warnings in logs** — verify that
  `E:\tts_server\output\run\registry` is writable. The server intentionally
  does not fall back to `%LOCALAPPDATA%`.
