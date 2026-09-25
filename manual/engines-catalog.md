# Engines catalog

Every `MODEL_SETUP` id, in the order the Setup tab cards are defined. For each engine: identity, install, load, speak, clone/controls, VRAM/CPU/cloud, and failure notes taken from the live catalog, voice notes, and guarded probes. Do not invent flags; if a control is not listed here, `tts.cmd schema <id>` is authoritative.

Shared install command (replace the id):

```cmd
tts.cmd install kokoro --wait --timeout 3600
tts.cmd model load kokoro --device cuda:0
tts.cmd wait-ready --model kokoro --timeout 180
tts.cmd tts kokoro --text "The lantern glows beside us." --out lantern.wav
tts.cmd model unload kokoro
```

---

## bark — Bark

- **Display:** Bark
- **Desc:** Expressive TTS - laughter, music, emotions
- **Hub:** `erogol/bark`
- **Weights dir:** `coqui/tts/tts_models--multilingual--multi-dataset--bark` plus extra `bark-voices`
- **Size:** ~12GB + voice prompts
- **Override:** `coqui` (shared with xtts and vits)
- **Flags:** ref_audio no, ref_text no, style_tags **yes**, builtin_voices **yes**
- **VRAM estimate:** 6 GiB. **Timeout:** 600s (slowest; autoregressive 3-stage). **Chunk:** 200 chars (~13s audio per call). **Rate:** 24000. **Profile:** history_reset_every 5.

### Install / load

Install from Setup or `tts.cmd install bark --wait`. Requires disk for ~12GB plus voice prompts. Load on GPU; cold load in the guarded probe was about one minute.

### Speak

130 bundled `v2/` presets: languages `en de es fr hi it ja ko pl pt ru tr zh` × speakers `0–9`. Default `v2/en_speaker_6`. Voice stays consistent across chunks via **history prompt chaining** (reset every 5 chunks per the audio profile).

```cmd
tts.cmd tts bark --text "[laughs] That was fun!" --voice v2/en_speaker_6 --out bark.wav
```

Testing tab Insert tags: `[laughs] `, `[sighs] `, `[gasps] `, `[clears throat] `, `[music] `. Non-speech tags produce timed gaps and are omitted from spoken subtitles. Prefer Bark for **character**, not exact narration (guarded probe 17/19 words). Short generation ~37–55 seconds.

### Clone / controls

No reference-audio cloning in this app. Controls: `temperature` 0.7, `waveform_temperature` 0.7. Unload after a batch; 600s infer timeout kills a stuck worker.

---

## chatterbox — Chatterbox

- **Display:** Chatterbox
- **Desc:** Emotion control, voice cloning
- **Hub:** `ResembleAI/chatterbox`
- **Required files:** `ve.safetensors`, `t3_cfg.safetensors`, `s3gen.safetensors`, `tokenizer.json`, `conds.pt`
- **Size:** English runtime assets only (setup is pinned for Perth compatibility)
- **Override:** `chatterbox`
- **Flags:** ref_audio **yes**, ref_text no, style_tags no, builtin_voices no
- **VRAM:** 4 GiB. **Timeout:** 600s. **Chunk:** 250. **Rate:** 24000.

### Install / load / speak / clone

Install, then spawn on GPU. There is no built-in voice list — you **must** clone from Saved Voice or `--ref`.

Controls: `temperature` 0.8, `repetition_penalty` 1.2, `exaggeration` 0.5 (emotion; GUI slider 0–1, API allows 0–2), `cfg_weight` 0.5 (voice match).

```cmd
tts.cmd tts chatterbox --text "The lantern glows beside us." --ref E:\tts_server\voices\angie_female.wav --out cb.wav
```

Guarded probe at 0.5/0.5 recovered 15/16 words (initial "The" omitted); timed SRT works. Unload after use.

---

## dia — Dia 1.6B

- **Display:** Dia 1.6B
- **Desc:** Dialogue TTS with [S1]/[S2] speaker tags
- **Hub:** `nari-labs/Dia-1.6B-0626`
- **Required:** `config.json`, `dia-v1.pth` plus extra `dia-dac`
- **Size:** ~6.45GB selected checkpoint + local DAC decoder
- **Override:** `dia`
- **Flags:** ref_audio **yes**, ref_text no, style_tags **yes**, builtin_voices no
- **VRAM:** 8 GiB. **Timeout:** 180s. **Chunk:** 400. **Rate:** 44100.

### Speak

Write turns inline:

```text
[S1] Hello there! (laughs) [S2] Hey, how are you?
```

Insert tags: `(laughs) `, `(sighs) `, `(clears throat) `, `(coughs) `, `(gasps) `. Optional reference audio for cloning. Chunker repeats the active `[S1]`/`[S2]` tag on fragments.

Guarded 3090 probe: first run's token budget cut off speaker two; bounded per-turn headroom fixed that. Retest produced both turns in 59 seconds; Whisper recovered 6/7 intended spoken words (final "clearly" missed). Cold load ~145 seconds plus a 38-second one-time warm-up. **Unload immediately after use.**

Defaults: temperature 1.8, cfg_scale 3.0, top_p 0.90, top_k 50.

---

## f5 — F5-TTS

- **Display:** F5-TTS
- **Desc:** Diffusion TTS, reference audio cloning
- **Hub:** `SWivid/F5-TTS`
- **Required:** `F5TTS_v1_Base/model_1250000.safetensors`, `F5TTS_v1_Base/vocab.txt`
- **Size:** ~1.35GB selected checkpoint (default v1 only)
- **Override:** `f5`
- **Flags:** ref_audio **yes**, ref_text **yes**, style_tags no, builtin_voices no
- **VRAM:** 3 GiB. **Timeout:** 600s (diffusion nfe_step loop). **Chunk:** 250. **Rate:** 24000.

### Clone

Requires **reference audio plus its transcript**. Testing tab shows Reference Text. Defaults: `nfe_step` 32, `cfg_scale` 2.0, `seed` 0. Higher NFE can improve quality and runtime.

```cmd
tts.cmd tts f5 --text "The lantern glows beside us." --ref E:\tts_server\voices\f5_english_ref.wav --reference-text "exact words spoken in that wav" --out f5.wav
```

Guarded probe with an existing app voice at 32-step/CFG 2.0: exact 16/16-word transcript and timed SRT; 24 kHz.

---

## fish — Fish Speech

- **Display:** Fish Speech
- **Desc:** Fast TTS with voice cloning (OpenAudio S1-mini)
- **Hub:** `fishaudio/openaudio-s1-mini`
- **Required:** `model.pth`, `codec.pth`, `config.json`, `tokenizer.tiktoken`, `special_tokens.json`
- **Size:** ~3.4GB model + codec
- **Override:** `fish`
- **Flags:** ref_audio **yes**, ref_text **yes**, style_tags **yes**, builtin_voices no
- **VRAM:** 5 GiB (~4.4 GiB observed). **Timeout:** 180s. **Chunk:** 250. **Rate:** 24000.
- **Temperature slider override:** max **1.0** (ServeTTSRequest).

### Speak / clone

Works **without** a reference, or clones from a short, clean, dry WAV. For cloning, `voice` + exact `reference_text`. Prefer **5–15 second** single-speaker; long audiobook clips reduced intelligibility. Keep API defaults 0.8 temperature/top-p, 1.1 repetition penalty, seed 0.

Style inserts: `(excited) `, `(sad) `, `(angry) `, `(whispering) `, `(shouting) `, `(laughing) `, `(sighing) `. Example text: `The silver river flows quietly.`

Setup pins S1-compatible source and exact tiktoken IDs; the later S2 tokenizer produced gibberish with S1-mini. Cold load 134 seconds. Five-word default-voice sample inferred in 22.5 seconds; Whisper recovered 4/5 words.

---

## higgs — Higgs Audio 3B

- **Display:** Higgs Audio 3B
- **Desc:** Boson AI ChatML (CPU supported)
- **Hub:** `bosonai/higgs-audio-v2-generation-3B-base`
- **Required:** config, index, three `model-0000N-of-00003.safetensors`, tokenizer.json; extras `higgs-audio-tokenizer`, `hubert_base`
- **Size:** ~12GB model + audio tokenizer + HuBERT
- **Override:** `higgs` (reuses base Torch; isolated Python layer ~235 MiB)
- **Flags:** ref_audio **yes**, ref_text **yes**, style_tags no, builtin_voices no
- **VRAM:** 16 GiB estimate (~14.8 GiB observed). **Timeout:** 180s. **Chunk:** 500. **Rate:** 24000.

### Speak

Works without a reference, with **Scene / Speaker Description** (`voice_description`, e.g. `SPEAKER0: warm mature voice; moderate pace; quiet studio`), or clones from reference + exact transcript. Best for long-form and dialogue-style prosody.

Defaults: temperature 0.3, top_p 0.95, top_k 50, seed 0.

Cold load 252 seconds on a 3090. Keep the worker warm for a batch, then **unload**. Capped five-word request inferred in 26.6 seconds, 2.0 seconds of audio; Whisper 4/5 words ("blows" for "glows") with timed SRT.

CPU is listed on the Setup card; GPU is the practical path.

---

## kokoro — Kokoro 82M

- **Display:** Kokoro 82M
- **Desc:** Lightweight, fast, 54 built-in voices
- **Hub:** `hexgrad/Kokoro-82M`
- **Size:** ~300MB
- **Override:** none (base venv)
- **Flags:** builtin_voices **yes**; no ref_audio, ref_text, or style_tags
- **VRAM:** 1 GiB. **Timeout:** 120s. **Chunk:** 500. **Rate:** 24000. **Profile:** extra front pad 0.15s, front_protect 250ms, end_protect 1100ms.

### Speak

Voice list is scanned from `/opt/tts_server/data/kokoro/voices/*.pt`; fallback `af_heart` if the folder is empty. Prefix meanings: `a` American, `b` British, `j` Japanese, `z` Mandarin, `e` Spanish, `f` French, `h` Hindi, `i` Italian, `p` Portuguese; `f`/`m` after the prefix = female/male (example `af_bella`, `af_heart`).

No engine-native sliders (speed is post-process). Guarded probe: exact 16/16-word transcript. Best first local engine.

```cmd
tts.cmd tts kokoro --text "hello world" --voice af_heart --out hello.wav
```

---

## qwen — Qwen Omni 7B

- **Display:** Qwen Omni 7B
- **Desc:** Two-voice speech output from Qwen Omni
- **Hub:** `Qwen/Qwen2.5-Omni-7B`
- **Required:** config, index, five safetensor shards, `spk_dict.pt` (SHA-256 validated), tokenizer.json
- **Size:** ~21GB selected runtime snapshot
- **Override:** `qwen` (bitsandbytes only; shared Torch/CUDA untouched)
- **Flags:** builtin_voices **yes** (Chelsie, Ethan); no cloning
- **VRAM:** 16 GiB estimate. Observed: cold load 412 seconds, peaked near 22 GiB temporary RAM, settled ~7.0 GiB VRAM / 1 GiB RAM. **Timeout:** 180s. **Chunk:** 500. **Rate:** 24000.

### Speak

Chelsie = warm and clear (default). Ethan = bright and energetic. **Does not clone** uploaded voices.

```cmd
tts.cmd tts qwen --text "The lantern glows beside us." --voice Chelsie --seed 0 --out qwen.wav
```

Defaults: temperature 0.9, top_p 0.8, top_k 40, seed 0. Isolated 4-bit loading. Source-guided SRT recovered 5/5 words after raw Whisper got 3/5 on a short probe. Unload after a batch.

---

## vibevoice — VibeVoice

- **Display:** VibeVoice
- **Desc:** Long-form turn-based TTS with up to four voices
- **Hub:** `microsoft/VibeVoice-1.5B`
- **Required:** config, preprocessor_config, index, three shards; extra `vibevoice-qwen-tokenizer`
- **Size:** ~5.41GB checkpoint + local Qwen tokenizer
- **Override:** `vibevoice` (~268 MiB isolated layer, reuses base Torch)
- **Flags:** ref_audio **yes**, ref_text no, style_tags no, builtin_voices no
- **VRAM:** 7 GiB (~5.9 GiB observed). **Timeout:** 300s. **Chunk:** 800 (long-form ≤90 min class). **Rate:** 24000.
- **GUI:** Testing tab **requires** a reference voice.

### Speak

Natural **turn-taking**, not overlapping speech. Text:

```text
Speaker 1: Welcome.
Speaker 2: Glad to be here.
```

Pass one to four voice paths in JSON `reference_audios` in speaker order; singular `voice` / `reference_audio` also works. Chunker preserves speaker lines and repeats `Speaker N:` on fragments.

Defaults: cfg_scale 1.3, seed 0. Cold load 127 seconds. Two-speaker 79-character request: 36 seconds infer, 5.87 seconds audio; source-guided SRT 15/15 words. Unload after a batch.

---

## whisper — Whisper

- **Display:** Whisper
- **Desc:** Speech recognition for verification
- **Hub:** `openai/whisper-base`
- **Required:** `base.pt`
- **Size:** ~145MB base; other sizes optional
- **Override:** none
- **Flags:** all false (it does not speak)
- **VRAM:** tiny/base ~1GB, small ~2GB, medium ~5GB, large ~10GB. Estimate map lists 10 (plan for large).

### Install / load / use

Install the Setup card (base). Load a size:

```cmd
tts.cmd whisper load base
tts.cmd whisper info
tts.cmd voices transcribe me.wav --size base
tts.cmd whisper unload base
```

API: `POST /api/whisper/{size}/load` and `/unload`. Used by:

- Testing tab **Enable verification** (`verify_whisper`, `whisper_model`, `tolerance` 80)
- `POST /api/jobs/{id}/srt`
- `generate_srt` on a TTS request (auto-spawns Whisper; if not ready after 60s, SRT may be skipped)
- Voices **Transcribe**
- `POST /api/transcribe` and `/transcribe/upload`

Tasks: `transcribe` (default) or `translate` (to English). `language` is ISO 639-1 (`en`, `ja`). Always unload Whisper after captions so its VRAM is free.

---

## xtts — XTTS v2

- **Display:** XTTS v2
- **Desc:** Multilingual voice cloning, 58 built-in voices
- **Hub:** `coqui/XTTS-v2`
- **Size:** ~1.8GB
- **Override:** `coqui`
- **Flags:** ref_audio **yes**, ref_text no, style_tags no, builtin_voices **yes**
- **VRAM:** 3 GiB. **Timeout:** 600s. **Chunk:** 250 (hard cap ~273 chars). **Rate:** 24000.

### Built-in voices (58)

Aaron Dreschner, Abrahan Mack, Adde Michal, Alexandra Hisakawa, Alison Dietlinde, Alma María, Ana Florence, Andrew Chipper, Annmarie Nele, Asya Anara, Badr Odhiambo, Baldur Sanjin, Barbora MacLean, Brenda Stern, Camilla Holmström, Chandra MacFarland, Claribel Dervla, Craig Gutsy, Daisy Studious, Damien Black, Damjan Chapman, Dionisio Schuyler, Eugenio Mataracı, Ferran Simen, Filip Traverse, Gilberto Mathias, Gitta Nikolina, Gracie Wise, Henriette Usha, Ige Behringer, Ilkin Urbano, Kazuhiko Atallah, Kumar Dahl, Lidiya Szekeres, Lilya Stainthorpe, Ludvig Milivoj, Luis Moray, Maja Ruoho, Marcos Rudaski, Narelle Moon, Nova Hogarth, Rosemary Okafor, Royston Min, Sofia Hellen, Suad Qasim, Szofi Granger, Tammie Ema, Tammy Grit, Tanja Adelina, Torcull Diarmuid, Uta Obando, Viktor Eka, Viktor Menelaos, Vjollca Johnnie, Wulf Carlevaro, Xavier Hayasaka, Zacharie Aimilios, Zofija Kendrick.

### Speak

Built-in: set `voice` to a speaker name and `mode` to `built-in`. Clone: `voice` / `--ref` to a WAV and `mode` `cloned` (API default).

**Language** select (Testing field): en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, ko, hu, hi. Pick the language that matches the input text.

Defaults: temperature 0.65, repetition_penalty 2.0, top_k 50, top_p 0.85.

```cmd
tts.cmd tts xtts --text "Hello from XTTS." --voice "Daisy Studious" --language en --out xtts.wav
tts.cmd tts xtts --text "Hello from a clone." --ref E:\tts_server\voices\angie_female.wav --out xtts_clone.wav
```

Guarded probe: both modes transcribed exactly 16/16 words. Allow roughly a minute for a cold load.

---

## speecht5 — SpeechT5

- **Display:** SpeechT5
- **Desc:** Microsoft HF-native, multi-speaker, lightweight
- **Hub:** `microsoft/speecht5_tts`
- **Size:** ~250MB
- **Override:** none
- **Flags:** builtin_voices **yes**; no clone
- **VRAM:** 1 GiB. **Timeout:** 120s. **Chunk:** 250. **Rate:** 16000.

### Speak

Seven CMU-Arctic speaker embeddings: `female_1`, `male_1`, `female_2`, `male_2`, `female_3`, `male_3`, `neutral`. No native sliders. Spell acronyms ("API" was heard as "app I"). Verified after a Parquet compatibility fix. Good CPU option.

---

## parler — Parler-TTS

- **Display:** Parler-TTS
- **Desc:** Describe the voice in text — style-prompted TTS
- **Hub:** `parler-tts/parler-tts-mini-v1.1`
- **Required:** `model.safetensors`, `config.json`, `tokenizer.json`; extra `parler-desc-tokenizer`
- **Size:** ~3.6GB + tokenizer
- **Override:** `parler`
- **Flags:** builtin_voices **yes** (example descriptions); no ref_audio
- **VRAM:** 2 GiB (~1.75 GiB observed). **Timeout:** 90s (kills/unloads the worker). **Chunk:** 300. **Rate:** 44100.

### Speak

The `voice` field **is a prose description**, not a filename:

```text
A warm female voice, clear and close up, moderate speed
A deep male voice, slightly slow, very clear recording
Jon's voice is monotone yet slightly fast, very close recording
Laura's voice is expressive and animated, moderate speed, high quality
```

Also send `voice_description` from the Testing field. Use speaker names (Jon, Laura) for consistency. Punctuation controls pacing. Default temperature 1.0.

Decoder is bounded (old unbounded path took 217 seconds). Probe: calm/warm/close-miked style + five words in 22.3 seconds, 3.18-second 44.1 kHz WAV, Whisper 5/5. Cold load ~147 seconds.

---

## outetts — OuteTTS 1.0 0.6B

- **Display:** OuteTTS 1.0 0.6B
- **Desc:** 14-language TTS with one bundled voice and reusable cloning profiles
- **Hub:** `OuteAI/OuteTTS-1.0-0.6B`
- **Required:** config, model.safetensors, tokenizer.json; extra `outetts-dac` (pinned 24 kHz DAC)
- **Size:** ~1.22GB model + DAC
- **Override:** `outetts` (reuses base Torch; omits llama.cpp, UI/playback, duplicate Whisper)
- **Flags:** ref_audio **yes**, ref_text **yes**, builtin_voices **yes** (`EN-FEMALE-1-NEUTRAL`)
- **VRAM:** 3 GiB (~2.1 GiB observed). **Timeout:** 180s. **Chunk:** 250. **Rate:** 44100 (native interface rate).

### Speak / clone

Only `EN-FEMALE-1-NEUTRAL` is bundled. Cloning: clean reference **no longer than 15 seconds** plus exact `reference_text`. Defaults: temperature 0.4, repetition_penalty 1.1, top_p 0.9, top_k 40, seed 0.

Cold load 52 seconds. Seeded five-word request: 59.3 seconds infer, 2.13 seconds speech, Whisper/source-guided SRT 5/5.

---

## vits — VITS

- **Display:** VITS
- **Desc:** Fast lightweight single-speaker (Coqui, no GPU needed)
- **Hub:** none (`weights_repo` null)
- **Size:** ~150MB
- **Override:** `coqui`
- **Flags:** all false
- **VRAM:** 0. **Timeout:** 60s. **Chunk:** 250. **Rate:** 22050.

### Speak

LJSpeech single English speaker. No cloning, no voice dropdown. Speed is post-process only. Status may be `packages_only` until first use auto-downloads via Coqui TTS. Verified on CPU: exact 16/16-word transcript, timed SRT. Fastest local CPU engine after Edge (which is cloud).

---

## edge — Edge TTS

- **Display:** Edge TTS
- **Desc:** Microsoft neural voices — cloud, 300+ live-catalog voices, no GPU
- **Hub:** none. **Size:** none
- **Override:** none
- **Flags:** builtin_voices **yes**
- **VRAM:** 0. **Timeout:** 120s. **Chunk:** 3000 (cloud limit ~10k chars). **Rate:** 24000.

### Requirements

**Internet.** No Hugging Face token, no GPU, no weight install. Live catalog via `edge_tts.list_voices()` (8s timeout), cached **six hours**. If offline, a static fallback list is used (`en-US-JennyNeural`, `en-US-GuyNeural`, `en-US-AriaNeural`, `en-US-DavisNeural`, `en-US-SaraNeural`, `en-US-AndrewNeural`, `en-GB-SoniaNeural`, `en-GB-RyanNeural`, `en-AU-NatashaNeural`, `en-AU-WilliamNeural`, plus fr/de/es/ja/zh/ko/pt/it names). Format `en-US-JennyNeural`. Details include locale, gender, friendly_name, status.

Controls: **Pitch Hz** (-50..50, default 0), **Volume %** (-50..50, default 0).

```cmd
tts.cmd tts edge --text "The lantern glows beside us." --voice en-US-JennyNeural --pitch 0 --volume 0 --out edge.wav
```

Guarded probe: exact 16/16-word transcript, 7.14-second WAV, exact word-timed SRT, no GPU. Best first engine when online.

---

## voxtral — Voxtral 4B TTS

- **Display:** Voxtral 4B TTS
- **Desc:** Mistral 4B TTS, 9 languages, voice cloning + 20 preset voices
- **Hub:** `mistralai/Voxtral-4B-TTS-2603`
- **Required:** `consolidated.safetensors`, `params.json`, `tekken.json`, `voice_embedding/casual_male.pt`
- **Size:** ~8.03GB selected checkpoint and preset embeddings
- **Override:** `voxtral` (~8.7 GiB isolated vLLM runtime)
- **Flags:** ref_audio **yes**, builtin_voices **yes**
- **VRAM:** 20 GiB estimate (~19.5 GiB observed, 4.8 GiB free on a 24 GB card). **Timeout:** 300s (kills the worker tree). **Chunk:** 600. **Rate:** 24000.
- **License:** CC BY-NC 4.0 — **non-commercial use only**.

### Speak / clone

Presets: `casual_male`, `casual_female`, `cheerful_female`, `neutral_male`, `neutral_female`, `ar_male`, `de_male`, `de_female`, `es_male`, `es_female`, `fr_male`, `fr_female`, `hi_male`, `hi_female`, `it_male`, `it_female`, `nl_male`, `nl_female`, `pt_male`, `pt_female`. Default `casual_male`. Language auto-detected from text (en, fr, es, de, it, pt, nl, ar, hi). Clone: `voice` = a WAV from Voices.

Control: `cfg_alpha` 1.2 (flow-matching guidance; higher = stricter voice match), seed 42.

Cold load 250 seconds. Seeded five-word `casual_male`: 10 seconds, 3.08-second WAV, Whisper 5/5. **One preset was acoustically verified; cloning and the other presets remain capability-level, not quality-tested.** Unload immediately after a batch.

---

## voxcpm2 — VoxCPM2

- **Display:** VoxCPM2
- **Desc:** OpenBMB diffusion-AR TTS, 30 languages, 48kHz, voice cloning + voice design
- **Hub:** `openbmb/VoxCPM2`
- **Required:** `config.json`, `model.safetensors`, `audiovae.pth`, `tokenizer.json`
- **Size:** ~4.96GB selected checkpoint
- **Override:** `voxcpm2` (1.6 MiB inference-only runtime on shared Torch; compile warm-up, denoiser, UI/training packages, hidden retries disabled)
- **Flags:** ref_audio **yes**, ref_text **yes**, style_tags **yes**, builtin_voices no
- **VRAM:** 8 GiB (~5.37 GiB observed). **Timeout:** 300s. **Chunk:** 400. **Rate:** 48000.
- **License:** Apache-2.0. Label synthetic audio; never impersonate without consent.

### Speak

**Voice design:** prepend a parenthesized instruction. Testing Insert replaces a leading `(...)`:

```text
(young woman, warm and calm) The lantern glows beside us.
```

Other inserts: `(mature man, deep and steady) `, `(soft intimate voice, gentle) `, `(young energetic speaker, bright and excited) `. Subtitle normalization strips the non-spoken leading instruction. Chunker repeats the design on every fragment.

**Cloning:** `voice` = WAV; add `reference_text` for "ultimate" cloning fidelity. Cloning modes are capability-level, not quality-tested. Officially supports 30 auto-detected languages and streaming; this app returns **completed jobs** (no streaming playback).

Defaults: cfg_scale 2.0, nfe_step 10. Cold eager load 129.5 seconds. 10-step five-word design request: 27 seconds, 4.27-second WAV, Whisper 5/5.

---

## csm — Sesame CSM-1B

- **Display:** Sesame CSM-1B
- **Desc:** Conversational speech model, natural in dialogue, multi-speaker via context
- **Hub:** `sesame/csm-1b`
- **Required:** config, transformers index + two shards, preprocessor_config, tokenizer.json
- **Size:** ~7.15GB selected Transformers checkpoint
- **Override:** none (native in shared Transformers)
- **Flags:** ref_audio **yes**, ref_text **yes**, builtin_voices **yes**
- **VRAM:** 5 GiB (~3.79 GiB observed). **Timeout:** 300s. **Chunk:** 300. **Rate:** 24000.
- **License:** Apache-2.0.

### Speak

Voices `speaker_0` … `speaker_3` are **conversation role IDs**, not guaranteed fixed identities. For a stable voice, supply a clean reference WAV and its exact transcript. English is the supported language. Defaults: temperature 0.9, top_p 0.95, top_k 50, seed 42.

Cold load ~196 seconds. Five-word request conditioned on an existing app voice: 2.52 seconds infer, 1.52 seconds speech, Whisper 5/5.

---

## orpheus — Orpheus 3B

- **Display:** Orpheus 3B
- **Desc:** Llama-based expressive English TTS with 8 preset voices and inline emotion tags
- **Hub:** `unsloth/orpheus-3b-0.1-ft`
- **Required:** config, index, two shards, tokenizer.json; extra `snac-24khz`
- **Size:** ~6.72GB selected FP16 checkpoint + SNAC decoder
- **Override:** `orpheus` (shared-Torch override below 1 MiB)
- **Flags:** style_tags **yes**, builtin_voices **yes**; no ref_audio (this app exposes the finetune's eight presets, not the pretrained cloning path)
- **VRAM:** 8 GiB (~6.65 GiB observed). **Timeout:** 300s. **Chunk:** 200 (~14s audio per call, SNAC context). **Rate:** 24000.
- **License:** Apache-2.0; do not impersonate without consent.

### Speak

Presets: `tara` (default), `leah`, `jess`, `leo`, `dan`, `mia`, `zac`, `zoe`. Inline non-spoken controls: `<laugh>`, `<sigh>`, `<chuckle>`, `<cough>`, `<sniffle>`, `<groan>`, `<yawn>`, `<gasp>`. Use `repetition_penalty >= 1.1` for stable speech (default 1.1). Other defaults: temperature 0.6, top_p 0.95, seed 42.

```cmd
tts.cmd tts orpheus --text "The lantern glows beside us. <chuckle>" --voice tara --out orpheus.wav
```

Cold load ~175 seconds. Bounded 38-character request with Tara + `<chuckle>`: 1.87 seconds speech without retry; Whisper 5/5. SRT keeps the effect in the original script but omits the control tag from timed cues.

---

## Quick "speak" cheatsheet

```cmd
tts.cmd tts edge --text "Hello." --voice en-US-JennyNeural --out e.wav
tts.cmd tts kokoro --text "Hello." --voice af_heart --out k.wav
tts.cmd tts speecht5 --text "Hello." --voice female_1 --out s.wav
tts.cmd tts vits --text "Hello." --out v.wav
tts.cmd tts xtts --text "Hello." --voice "Daisy Studious" --language en --out x.wav
tts.cmd tts bark --text "[laughs] Hello." --voice v2/en_speaker_6 --out b.wav
tts.cmd tts qwen --text "Hello." --voice Chelsie --out q.wav
tts.cmd tts orpheus --text "Hello. <laugh>" --voice tara --out o.wav
tts.cmd tts parler --text "Hello." --voice "A warm female voice, clear and close up, moderate speed" --out p.wav
tts.cmd tts voxtral --text "Hello." --voice casual_male --out vt.wav
tts.cmd tts csm --text "Hello." --voice speaker_0 --out c.wav
tts.cmd tts dia --text "[S1] Hello. [S2] Hi there." --out d.wav
tts.cmd tts voxcpm2 --text "(young woman, warm and calm) Hello." --out vc.wav
```

Cloning engines need `--ref` (and often `--reference-text`): chatterbox, f5, fish, higgs, vibevoice, outetts, voxcpm2, csm, xtts cloned mode, voxtral clone, dia optional ref.

## Related pages

- [Engines overview](engines-overview.md)
- [Testing tab](gui-testing.md)
- [Setup tab](gui-setup.md)
- [SRT and Whisper](srt-and-whisper.md)
