# A to Z knowledge page

Alphabetical full-usage index for TTS Server, the **linux distro for windows users** called Portable TTS Server V2. Every entry points at the live product (GUI label, `tts.cmd` verb, HTTP path, or engine id). Read the dedicated pages for procedures; this page is the A-to-Z map.

---

## A

**about** — `tts.cmd about` / `GET /api/about`. Paths, ports, uptime, GPU, workers, models.

**af_heart / af_bella** — Kokoro voice ids. Prefix `a` = American, `f` = female. See [engines-catalog.md](engines-catalog.md).

**API token** — Persisted local secret, reused across restarts. Header `X-TTS-API-Token`, or `Authorization: Bearer`, or `?token=` on SSE. Files: `output\run\registry\tts_server.json` `auth.token` and `output\run\api_token`. `tts.cmd token`. 401 = missing or mismatched token. See [http-api.md](http-api.md).

**app.json** — Application metadata and version. Startup derives the distro from the actual folder; native window content starts at 1440×900.

**artifact ref** — `tts://jobs/{job_id}/output`. Resolve with `GET /api/jobs/{id}/output` or `/output/info`.

**async submit** — `POST /api/tts/{model}/submit` or `tts.cmd tts <model> --async`. HTTP 202 with `job_id`, `poll_url`, `output_url`. Prefer this for long text (`prefer_async_submit_for_long_text`).

**audio editor** — GUI **Editor** tab; `tts.cmd audio effects|render|edits|edit-get|edit-delete|peaks`; `/api/audio/*`. See [audio-editor-usage.md](audio-editor-usage.md) and [gui-editor.md](gui-editor.md).

**audio profile** — Per-engine trim/pad/LUFS/pause. Testing tab collapsible **Audio Profile**. Keys: `inter_pause_sec`, `front_pad_sec`, `padding_sec`, `trim_db`, `min_silence_ms`, `front_protect_ms`, `end_protect_ms`, `clipping`, `lufs`.

**auto_retry** — 0–10. Testing **Retries** default 3.

---

## B

**bark** — Expressive Coqui engine, `v2/{lang}_speaker_{0-9}`, tags `[laughs]` `[music]`, ~12GB, ~6 GiB VRAM, 600s timeout, history chaining. Not for exact narration.

**batch** — `tts.cmd --json batch batch.json` with `model`, `wait`, `jobs[]` of `{text, save_path, voice?}`.

**bf16 / fp16 / fp32** — Server tab **Precision**. Auto = omit. Spawn body `precision`.

**bridge** — proxy inside the TTS WSL distro on port **9300**, reached from Windows through WSL localhost forwarding. Allowlist `/api/`, `/health`, `/static/`, `/docs`, `/openapi.json`. CLI default URL.

**builtin_voices** — `MODEL_SETUP` flag. Voice dropdown from `/api/tts/{model}/voices`.

---

## C

**cache** — Linux: `/opt/tts_server/cache` (HOME, XDG, pip, tmp, CUDA, Triton). Windows GUI: `E:\tts_server\cache` (WebView2). `maintenance clear-cache --kind tmp|pip|hub|datasets|torch|xdg|modules|logs|all`. Never casual-clear `hub`.

**capabilities** — `GET /api/capabilities` (app contract) vs `GET /api/tts/{model}/capabilities` (one engine). CLI `tts.cmd capabilities` / `caps`.

**cfg_alpha** — Voxtral flow-matching guidance, default 1.2, range 0.5–3.0.

**cfg_scale / cfg_weight** — Diffusion/CFG knobs. Chatterbox `cfg_weight` 0.5; F5 `cfg_scale` 2.0; Dia 3.0; VibeVoice 1.3; VoxCPM2 2.0.

**chatterbox** — Emotion clone engine. `exaggeration` + `cfg_weight`. Requires reference audio. English assets only.

**Chelsie / Ethan** — Qwen Omni's only two voices. No cloning.

**chunk** — Pipeline split using `TEXT_LIMITS`. Files `chunk_000.wav`. Edit: `jobs edit-chunk`. Rerun: `jobs rerun-chunk`. Audio: `jobs chunk-audio`. Editor library **CHK N**.

**clear-cache** — See cache. Does not delete weights or venv.

**clipping** — Peak limit in the audio profile, default 0.95.

**clone (voice)** — Engines with `ref_audio`: chatterbox, dia, f5, fish, higgs, vibevoice, xtts, outetts, voxtral, voxcpm2, csm. Many also need `reference_text` (`ref_text` flag). Files live in `E:\tts_server\voices`.

**cloud** — Edge TTS only. Needs network, not GPU, not Hub weights.

**coqui** — Shared override for bark, xtts, vits. Removing one engine keeps the override while another needs it.

**compressor** — Editor effect: threshold_db, ratio, attack_ms, release_ms, makeup_db.

**config** — `GET /api/config`, `tts.cmd config [dotted.key]`. Source of sliders, profiles, `MODEL_SETUP`.

**Connected (N workers)** — Green gateway badge. Red **Disconnected**. Orange **Reconnecting...**. Gray **Connecting...**.

**CORS** — localhost/127.0.0.1 on 9300, 8300, 9091, 8100.

**csm** — Sesame CSM-1B. Roles `speaker_0`–`speaker_3`, not fixed identities. Apache-2.0. Supply reference + transcript for a stable voice.

**CUDA / cuda:0** — Default worker device when `nvidia-smi` works. Reserve 1.5 GiB (`TTS_SERVER_GPU_RESERVE_GB`). Loads serialized per GPU.

**cut / trim / silence / fade_in / fade_out** — Editor range ops (`start_sec`, `end_sec`).

---

## D

**de_ess / de_reverb** — TTS post-process sliders (default 0) and editor restoration effects. Leave TTS de_reverb at 0 for dry speech.

**deliver_to** — Absolute output path on a generate request; mutually exclusive with `save_path`. Copies final audio + SRT/timing; purges the work dir. Response `delivered_to`. Manifests under `output\delivered_jobs\`.

**devices** — `GET /api/devices`, `tts.cmd devices`. Call immediately before load.

**dia** — `[S1]` / `[S2]` dialogue, 44.1 kHz, ~8 GiB, laughs as `(laughs)`. Unload after use.

**diagnostics** — `tts.cmd diagnose` / `GET /api/diagnostics`. See [diagnostics-and-maintenance.md](diagnostics-and-maintenance.md).

**discovery registry** — `E:\tts_server\output\run\registry\tts_server.json`. Created on start, removed on Shutdown. No AppData fallback. `tts.cmd peers` / `GET /api/peers`.

**disk** — `tts.cmd disk` / `GET /api/disk`. Weights on VHDX; jobs on NTFS.

**distro** — Dedicated `TTS-Server-V2-<path hash>` WSL2 instance. Query `Start-TTSServer.ps1 -VerifyOnly` for the actual name.

**dryrun** — `POST /api/tts/{model}/dryrun`, `tts.cmd dryrun`. Chunk plan, path, estimate, `effective_params`. No job, no VRAM occupancy.

---

## E

**E:\tts_server** — Example Windows app root used in this manual. Other local folders and drives are supported.

**edge** — Edge TTS. Cloud, 300+ live voices cached six hours, pitch/volume, no GPU. Fallback `en-US-JennyNeural` and siblings.

**Editor** — GUI tab (DOM id `jobs`). Library / waveform / effects / Render.

**edits/** — Per-job folder of rendered editor output. `GET /api/audio/edits/{job_id}`.

**EN-FEMALE-1-NEUTRAL** — Only bundled OuteTTS voice.

**env** — `tts.cmd env` / `GET /api/env`. Confirm caches inside `/opt/tts_server`.

**exaggeration** — Chatterbox emotion. Default 0.5. GUI 0–1, API 0–2.

**exit codes (CLI)** — 0 ok, 1 HTTP error, 2 unreachable, 3 wait-ready timeout, 4 install/job wait timeout, 5 job failed, 127 no Windows Python, 130 Ctrl-C.

**ext4.vhdx** — `E:\tts_server\wsl\ext4.vhdx`. The Linux disk. Copy it when you copy the app.

---

## F

**f5** — F5-TTS diffusion clone. Needs reference **and** transcript. nfe_step 32, cfg_scale 2.0. ~1.35GB weights, ~3 GiB VRAM.

**fail-closed** — Wrong registered backing disk, unrelated distro, or a path escaping the app/private directories causes startup to refuse.

**ffmpeg** — Required binary inside the distro; `diagnose` checks it.

**fish** — Fish Speech S1-mini. Clone with exact transcript, 5–15s dry clip. Temperature max 1.0. Style `(excited)` etc.

**format** — wav (default), mp3, ogg, flac, m4a (API/CLI; Testing tab omits m4a). Editor render: wav, flac, ogg, mp3.

**fp16 / fp32 / bf16** — See Precision.

---

## G

**gateway** — FastAPI on port **8300**. Server tab **Gateway** panel.

**gc-jobs** — `tts.cmd maintenance gc-jobs --older-than 72`. Deletes old unnamed jobs. 1–8760 hours.

**Generate** — Testing tab primary button. Sync POST `/api/tts/{model}` or `/upload`.

**generate_srt** — Pipeline flag. Auto-spawns Whisper; may skip after 60s if Whisper is not ready.

**GPU reserve** — Default 1.5 GiB. Env `TTS_SERVER_GPU_RESERVE_GB`.

---

## H

**health** — `GET /health` (no auth). `tts.cmd health`.

**higgs** — Higgs Audio 3B. Scene/speaker description, clone, ~12GB weights, ~16 GiB VRAM estimate, long cold load. CPU listed on the Setup card; GPU is the practical path.

**history (Bark)** — Speaker-history chaining across chunks; profile `history_reset_every` 5.

**history (Testing)** — In-memory Response History with Play / Stop / Save. Not the job store.

**Hugging Face token** — Setup panel **HuggingFace Token**, `Save Token`, `hf_...`. File `E:\tts_server\secrets\hf_token`. `tts.cmd hf-token get|set`. Required for gated models. Accept each Hub license.

---

## I

**Install / Install All / Remove** — Setup tab. `POST /api/setup/install/{model|all}`, `DELETE /api/setup/{model}`, `POST /api/setup/cancel/{model|all}`. Statuses: not_installed, partial, packages_only, ready, installing.

**install_model.sh** — Distro-side installer. Ids: kokoro, dia, fish, f5, bark, xtts, chatterbox, qwen, vibevoice, higgs, whisper, speecht5, parler, outetts, vits, edge, voxtral, voxcpm2, csm, orpheus.

---

## J

**jobs** — See [jobs-and-projects.md](jobs-and-projects.md). CLI `jobs list|get|output|chunks|chunk-audio|cancel|recover|delete|edit-chunk|rerun-chunk|wait|manifest|zip|stream`. HTTP `/api/jobs`.

**job.json** — Atomic manifest: parameters, chunks, status, paths.

**JSON CLI** — `tts.cmd --json ...` anywhere on the command line.

---

## K

**Kill / Kill All** — Server tab. `DELETE /api/workers/{id}`. Kill All confirms unloading all GPU models.

**kill-stale** — `tts.cmd maintenance kill-stale`. Workers not in ready/busy.

**kokoro** — Small local narrator, ~300MB, ~1 GiB VRAM, many `*.pt` voices, no clone. Best first local engine.

---

## L

**Language (XTTS)** — Testing select: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, ko, hu, hi.

**linbox-TTS_Server** — Legacy distro name accepted only when it is already registered to this portable folder’s disk.

**live** — `GET /api/live`.

**Log** — GUI tab. Filters Info / Success / Error / Warning, Clear, auto-scroll. SSE `/api/logs/stream`. CLI `logs follow`, `logs tail --file server|bridge|setup|startup`.

**LUFS** — Profile target default -23. Editor **LUFS Normalize** default -16.

---

## M

**maintenance** — Alias `maint`. gc-jobs, clear-cache, kill-stale, restart-workers, cleanup-temp.

**MAX_CHUNKS** — Default 500 (`TTS_SERVER_MAX_CHUNKS`).

**MAX_TEXT_CHARS** — Default 100000.

**mode (XTTS)** — `cloned` (default) or `built-in`.

**MODEL_SETUP** — Authoritative engine table in `server/config.py`.

**models** — `tts.cmd models [--installed --loaded]`, `GET /api/models`, `GET /api/models/status`.

**multipart upload** — Testing Or Upload and `POST /api/tts/{model}/upload`; voices `POST /api/voices/upload`. Reference audio ≤ 50MB.

---

## N

**nfe_step** — Diffusion/flow steps. F5 default 32 (GUI slider max 64, API max 128). VoxCPM2 default 10.

**nvidia-smi** — Detected via `/usr/lib/wsl/lib`. Gateway refreshes it before each load.

---

## O

**open** — `tts.cmd open app|voices|output|projects|models|logs|run`. Explorer only; refuses non-directories.

**openapi** — `http://127.0.0.1:9300/openapi.json`, `tts.cmd openapi`.

**orpheus** — Eight voices tara/leah/jess/leo/dan/mia/zac/zoe. Tags `<laugh>` `<chuckle>` … `repetition_penalty` ≥ 1.1. No clone in this app.

**output/** — Windows-visible jobs, logs, run, delivered_jobs.

**overrides/** — `/opt/tts_server/overrides`. See [storage-layout-and-ports.md](storage-layout-and-ports.md).

---

## P

**packages_only** — Packages exist but required model weights are missing. Choose Install Weights; VITS can instead download its weights on first load.

**parler** — Voice = natural-language description. 44.1 kHz. 90s infer timeout kills the worker.

**path source** — Editor/API `kind: path`. Must stay under voices, output, or projects_output.

**peers** — `tts.cmd peers [--local]`, `GET /api/peers`.

**pitch / volume** — Edge Hz and percent, -50..50.

**ports** — Gateway 8300, bridge 9300, workers 8101–8200.

**portability** — Stop with `Stop-TTSServer.cmd`; when the disk is released, copy the entire folder including `wsl/ext4.vhdx`, then Start at the new local path. If WSL holds the disk, use `Copy-TTSServer.ps1 -Destination <new-folder>`. See [start and portability](start-stop-and-portability.md).

**Precision** — Spawn control Auto / FP16 / BF16 / FP32.

**preview** — `tts.cmd preview <model>` canned short smoke test.

**projects / projects_output** — Named persistent folders. `tts.cmd projects`, `project get|zip|delete`. `save_path` lands here.

**python (Windows)** — Required for `tts.cmd` only (`py` / `python` / `python3`). Not required for the GUI.

---

## Q

**quickstart** — [Illustrated first narration](quickstart.md): install Kokoro, load a worker, generate a take, edit it, and save/download the result.

**qwen** — Qwen Omni 7B. Chelsie / Ethan only. ~21GB snapshot, long cold load, 4-bit isolated path. Unload after batches.

---

## R

**ready** — Worker status and `/api/ready?model=&timeout=`. 503 timeout body is `{status, ready, model}` not `{detail}`.

**recover** — `POST /api/jobs/{id}/recover`, `tts.cmd jobs recover`. First incomplete chunk.

**reference_audio / reference_text** — Clone inputs. `--ref` on CLI base64-encodes the file.

**Refresh Status** — Setup toolbar. Re-fetches install status.

**registry** — See discovery registry.

**Render** — Editor tab. `POST /api/audio/render`.

**repetition_penalty** — Sampling knob. Orpheus keep ≥ 1.1. XTTS default 2.0.

**Response History** — Testing tab table.

**restart-workers** — Maintenance: kill workers so the next request respawns.

**Retries** — Testing post-process `auto_retry`.

**rootfs.tar.gz** — Bundled `runtime/linux-rootfs.tar.gz` initializes the disk only when no installed VHD exists.

---

## S

**save_path** — Relative path under projects_output / output for the whole job folder.

**Save Token** — Setup button. Empty field removes the token.

**schema** — `GET /api/schema/tts[/{model}]`, `tts.cmd schema`. Authoritative params.

**secrets\hf_token** — Hugging Face token file.

**seed** — 0–99999. 0 = random on engines that treat 0 that way; several engines default seed 0 or 42 for reproducibility.

**Server** — GUI tab: Gateway, Spawn Worker, workers table.

**Setup** — GUI tab: token, Install All, engine cards.

**Shutdown** — Header button. `POST /api/shutdown`. Unload workers, unpublish registry, stop bridge, close window.

**Skip post-processing** — Testing toggle `skip_post_process`.

**source_guided** — SRT default true. See [srt-and-whisper.md](srt-and-whisper.md).

**Spawn Worker** — Server tab. `POST /api/workers/spawn`.

**Speaker 1:** — VibeVoice turn-taking syntax (up to four ordered refs).

**speaker_0** — CSM role id.

**speecht5** — CPU-friendly, seven CMU-Arctic names, 16 kHz. Spell acronyms.

**speed** — Post time-stretch 0.5–2.0 via pyrubberband, not an engine sampler unless noted.

**SRT** — `POST /api/jobs/{id}/srt`. Files `.srt` + `_timing.json`. words_per_line 1–20 default 3.

**Start-TTSServer.cmd** — Only supported start. Calls `Start-TTSServer.ps1`. `-VerifyOnly` for a dry registration check.

**status (CLI)** — Combined install + workers; `--watch` refreshes.

**style tags** — Testing Insert row when `style_tags` is true. Dialects differ (bark brackets, orpheus angles, voxcpm2 leading parentheses).

---

## T

**temperature** — Sampling randomness. Per-engine defaults in the catalog. Fish max 1.0.

**Testing** — GUI tab for interactive Generate.

**token** — See API token and Hugging Face token (different secrets).

**tolerance** — Whisper verification percent, default 80.

**top_p / top_k** — Nucleus / top-k sampling.

**Transcribe** — Voices tab; Whisper sidecar `.txt`.

**tts.cmd / tts.py** — Windows CLI. Stdlib only. Does not launch the server.

**TTSServer.exe** — GUI launcher. Not a help CLI. Prefer the `.cmd` wrapper.

---

## U

**unload** — `POST /api/models/{model}/unload`, `tts.cmd model unload`. Kills the process group. Always do this in scripts.

**upload** — Voices files; TTS multipart reference; transcribe upload ≤ 50MB.

---

## V

**venv** — `/opt/tts_server/venv`. Shared Python. Not on NTFS.

**verification** — Whisper-compare while generating. `/api/jobs/{id}/verification`.

**VerifyOnly** — `.\Start-TTSServer.ps1 -VerifyOnly`.

**VHDX** — `E:\tts_server\wsl\ext4.vhdx`.

**vibevoice** — Long-form `Speaker N:` , up to four refs, required on the Testing tab. Not overlapping speech.

**vits** — CPU LJSpeech, ~150MB, no clone, 22.05 kHz, 60s timeout.

**voice_description** — Parler and Higgs prose prompt field.

**Voices** — GUI tab for `E:\tts_server\voices`. Also `/api/tts/{model}/voices` for built-in catalogs.

**volume** — Edge percent. Also editor `gain`.

**voxcpm2** — 48 kHz, parenthesized voice design, 30 languages, Apache-2.0. Label synthetic audio.

**voxtral** — 20 presets, 9 languages, ~20 GiB VRAM, **CC BY-NC 4.0 non-commercial**, vLLM. Unload after a batch.

**VRAM estimates** — `MODEL_VRAM_ESTIMATE_GB` in config. Compare to live `devices` plus 1.5 GiB reserve.

---

## W

**wait-ready** — `tts.cmd wait-ready --model kokoro --timeout 60`. Exit 3 on timeout.

**waveform_temperature** — Bark-only, default 0.7.

**WebView2** — Fixed Version GUI browser bundled under `runtime/webview2`; profile in `cache/webview2`.

**whisper** — ASR engine for SRT, verify, Transcribe. Sizes tiny/base/small/medium/large. Not a speaker.

**Worker** — One process, one model (or one Whisper size), port 8101–8200. Testing dropdown lists ready/busy workers.

**workers scale** — `POST /api/models/{model}/scale` `{count:0-16}`.

**WSL2** — Host requirement. Dedicated folder-specific distro; Windows registration points to this copy’s `wsl` directory.

---

## X

**X-TTS-API-Token** — Auth header name (`TOKEN_HEADER` in `tts.py`).

**xtts** — XTTS v2. 58 named speakers, 17 languages, clone or built-in, ~1.8GB, ~3 GiB VRAM, 250-char chunks.

---

## Y

**yes (project delete)** — `tts.cmd project delete NAME --yes` is required; delete is irreversible.

---

## Z

**zip** — `tts.cmd jobs zip`, `project zip`, `GET /api/jobs/{id}/zip`, `GET /api/projects/{name}/zip`. Handoff bundles.

**Zoom** — Editor waveform 1×–40×.

---

## Cross-links

- [Identity and requirements](identity-and-requirements.md) — linux distro for windows users, every requirement
- [Start, stop, and portability](start-stop-and-portability.md)
- [GUI overview](gui-overview.md) — Setup, Server, Voices, Testing, Editor, Log, Shutdown
- [Windows CLI (`tts.cmd`)](windows-cli.md)
- [Authenticated HTTP API](http-api.md) — token, discovery, generate, jobs, voices, setup, workers, SRT, audio editor, maintenance
- [Engines catalog](engines-catalog.md) — bark chatterbox dia f5 fish higgs kokoro qwen vibevoice whisper xtts speecht5 parler outetts vits edge voxtral voxcpm2 csm orpheus
- [Troubleshooting](troubleshooting.md)
