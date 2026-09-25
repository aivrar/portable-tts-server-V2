# Windows CLI (`tts.cmd`)

The CLI is `tts.py` with the Windows wrapper `tts.cmd`. The complete release uses its bundled `runtime\python\python.exe`; no separate Python installation is needed. A source checkout falls back to `py -3`, `python`, or `python3` and exits 127 if none is available.

Run from your portable folder. Paths below use `E:\tts_server` as an example. The CLI talks to an already-running server and does not launch the GUI; start with `Start-TTSServer.cmd` first.

## Invocation

```cmd
E:\tts_server\tts.cmd <verb> [args]
E:\tts_server\tts.cmd tts kokoro --help
```

From the app directory you can shorten to `tts.cmd ...`. `python E:\tts_server\tts.py` is the same program.

Global flags may appear **before or after** the verb (`--json`, `--url`, `--token`). If you need those literal strings as values, use `=` form (`--text=--json`) or a `--` sentinel (`tts.cmd tts kokoro -- --text "--json starts here"`).

| Global flag | Meaning |
| --- | --- |
| `--json` | Machine-readable JSON on stdout |
| `--url URL` | Override API base (else discovery) |
| `--token TOKEN` | Override API token (else discovery) |

## Discovery and auth

Order for URL + token (highest first):

1. `--url` / `--token`
2. `TTS_API_URL` / `TTS_API_TOKEN`
3. `E:\tts_server\output\run\registry\tts_server.json`
4. `E:\tts_server\output\run\api_token` (token only; URL defaults to the bridge)
5. Defaults: `http://127.0.0.1:9300`

```cmd
tts.cmd token
```

Prints the auto-discovered token. Treat it as secret. Header name is always `X-TTS-API-Token`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | HTTP error from the server |
| 2 | Server unreachable / discovery failed |
| 3 | `wait-ready` timeout |
| 4 | Install / job wait timeout |
| 5 | Job ended `failed` |
| 127 | Wrapper: no Windows Python |
| 130 | Interrupted (Ctrl-C) |

## Discovery / status verbs

```cmd
tts.cmd health
tts.cmd wait-ready --model kokoro --timeout 60
tts.cmd config
tts.cmd config models.kokoro
tts.cmd models
tts.cmd models --installed --loaded
tts.cmd devices
tts.cmd status
tts.cmd status --watch
tts.cmd token
tts.cmd peers
tts.cmd peers --local
tts.cmd version
tts.cmd about
tts.cmd openapi --out openapi.json
tts.cmd schema
tts.cmd schema xtts
tts.cmd capabilities edge
tts.cmd caps kokoro
```

- **health** — `GET /health` (no auth): status, loaded_models, worker_count.
- **wait-ready** — `GET /api/ready?model=&timeout=`. Without `--model`, returns as soon as the gateway is up. With `--model`, waits until a worker for that id is ready (timeout 0–600 seconds on the API; CLI `--timeout` is the wait). Exit 3 on timeout.
- **config [key]** — `GET /api/config`, optional dotted key.
- **models** — catalog plus install/load when flags given.
- **devices** — GPUs/CPU and workers per device. Call this immediately before a heavy load.
- **status --watch** — combined install + worker view, auto-refresh.
- **peers** — other local discovery entries. `--local` reads the filesystem only (no HTTP).
- **schema** — per-model params, defaults, ranges, capabilities. Authoritative before you invent flags.
- **capabilities / caps** — compact accepted params + voices. Edge includes live catalog metadata.

## Install / Hugging Face

```cmd
tts.cmd install kokoro --wait --timeout 3600
tts.cmd install all --wait --timeout 3600
tts.cmd remove bark
tts.cmd hf-token get
tts.cmd hf-token set hf_xxxxxxxx
tts.cmd hf-token set -
```

`install` is `POST /api/setup/install/{model}`. `--wait` waits for the whole requested installation and reports failed/cancelled attempts with exit 4. VITS packages-only is accepted when weights are explicitly marked on-demand. `all` installs missing engines sequentially. `remove` is `DELETE /api/setup/{model}` (weights + override; not a base-venv uninstall). `hf-token set -` reads the token from stdin.

## Workers

```cmd
tts.cmd workers list
tts.cmd workers spawn xtts --device cuda:0 --precision fp16
tts.cmd workers kill <worker_id>
tts.cmd workers scale kokoro 2 --device cuda:0
tts.cmd model load chatterbox --device cuda:0
tts.cmd model unload chatterbox
```

`model load` is the convenience spawn-if-missing path. `model unload` kills every worker for that engine (including whisper).

## Voices

```cmd
tts.cmd voices list
tts.cmd voices upload E:\tts_server\voices\me.wav
tts.cmd voices info me.wav
tts.cmd voices transcribe me.wav --size base
tts.cmd voices rename me.wav narrator.wav
tts.cmd voices download narrator.wav --out C:\out\narrator.wav
tts.cmd voices delete narrator.wav
```

Upload cap 50 MB. Transcribe writes a `.txt` sidecar. Rename keeps the sidecar.

## Whisper

```cmd
tts.cmd whisper info
tts.cmd whisper load base
tts.cmd whisper unload base
```

Sizes: `tiny`, `base`, `small`, `medium`, `large`.

## TTS generation (`tts` verb)

```cmd
tts.cmd tts kokoro --text "hello world" --out hello.wav
tts.cmd tts xtts --text-file scene.txt --voice "Daisy Studious" --save-path projects/foo/intro
tts.cmd tts f5 --text "..." --ref E:\tts_server\voices\f5_english_ref.wav --reference-text "exact transcript" --out out.wav
tts.cmd --json tts xtts --text "..." --save-path myproj/line --async
```

Text comes from `--text`, or `--text-file`, or stdin (not a mix of `--text` and `--text-file`).

| Flag | Sends |
| --- | --- |
| `--out FILE` | Download/save audio on the Windows side (else sync response may include inline base64) |
| `--save-path SUBDIR/NAME` | Server-side folder under `projects_output/` |
| `--format wav\|mp3\|ogg\|flac\|m4a` | `output_format` |
| `--ref FILE` | Base64 `reference_audio` + `reference_audio_name` |
| `--voice NAME` | Built-in name or voices/ filename |
| `--reference-text "..."` | Exact transcript of the reference |
| `--language en` | XTTS language id |
| `--device cuda:1` | Pin a device |
| `--speed 1.0` | Post time-stretch |
| `--temperature` `--repetition-penalty` `--top-p` `--top-k` `--cfg-scale` `--cfg-alpha` `--exaggeration` `--cfg-weight` `--waveform-temperature` `--seed` `--nfe-step` `--pitch` `--volume` `--speaker-idx` | Engine knobs (JSON numbers, not strings) |
| `--de-reverb` `--de-ess` | Restoration (keep 0 unless needed) |
| `--verify-whisper` `--whisper-model base` `--tolerance 80` | Verification |
| `--auto-retry 3` | Retries |
| `--no-postprocess` | `skip_post_process` |
| `--voice-description "..."` | Parler / Higgs style prompt |
| `--inter-pause-sec` `--front-pad-sec` `--padding-sec` `--trim-db` `--min-silence-ms` `--front-protect-ms` `--end-protect-ms` `--clipping` `--lufs` | Audio profile overrides |
| `--async` | `POST /api/tts/{model}/submit` (202 + `job_id`); do not block |
| `--request-timeout 1800` | HTTP timeout seconds |

`--json` + `--async` is the orchestrator pattern: parse `job_id`, then `jobs wait`.

### Dry run and preview

```cmd
tts.cmd dryrun xtts --text-file long.txt --save-path myproj/scene_01
tts.cmd preview kokoro --out preview.wav
```

**dryrun** is `POST /api/tts/{model}/dryrun`: chunk plan, `save_path` resolution, runtime estimate, `effective_params` — no worker occupancy, no job. **preview** speaks a canned short line (quick smoke test).

### Batch

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

Each element can override voice and other tts fields. With `"wait": false` you poll `jobs wait` yourself.

## Jobs

```cmd
tts.cmd jobs list --status running --limit 20
tts.cmd jobs get <id>
tts.cmd jobs chunks <id>
tts.cmd jobs output <id> --out final.wav
tts.cmd jobs chunk-audio <id> 0 --out chunk0.wav
tts.cmd jobs cancel <id>
tts.cmd jobs recover <id>
tts.cmd jobs delete <id> <id2>
tts.cmd jobs edit-chunk <id> 2 --text "corrected sentence."
tts.cmd jobs rerun-chunk <id> 2
tts.cmd jobs wait <id> --timeout 1800 --interval 2
tts.cmd jobs manifest <id>
tts.cmd jobs zip <id> --out my-job.zip
tts.cmd jobs stream <id> --timeout 1800
```

`jobs list` also accepts the API's `model`, `since`, `until`, `offset` when you use HTTP; the CLI exposes `--status` and `--limit` on the parser. `wait` exits 5 if the job failed, 4 on timeout. `stream` prints lines like `[running] chunks=5/12`. `cancel` maps to `POST /api/tts/{model}/cancel` with that job id. `recover` resumes from the first incomplete chunk. `edit-chunk` resets that chunk and all later chunks. Deleting a **running** job is refused.

There is no `jobs srt` verb; generate captions with HTTP `POST /api/jobs/{id}/srt` (see the SRT page) or by sending `generate_srt` on a generate request via HTTP. The CLI tts flags do not currently hoist `generate_srt`; use the API or the GUI-adjacent job SRT endpoint via `skills/use-tts-server/scripts/tts_api.py`.

## Projects

```cmd
tts.cmd projects
tts.cmd project get myproject
tts.cmd project zip myproject --out myproject.zip
tts.cmd project delete myproject --yes
```

`projects` lists `projects_output/` subdirectories. **delete** is irreversible and purges jobs under that folder; `--yes` is required.

## Audio editor

```cmd
tts.cmd audio effects
tts.cmd audio render final:<job_id> --effect gain:db=3 --effect eq_highpass:cutoff_hz=120 --effect compressor:threshold_db=-18,ratio=4 --effect normalize_lufs:target_lufs=-16 --output-name podcast_master --format mp3
tts.cmd audio render chunk:<job_id>:5 --chain-file chain.json --format flac
tts.cmd audio render path:E:\tts_server\voices\me.wav --effect pitch:semitones=-2 --output-path E:\tts_server\projects_output\me_lower.wav
tts.cmd audio edits <job_id>
tts.cmd audio edit-get <job_id> podcast_master.mp3 --out C:\out\final.mp3
tts.cmd audio edit-delete <job_id> podcast_master.mp3
tts.cmd audio peaks final:<job_id> --buckets 1024 --json
```

Source specs: `final:JOB`, `chunk:JOB:N`, `edit:JOB:NAME`, `path:E:\tts_server\...` (Windows paths are translated to `/mnt/<drive>/`). `--effect` is repeatable `TYPE[:k=v,k=v]`. `--chain-file` is a JSON list of `{type, params}`. `--output-name` vs `--output-path` are mutually exclusive. `--overwrite` needed to replace an existing file (else HTTP 409). `--timeout` default 600s. Format is ignored when `--output-path` already has an extension the server honors as given.

`chain.json` example:

```json
[
  {"type": "de_reverb", "params": {"strength": 0.5}},
  {"type": "eq_shelf_high", "params": {"cutoff_hz": 4000, "gain_db": 2}},
  {"type": "compressor", "params": {"threshold_db": -18, "ratio": 4}},
  {"type": "normalize_lufs", "params": {"target_lufs": -16}}
]
```

## Logs

```cmd
tts.cmd logs follow --filter "XTTS" --reconnect
tts.cmd logs tail --lines 500 --file server
```

## Diagnostics and maintenance

```cmd
tts.cmd diagnose
tts.cmd disk
tts.cmd env
tts.cmd maintenance gc-jobs --older-than 168
tts.cmd maintenance clear-cache --kind hub --kind pip
tts.cmd maintenance kill-stale
tts.cmd maintenance restart-workers
tts.cmd maintenance restart-workers --model xtts
tts.cmd maintenance cleanup-temp
```

`clear-cache` kinds: `tmp`, `pip`, `hub`, `datasets`, `torch`, `xdg`, `modules`, `logs`, or `all`. Default if you called the API with no kinds is `["tmp"]`. **Does not delete model weights or the venv.** Do not clear `hub` blindly; some engines use it as their offline store. Alias: `tts.cmd maint ...`.

`open` kinds: `app`, `voices`, `output`, `projects`, `models`, `logs`, `run`.

## Orchestrator snippet

```python
import json, requests, subprocess
subprocess.check_call([r"E:\tts_server\tts.cmd", "tts", "kokoro",
                       "--text", "hello world", "--save-path", "demo/hello"])
```

Or HTTP with the registry:

```python
import json, requests
reg = json.load(open(r"E:\tts_server\output\run\registry\tts_server.json"))
url, token = reg["endpoints"]["api"], reg["auth"]["token"]
requests.post(f"{url}/api/tts/kokoro",
              headers={"X-TTS-API-Token": token},
              json={"text": "hello world", "save_path": "demo/hello"})
```

## Failure notes

- `error: server unreachable at http://127.0.0.1:9300` — app not running. Start `Start-TTSServer.cmd`. Exit 2.
- `HTTP 401 Missing or invalid API token` — the supplied token does not match the server. Re-run without a stale `--token`; let discovery re-read the registry. Normal restarts reuse the token.
- `discovery.publish` warnings — `output\run\registry` not writable. There is no AppData fallback.

## Output and failure handling

`--out speech.mp3` selects MP3 unless an explicit matching `--format` is supplied; conflicting extensions are rejected. `--out` also downloads saved jobs when combined with `--save-path`. Large generated audio and ZIP exports stream to disk.

Job waiting and streaming return failure for cancelled, failed, or incomplete jobs and for a stream that ends before completion. A batch exits with failure if any entry fails and omits audio base64 from its JSON summary. Install waiting emits one JSON result.

Local discovery tokens are not implicitly sent to an unrelated `--url` override. Supply that server's token explicitly when needed. Cross-origin redirects are rejected, and `peers --local` omits authentication secrets.

## Related pages

- [Authenticated HTTP API](http-api.md)
- [Jobs and projects](jobs-and-projects.md)
- [Audio editor usage](audio-editor-usage.md)
- [Diagnostics and maintenance](diagnostics-and-maintenance.md)
