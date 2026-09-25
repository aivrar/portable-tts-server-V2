# Identity and requirements

Portable TTS Server V2 is a **linux distro for windows users**. Windows provides a native desktop window and CLI; inference and audio processing run inside this app's dedicated WSL2 distro. The complete release includes its own dependencies and Kokoro model data. Optional engines install inside its portable disk when chosen.

## Product identity and paths

| Item | Location or behavior |
| --- | --- |
| Windows app root | Any writable folder on a local drive; `E:\tts_server` is an example |
| WSL distro | `TTS-Server-V2-<12-character path hash>` |
| Portable Linux disk | `<app>\wsl\ext4.vhdx` |
| Linux-private state | `/opt/tts_server` in that disk |
| Clean initial runtime | `<app>\runtime\linux-rootfs.tar.gz` |
| Native launcher | `<app>\runtime\launcher\TTSServer.exe` |
| Browser runtime | `<app>\runtime\webview2` |
| Browser profile | `<app>\cache\webview2` |
| Windows CLI Python | `<app>\runtime\python\python.exe` |
| Start / stop | `Start-TTSServer.cmd` / `Stop-TTSServer.cmd` |
| Discovery | `<app>\output\run\registry\tts_server.json` |
| Default ports | Bridge 9300, gateway 8300; customizable at startup |

The legacy distro name `linbox-TTS_Server` is accepted only when its registered disk already belongs to this exact folder. Startup validates the WSL registration against the folder, rather than selecting a default Ubuntu distro. Linux-private paths stay within `/opt/tts_server`; host-visible paths stay within the app tree.

## Host requirements

### Windows and WSL2

Use Windows x64 with WSL2 and hardware virtualization enabled. Check `wsl --status`. If WSL is missing, install it from an administrator terminal with `wsl --install --no-distribution`, then restart if requested. WSL is a Windows component and cannot be made folder-local by this app. WSL1 and Windows ARM packages are not provided.

### App dependencies are bundled

The release supplies Windows Python for `tts.cmd`, a self-contained .NET native launcher, WebView2 Fixed Version, and Linux Python, PyTorch, FFmpeg, espeak-ng, SoX, rubberband, and speech dependencies. No separate Python, .NET, WebView2, or CUDA toolkit install is required for the complete release. Source checkouts need the build procedure in [release/BUILD.md](../release/BUILD.md).

### Offline Kokoro and optional engines

Kokoro weights, voices, the English spaCy model, Japanese UniDic data, and Chinese pronunciation dependencies are included. Nine Kokoro language groups are checked by actual CPU synthesis with Python network connections blocked. No Hugging Face token is required for bundled Kokoro.

Choose other engines in Setup; their packages and weights download into this app's VHD. Gated engines require your own Hugging Face access. Whisper transcription/subtitle weights are optional downloads. Edge uses an online service for every request. Offline availability is specific to installed engines and their selected language/voice assets.

### Folder and GPU

Extract the whole release into a writable local folder. Spaces are supported; UNC/network shares are not. The first start imports the bundled image into `wsl/ext4.vhdx`. Later starts use that disk. Run Stop before moving or copying it; see [start and portability](start-stop-and-portability.md).

Kokoro can run on CPU. For GPU use, install a compatible Windows NVIDIA driver with WSL support. The CUDA user-space libraries are bundled, but a host GPU driver is not. GPU numbers vary by computer; choose a currently listed device.

## Disk space for engines and weights

Weights and override packages live on the VHDX under `/opt/tts_server/data` and `/opt/tts_server/overrides`, not on the small Windows-visible folders. Plan space **on the VHDX / the local volume that holds it**. Published `weights_size` values from `MODEL_SETUP` (these are the installer-facing sizes, not VRAM):

| Engine id | Display | Approximate weight size |
| --- | --- | --- |
| `edge` | Edge TTS | none (cloud) |
| `whisper` | Whisper | ~145MB base; other sizes optional |
| `vits` | VITS | ~150MB |
| `speecht5` | SpeechT5 | ~250MB |
| `kokoro` | Kokoro 82M | ~300MB |
| `f5` | F5-TTS | ~1.35GB selected checkpoint |
| `outetts` | OuteTTS 1.0 0.6B | ~1.22GB model + pinned 24kHz DAC |
| `xtts` | XTTS v2 | ~1.8GB |
| `fish` | Fish Speech | ~3.4GB model + codec |
| `parler` | Parler-TTS | ~3.6GB + tokenizer |
| `voxcpm2` | VoxCPM2 | ~4.96GB selected checkpoint |
| `vibevoice` | VibeVoice | ~5.41GB checkpoint + local Qwen tokenizer |
| `dia` | Dia 1.6B | ~6.45GB selected checkpoint + local DAC decoder |
| `orpheus` | Orpheus 3B | ~6.72GB selected FP16 checkpoint + SNAC decoder |
| `csm` | Sesame CSM-1B | ~7.15GB selected Transformers checkpoint |
| `voxtral` | Voxtral 4B TTS | ~8.03GB selected checkpoint and preset embeddings |
| `bark` | Bark | ~12GB + voice prompts |
| `higgs` | Higgs Audio 3B | ~12GB model + audio tokenizer + HuBERT |
| `qwen` | Qwen Omni 7B | ~21GB selected runtime snapshot |
| `chatterbox` | Chatterbox | English runtime assets only (pinned files) |

Installing **all** local engines needs tens of gigabytes on the VHDX plus Hugging Face / pip caches under `/opt/tts_server/cache`. User audio (jobs, projects, voices) additionally grows `E:\tts_server\output` and `E:\tts_server\projects_output`. Use `tts.cmd disk` or `GET /api/disk` to see per-directory size and mount free space.

### 8. GPU vs CPU vs cloud, as the engines actually apply

VRAM planning estimates (`MODEL_VRAM_ESTIMATE_GB`) are conservative numbers for orchestrators, not hard CUDA limits. The gateway also keeps a safety reserve (`TTS_SERVER_GPU_RESERVE_GB`, default **1.5 GiB**) and serializes loads per GPU.

| Class | Engine ids | What you need |
| --- | --- | --- |
| Cloud, no GPU, no weights | `edge` | Working internet. Live catalog of 300+ Microsoft neural voices. |
| CPU-friendly local | `vits`, `speecht5`, `kokoro` (Kokoro also runs well on GPU) | Modest RAM. VITS is documented as no GPU needed. Higgs lists CPU support in its Setup card but is a large load. |
| Typical local GPU | `xtts`, `f5`, `chatterbox`, `outetts`, `csm`, `fish`, `bark`, `dia`, `orpheus`, `voxcpm2`, `vibevoice` | NVIDIA GPU visible to WSL2 (`nvidia-smi` inside the distro). Estimates range from ~1 GB (kokoro/speecht5) to ~8 GB (dia/orpheus/voxcpm2). |
| Large GPU | `higgs` (~16 GB estimate, ~14.8 GiB observed), `qwen` (~16 GB estimate, ~7 GiB settled after a heavy load), `voxtral` (~20 GB estimate, ~19.5 GiB observed) | A high-VRAM NVIDIA card. Unload immediately after a batch. |
| Verification / captions | `whisper` | tiny/base are ~1 GB; `large` is ~10 GB. Used for SRT and optional verify-while-generating. |

Default worker device is `cuda:0` when `nvidia-smi` succeeds, otherwise `cpu`. Edge never uses the GPU. A machine with no NVIDIA GPU can still use Edge, VITS, SpeechT5, and (slowly) other engines on CPU; large GPU-first engines will be painful or will fail the reserve check.

### 9. A Hugging Face token for gated models

Many weight repos are on Hugging Face and some are gated. The Setup tab's **HuggingFace Token** field (`hf_...`) saves to `E:\tts_server\secrets\hf_token` (Linux path `/mnt/e/tts_server/secrets/hf_token`). Create a token at `huggingface.co/settings/tokens`. For fine-grained tokens, enable "Read access to contents of all public gated repos you can access". Classic Read tokens work out of the box. You must also accept each model's license on its Hugging Face page before the download will succeed.

CLI equivalents:

```cmd
tts.cmd hf-token get
tts.cmd hf-token set hf_yourtokenhere
```

Empty string / empty Save removes the token. Edge TTS does not need a Hugging Face token. Local engines that pull gated or authenticated Hub files do.

### 10. Network for first-time installs and cloud Edge TTS

For optional engines, first-time **Install** on the Setup tab clones packages and downloads weights (Hugging Face Hub). That requires outbound HTTPS. **Edge TTS** is a cloud API: every Generate call needs internet, and the live voice catalog is fetched (cached six hours, with a static fallback of common `en-US-JennyNeural`-style names if the catalog request fails). After weights are on the VHDX, local engines can speak offline. Clearing the `hub` cache is dangerous for engines that treat Hub as their offline store; prefer `tmp` / `pip` when cleaning.

## What you get once those requirements are met

- A WebView2 window titled **Portable TTS Server V2** with tabs **Setup**, **Server**, **Voices**, **Testing**, **Editor**, **Log**, plus **Shutdown**.
- An authenticated HTTP API (token in `X-TTS-API-Token`, also accepted as `Authorization: Bearer`; the GUI's EventSource log stream uses `?token=` because it cannot set a header).
- A Windows CLI at `E:\tts_server\tts.cmd` that auto-discovers the URL and token from the registry.
- Reference voices in `E:\tts_server\voices`, jobs in `E:\tts_server\output\jobs`, named projects in `E:\tts_server\projects_output`.

## Related pages

- [Start, stop, and portability](start-stop-and-portability.md)
- [Storage, layout, and ports](storage-layout-and-ports.md)
- [Troubleshooting](troubleshooting.md)
- [A to Z knowledge page](a-to-z.md)
