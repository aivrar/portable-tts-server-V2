# Diagnostics and maintenance

Use these tools when an install looks stuck, VRAM never returns, the VHDX is growing, or a peer app needs a machine-readable health picture. None of this replaces **Shutdown** for ending a session.

## Self-check

```cmd
tts.cmd diagnose
tts.cmd disk
tts.cmd env
tts.cmd about
tts.cmd version
tts.cmd devices
tts.cmd status
tts.cmd health
```

| Verb / route | What it tells you |
| --- | --- |
| `diagnose` `GET /api/diagnostics` | System binaries (ffmpeg, nvidia-smi, …), GPU, whether an HF token is saved, per-model install status, worker health, env-var hygiene (are caches really under `/opt/tts_server`?), disk free |
| `disk` `GET /api/disk` | Size of output, projects, voices, models, cache trees + free space on each mount (the selected Windows volume and the VHDX) |
| `env` `GET /api/env` | `HF_HOME`, `TORCH_HOME`, `CUDA_VISIBLE_DEVICES`, `TMPDIR`, `TTS_SERVER_*` |
| `about` `GET /api/about` | Paths, ports, uptime, GPU, worker rollup, registered/loaded models |
| `GET /api/capabilities` | Peer-app contract: endpoints grouped, artifact roots, policy flags (`prefer_async_submit_for_long_text`, `reuse_ready_workers_for_batches`, `unload_workers_after_final_artifacts`) |

If `env` shows Hub or Torch pointing at `/mnt/c` or another distro's home, stop and fix containment; do not continue installing. Check the distro returned by `Start-TTSServer.ps1 -VerifyOnly` and its backing disk.

## Logs

```cmd
tts.cmd logs follow --filter "ERROR" --reconnect
tts.cmd logs tail --file server --lines 500
tts.cmd logs tail --file bridge --lines 200
tts.cmd logs tail --file setup --lines 500
tts.cmd logs tail --file startup --lines 200
```

Setup's debug log is also `E:\tts_server\output\run\tts_server_setup.log`. Worker logs live under `E:\tts_server\output\logs\` and `GET /api/workers/{id}/logs?lines=200`.

The GUI **Log** tab is the SSE stream only (ring buffer 500). **Clear** there does not truncate files.

## Job garbage collection

Unnamed jobs older than 72 hours are the default retention target.

```cmd
tts.cmd maintenance gc-jobs --older-than 72
tts.cmd maintenance gc-jobs --older-than 168
```

API: `POST /api/maintenance/gc-jobs` `{older_than_hours: 1..8760, default 72}`. Does not delete named projects.

## Cache clear

```cmd
tts.cmd maintenance clear-cache --kind tmp --kind pip
tts.cmd maintenance clear-cache --kind all
```

API `{kinds: [...]}`. Allowed kinds: `tmp`, `pip`, `hub`, `datasets`, `torch`, `xdg`, `modules`, `logs`, or `all`. Default on the API when omitted: `["tmp"]`. Reports bytes freed per kind.

**Does not touch model weights or the venv.** Those are `/api/setup` Remove.

Do **not** clear `hub` as a casual cleanup. Some engines treat the Hub cache as their offline store; wiping it forces another multi-gigabyte download. Prefer `tmp` and `pip`. Skill guidance also mentions `home`, `xdg-config`, `xdg-data`, `xdg-state` when asking the API to drop disposable XDG trees.

## Workers

```cmd
tts.cmd maintenance kill-stale
tts.cmd maintenance restart-workers
tts.cmd maintenance restart-workers --model xtts
tts.cmd maintenance cleanup-temp
```

- **kill-stale** — `POST /api/maintenance/kill-stale-workers`. Kills workers whose status is not `ready` or `busy` (stuck `loading`, dead, etc.).
- **restart-workers** — kill matching workers; the next request or an explicit spawn loads fresh (useful after package updates).
- **cleanup-temp** — `min_age_minutes` default 30. Sweeps orphaned `raw_*.wav`, `assembled_*.wav`, `ref_*.wav`, `tmp*.wav/mp3`.

Per-worker kill remains `tts.cmd workers kill` / Server tab **Kill**. Full session teardown remains **Shutdown**.

## Disk pressure

Weights live on the VHDX (`/opt/tts_server/data`). Jobs and voices live on NTFS `E:\tts_server\output` and `voices`. If Explorer shows your app volume full, check both `tts.cmd disk` mounts. Copying the tree to a new PC copies the VHDX as well; shrinking a VHDX is a WSL/Windows operation outside this app and is not a supported in-app button.

`tts.cmd open logs` / `open output` / `open models` jumps Explorer to the right place (`models` may be `\\wsl$\linbox-TTS_Server\opt\tts_server\data`).

## Maintenance availability

Clear-cache and cleanup-temp return HTTP 409 while requests, downloads, jobs, installations, model loading, or registered workers could still be using the files. Wait for activity to finish and stop all workers before retrying. New API work is briefly refused during the cleanup operation. Failed cache deletions return `ok: false` for that cache kind.

Temporary reference cleanup includes supported audio extensions, including MP3 and M4A.

## Related pages

- [Troubleshooting](troubleshooting.md)
- [Storage, layout, and ports](storage-layout-and-ports.md)
- [Server tab](gui-server.md)
- [Jobs and projects](jobs-and-projects.md)
