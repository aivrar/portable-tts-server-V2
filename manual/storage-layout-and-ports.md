# Storage, layout, and ports

TTS Server keeps two storage areas under your portable folder (examples below use `E:\tts_server` and legacy `linbox-TTS_Server`; use your actual path/name from `Start-TTSServer.ps1 -VerifyOnly`): NTFS folders you browse in Explorer, and the ext4 filesystem inside `E:\tts_server\wsl\ext4.vhdx` mounted as `/opt/tts_server` in the `linbox-TTS_Server` distro. Mixing those up is the usual reason "I installed a model but Windows Search cannot find the weights."

## Two views of the same product

| Role | Windows path | Linux path | Lives on |
| --- | --- | --- | --- |
| App root (source, launchers, GUI static files) | `E:\tts_server` | `/mnt/e/tts_server` | NTFS (E:) |
| WSL registration directory | `E:\tts_server\wsl` | (the distro disk itself) | NTFS folder holding the VHDX |
| Portable Linux disk | `E:\tts_server\wsl\ext4.vhdx` | `/` of `linbox-TTS_Server` | VHDX |
| Linux-private app state | (Explorer: `\\wsl$\linbox-TTS_Server\opt\tts_server`) | `/opt/tts_server` | VHDX ext4 |
| User voices | `E:\tts_server\voices` | `/mnt/e/tts_server/voices` | NTFS |
| Jobs, logs, registry, token | `E:\tts_server\output` | `/mnt/e/tts_server/output` | NTFS |
| Named projects | `E:\tts_server\projects_output` | `/mnt/e/tts_server/projects_output` | NTFS |
| Hugging Face token | `E:\tts_server\secrets\hf_token` | `/mnt/e/tts_server/secrets/hf_token` | NTFS |
| WebView2 profile | `E:\tts_server\cache` | not used by Linux workers | NTFS |
| Packaged bootstrap rootfs | `E:\tts_server\runtime\linux-rootfs.tar.gz` | imported only if no VHDX | NTFS |

`tts.cmd open` opens these in Explorer (`app`, `voices`, `output`, `projects`, `models`, `logs`, `run`). Linux-only paths such as `/opt/tts_server/data` are translated to `\\wsl$\linbox-TTS_Server\opt\tts_server\data`.

## Linux-private tree (`/opt/tts_server`)

Created and owned by `setup.sh`. Nothing here should be stored on `/mnt/e` because 9P/drvfs is slow for PyTorch and Hugging Face.

| Subdir | Purpose |
| --- | --- |
| `venv/` | Shared Python runtime (`python3` used by gateway and workers) |
| `overrides/` | Isolated engine dependency trees (`coqui`, `dia`, `chatterbox`, `f5`, `fish`, `qwen`, `vibevoice`, `higgs`, `parler`, `outetts`, `voxtral`, `voxcpm2`, `orpheus`). `None` in `MODEL_OVERRIDE_MAP` means the engine uses the base venv only: kokoro, whisper, speecht5, edge, csm. |
| `repos/` | Pinned upstream source a few engines need |
| `data/` | Model weights. Also `HF_HOME`, Hub cache, datasets, modules, `TORCH_HOME`, Coqui `TTS_HOME` |
| `cache/` | App-owned HOME, XDG, pip, tmp, numba, triton, cuda, torchinductor, matplotlib |
| `env.conf` | Key=value paths written by setup; `config.py` reads this |
| `hf_token` | Legacy location; new saves go to `secrets/hf_token` on E: and are copied forward if the legacy file still exists |

`MODEL_OVERRIDE_MAP` (engine id → override directory name):

| Engine ids | Override |
| --- | --- |
| xtts, bark, vits | `coqui` (shared; removing one engine does not delete Coqui while another still needs it) |
| fish | `fish` |
| dia | `dia` |
| chatterbox | `chatterbox` |
| f5 | `f5` |
| qwen | `qwen` (tiny bitsandbytes-only layer; reuses base torch) |
| vibevoice | `vibevoice` |
| higgs | `higgs` |
| parler | `parler` |
| outetts | `outetts` |
| voxtral | `voxtral` |
| voxcpm2 | `voxcpm2` |
| orpheus | `orpheus` |
| kokoro, whisper, speecht5, edge, csm | base venv only |

## Windows-visible tree (`E:\tts_server`)

| Path | Purpose |
| --- | --- |
| `Start-TTSServer.cmd` / `.ps1` | Host preflight + launch |
| `runtime\launcher`, `runtime\webview2` | Native GUI and bundled browser |
| `tts.cmd`, `tts.py` | Windows CLI |
| `app.json` | Application identity/version and default launch metadata |
| `server\` | Gateway, workers, static GUI, install scripts |
| `voices\` | Reference audio (`.wav` / `.mp3` / `.flac` / `.ogg`) plus optional `.txt` Whisper sidecars |
| `output\jobs\` | Temporary jobs `{job_id}/job.json`, `chunk_NNN.wav`, final audio, optional SRT |
| `output\logs\` | Worker and server logs (`WORKER_LOG_DIR`) |
| `output\run\` | Runtime: `api_token`, `registry\tts_server.json`, setup lock and setup log |
| `output\delivered_jobs\` | Manifests for jobs that used `deliver_to` (work dir purged after copy) |
| `projects_output\` | Named persistent projects; `save_path` lands here |
| `secrets\hf_token` | Hugging Face token (mode 0600 on Linux) |
| `manual\` | This user manual |
| `skills\use-tts-server\` | Agent skill (not required for interactive use) |

Job directory shape:

```text
E:\tts_server\output\jobs\<job-folder>\
    job.json
    chunk_000.wav
    chunk_001.wav
    <stem>_final.wav          (or mp3/ogg/flac/m4a)
    <stem>.srt                (after SRT generation)
    <stem>_timing.json
    edits\                    (audio editor renders)
```

The job UUID is stored in `job.json` and is the identifier used by CLI/API routes. A Testing job's directory can instead be named `temp_<engine>_<timestamp>_<suffix>`; use discovery/job information rather than assuming its folder name equals the UUID. Saved projects use their selected project location.

When the stopped app moves, manifests are found under the new roots. Job-local reference snapshots and legacy references inside the app's voices/output/projects folders are rebound to that copy. Explicit exports outside the portable folder remain at their chosen external locations and are not included when copying the app.

Temporary jobs are subject to retention (`MAX_JOB_AGE_HOURS = 72` by default; `tts.cmd maintenance gc-jobs` / `POST /api/maintenance/gc-jobs`). Named projects are kept until you delete them.

## Ports and process map

| Port | Process | Role |
| --- | --- | --- |
| **8300** | `tts_api_server.py` inside WSL | FastAPI gateway. The packaged `app.json` start command binds `--host 0.0.0.0 --port 8300`; the bridge reaches it over localhost inside the same distro. Treat it as a local desktop API, not a LAN service. |
| **9300** | `bridge.py` inside WSL | Windows WebView and CLI entry point through WSL localhost forwarding. Path allowlist: `/api/`, `/health`, `/static/`, `/docs`, `/openapi.json`. |
| **8101–8200** | `tts_worker.py` children | One model (or one Whisper size) per process. Health every 10s, startup timeout 120s, 3 health failures then dead. |
| (none) | WebView2 | Renders `GET /` which is `static/index.html` with the API token injected as `window.__TTS_API_TOKEN__`. |

CORS on the gateway allows `http://localhost` and `http://127.0.0.1` on ports **9300, 8300, 9091, 8100**.

`GET /health` has **no auth**. Every `/api/*` route requires the persisted token except `OPTIONS` preflight.

## Discovery registry

Path (two views of one NTFS file):

- Windows: `E:\tts_server\output\run\registry\tts_server.json`
- WSL2: `/mnt/e/tts_server/output/run/registry/tts_server.json`

The server sets `APPHUB_REGISTRY_DIR` to this directory. `tts.py` resolves it relative to the app folder. Tests may override with `TTS_REGISTRY_PATH`. The file is created on startup, replaced atomically, removed on clean shutdown.

CLI discovery order for `--url` and `--token` (highest first):

1. Flags `--url`, `--token`
2. Env `TTS_API_URL`, `TTS_API_TOKEN`
3. Registry file
4. `output/run/api_token` (token only; URL defaults)
5. Defaults `http://127.0.0.1:9300` (bridge)

## Environment containment

`config.py` `app_environment()` forces:

- `HOME`, all `XDG_*`, `PIP_CACHE_DIR`, `NUMBA_CACHE_DIR`, `TRITON_CACHE_DIR`, `CUDA_CACHE_PATH`, `TORCHINDUCTOR_CACHE_DIR`, `MPLCONFIGDIR` under `/opt/tts_server/cache`
- `HF_HOME` / Hub / datasets / modules / Transformers cache under `/opt/tts_server/data`
- `TORCH_HOME`, `COQUI_TTS_CACHE`, `TTS_HOME` under `/opt/tts_server/data`
- `COQUI_TOS_AGREED=1`
- `TMPDIR` + Python `tempfile.tempdir` rebound to `/opt/tts_server/cache/tmp`
- `PATH` replaced with a portable Linux PATH (`venv/bin`, CUDA, `/usr/lib/wsl/lib` for `nvidia-smi`, then standard usr/bin). Inherited Windows PATH is discarded so a missing Linux binary fails instead of silently running `C:\...`.
- `PYTHONNOUSERSITE=1`

If a tool is missing, `tts.cmd diagnose` / `GET /api/diagnostics` should say so. Do not "fix" it by adding Windows Python to WSL PATH.

## Limits the server enforces

| Limit | Default | Env override |
| --- | --- | --- |
| Max pipeline text | 100000 chars | `TTS_SERVER_MAX_TEXT_CHARS` |
| Max chunks | 500 | `TTS_SERVER_MAX_CHUNKS` |
| Inline base64 audio in a sync response | 25 MB (omitted above this) | `TTS_SERVER_MAX_INLINE_AUDIO_BYTES` |
| Reference audio upload | 50 MB | (fixed) |
| GPU free-VRAM reserve before load | 1.5 GiB | `TTS_SERVER_GPU_RESERVE_GB` |
| Max inference pipeline threads | 16 | (code constant) |
| Job retention for unnamed jobs | 72 hours | `maintenance gc-jobs --older-than` |
| Per-engine infer timeout | see engine catalog | (code map `MODEL_INFER_TIMEOUT`) |

Per-engine **text chunk** limits (`TEXT_LIMITS`, characters per chunk the pipeline will feed the model) are separate from the 100000-char request cap. Long text is split automatically. Examples: bark 200, orpheus 200, xtts 250, edge 3000, vibevoice 800.

## Artifact references

Completed job audio has a canonical handle `tts://jobs/{job_id}/output`. Resolve it with `GET /api/jobs/{job_id}/output` (bytes) or `/output/info` (metadata). Peer apps should store the `tts://` ref, not a raw Windows path that might be a temp job about to be garbage-collected.

## Opening folders

```cmd
tts.cmd open app
tts.cmd open voices
tts.cmd open output
tts.cmd open projects
tts.cmd open models
tts.cmd open logs
tts.cmd open run
```

`open` refuses to ShellExecute anything that is not an existing directory.

## Related pages

- [Identity and requirements](identity-and-requirements.md)
- [Start, stop, and portability](start-stop-and-portability.md)
- [Jobs and projects](jobs-and-projects.md)
- [Diagnostics and maintenance](diagnostics-and-maintenance.md)
