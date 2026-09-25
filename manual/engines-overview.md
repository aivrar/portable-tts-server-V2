# Engines overview

An **engine** is a `MODEL_SETUP` key: `bark`, `chatterbox`, `dia`, `f5`, `fish`, `higgs`, `kokoro`, `qwen`, `vibevoice`, `whisper`, `xtts`, `speecht5`, `parler`, `outetts`, `vits`, `edge`, `voxtral`, `voxcpm2`, `csm`, `orpheus`. The GUI Setup cards, the Server spawn list (except whisper), `tts.cmd tts <model>`, and `/api/tts/{model}` all use these ids.

Whisper is an engine you **install and load**, but it does not speak. It transcribes for verification, SRT, and the Voices-tab **Transcribe** button.

## Shared lifecycle (every speaking engine)

1. **Install** — Setup card **Install**, or `tts.cmd install <id> --wait`, or `POST /api/setup/install/{id}`. Wait until `GET /api/setup/status` reports `ready` (or `packages_only` for engines that fetch weights on first use, notably vits).
2. **Check devices** — `tts.cmd devices` / `GET /api/devices`. Compare free VRAM to `MODEL_VRAM_ESTIMATE_GB` plus the 1.5 GiB reserve.
3. **Load** — Server tab **Spawn Worker**, or `tts.cmd model load <id> --device cuda:0`, or `POST /api/models/{id}/load`. Poll until a worker is `ready`. Cold-load times vary from seconds to several minutes.
4. **Inspect voices and schema** — `tts.cmd schema <id>`, `tts.cmd capabilities <id>`, `GET /api/tts/{id}/voices`.
5. **Optional dry run** — `tts.cmd dryrun <id> --text "..."`.
6. **Speak** — Testing tab **Generate**, or `tts.cmd tts <id> --text "..."`, or `POST /api/tts/{id}` / `/submit`.
7. **Unload** — Server **Kill**, `tts.cmd model unload <id>`, `POST /api/models/{id}/unload`. Unload terminates the **process group** and runs targeted `posix_fadvise` on that model's files. It does not `drop_caches` for the whole VM.

Never load two huge engines at once on a single 24 GB card (qwen + voxtral will not fit with the reserve). Edge and VITS are the exceptions that do not need GPU.

## How cloning vs built-in vs style works

`MODEL_SETUP` flags drive the Testing tab:

| Flag | UI / API meaning |
| --- | --- |
| `builtin_voices` | Voice dropdown from `/api/tts/{model}/voices` |
| `ref_audio` | Saved Voice / Or Upload row |
| `ref_text` | Reference Text field (exact transcript of the clip) |
| `style_tags` | Insert style tag row |

Pass a reference as:

- GUI Saved Voice → `voice` = filename in `voices/`
- GUI Upload → multipart `reference_audio`
- CLI `--ref FILE` → base64 plus `reference_audio_name`
- CLI/API `--voice path-or-name`
- VibeVoice also accepts JSON `reference_audios` (one to four paths in speaker order)

XTTS `mode`: `"cloned"` (default) vs `"built-in"` when `voice` is a named speaker such as `Daisy Studious`.

## Planning table (VRAM estimate, timeout, chunk size, sample rate)

Estimates are **not** hard CUDA limits. Generation length, precision, and cloning inputs add transient allocations.

| id | Display | VRAM est. GiB | Infer timeout s | Chunk chars | Sample rate | GPU? |
| --- | --- | --- | --- | --- | --- | --- |
| edge | Edge TTS | 0 | 120 | 3000 | 24000 | Cloud, no GPU |
| vits | VITS | 0 | 60 | 250 | 22050 | No GPU needed |
| kokoro | Kokoro 82M | 1 | 120 | 500 | 24000 | CPU or GPU |
| speecht5 | SpeechT5 | 1 | 120 | 250 | 16000 | CPU or GPU |
| parler | Parler-TTS | 2 | 90 | 300 | 44100 | GPU preferred |
| f5 | F5-TTS | 3 | 600 | 250 | 24000 | GPU |
| xtts | XTTS v2 | 3 | 600 | 250 | 24000 | GPU |
| outetts | OuteTTS 1.0 0.6B | 3 | 180 | 250 | 44100 | GPU |
| chatterbox | Chatterbox | 4 | 600 | 250 | 24000 | GPU |
| csm | Sesame CSM-1B | 5 | 300 | 300 | 24000 | GPU |
| fish | Fish Speech | 5 | 180 | 250 | 24000 | GPU |
| bark | Bark | 6 | 600 | 200 | 24000 | GPU |
| vibevoice | VibeVoice | 7 | 300 | 800 | 24000 | GPU |
| dia | Dia 1.6B | 8 | 180 | 400 | 44100 | GPU |
| voxcpm2 | VoxCPM2 | 8 | 300 | 400 | 48000 | GPU |
| orpheus | Orpheus 3B | 8 | 300 | 200 | 24000 | GPU |
| whisper | Whisper | 10 (large) | n/a | n/a | n/a | GPU for large |
| higgs | Higgs Audio 3B | 16 | 180 | 500 | 24000 | Large GPU; CPU listed on the Setup card |
| qwen | Qwen Omni 7B | 16 | 180 | 500 | 24000 | Large GPU |
| voxtral | Voxtral 4B TTS | 20 | 300 | 600 | 24000 | Very large GPU |

Whisper size VRAM from config: tiny ~1GB, base ~1GB, small ~2GB, medium ~5GB, large ~10GB.

## Override directories

Conflicting Python stacks live under `/opt/tts_server/overrides/<name>`. Shared **coqui** serves bark, xtts, and vits. Removing one of those three does not delete Coqui while another remains. kokoro, whisper, speecht5, edge, and csm use the base venv only.

## Text chunking you should know about

The gateway splits long `text` before inference. Engine-aware rules:

- **VibeVoice** — keeps `Speaker N:` line boundaries; repeats the speaker prefix on fragments; max 800 chars.
- **Dia** — repeats the active `[S1]` / `[S2]` tag on each fragment; max 400 chars.
- **VoxCPM2** — repeats a leading `(voice design)` on every fragment; max 400 chars.

If a take cuts off speaker two on Dia, the text was too long for one chunk's token budget; split turns yourself or rely on the bounded chunker.

## Choosing an engine (usage, not marketing)

| You want | Start with |
| --- | --- |
| Fast cloud, 300+ languages/locales, no install weight | **edge** |
| Fast local narration, many compact voices | **kokoro** |
| Lightweight CPU multi-speaker | **speecht5** |
| Fast CPU single English speaker | **vits** |
| Named speakers + multilingual clone | **xtts** |
| Two-person script with laughs | **dia** |
| Emotion slider on a cloned voice | **chatterbox** |
| Clone from reference + transcript (diffusion) | **f5** |
| Clone + emotion/tone markers, fast local | **fish** |
| Describe the voice in English prose | **parler** (or **voxcpm2** parenthesized design) |
| Long-form / scene description / clone | **higgs** |
| Two built-in voices only (Chelsie / Ethan) | **qwen** |
| Multi-speaker `Speaker N:` narration | **vibevoice** |
| 14-language compact clone | **outetts** |
| 9-language presets + clone, non-commercial | **voxtral** |
| 48 kHz design/clone, 30 languages | **voxcpm2** |
| Conversational continuation from a clip | **csm** |
| English character + `<laugh>` tags | **orpheus** |
| Expressive non-speech effects, history chaining | **bark** |
| Captions / verify speech | **whisper** |

Read [engines-catalog.md](engines-catalog.md) for install files, speak examples, clone rules, and failure notes per id.

## Related pages

- [Setup tab](gui-setup.md)
- [Server tab](gui-server.md)
- [Testing tab](gui-testing.md)
- [Engines catalog](engines-catalog.md)
- [A to Z knowledge page](a-to-z.md)
