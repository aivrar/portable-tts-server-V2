# Resource and portability rules

- Use only `linbox-TTS_Server` registered at `E:\tts_server\wsl`.
- Use the authenticated HTTP API for engine operations.
- Never stop or alter TQ Server or another Linbox app.
- Query `/api/devices` immediately before every load; another app can change
  free VRAM between calls. The gateway repeats a fresh check at load time and
  keeps a configurable reserve (`TTS_SERVER_GPU_RESERVE_GB`, default 1.5 GiB),
  but an external app can still allocate after that check.
- Load one TTS engine at a time. Use short text and bounded polling timeouts.
- Cancel at sustained host CPU >=85% (three samples) or free host RAM <12 GiB.
- Always call `/api/models/{model}/unload`, even after load/inference failure.
- Confirm `/api/workers` is empty for the tested engine and recheck GPU memory.
- Model unload uses targeted `posix_fadvise` on this app's files. Do not enable
  VM-wide `drop_caches`; WSL could evict cache used by unrelated Linbox apps.
- Setup is capped at two low-priority CPU cores. Install one named model only.
- Clear disposable pip/temp cache through `/api/maintenance/clear-cache`; do
  not clear `hub` blindly because some engines use it as their offline store.
- Keep outputs under `output/` or `projects_output/` in this app.
