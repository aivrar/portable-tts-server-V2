# SRT and Whisper

TTS Server captions with **Whisper word timing**, not a forced aligner. Two modes exist. Neither is mathematically perfect timing.

## Modes

| Mode | `source_guided` | SRT text | Timing JSON |
| --- | --- | --- | --- |
| Source-guided (default for TTS jobs) | `true` | Intended script (your job text) | Acoustic clock from Whisper; also retains uncorrected ASR words and text for audit |
| ASR-only | `false` | Only what Whisper recognized | Whisper words |

Source-guided is the right default when you wrote the script and want captions to match it. Use ASR-only when you need to see mispronunciations (verification, or a take that ad-libbed).

Bark non-speech tags such as `[laughs]` produce a timed gap and are omitted from spoken subtitle cues. Orpheus `<chuckle>` stays in the original job script but is omitted from timed cues. VoxCPM2 leading `(voice design)` is stripped from captions.

## Generate SRT for an existing job

HTTP (no dedicated `tts.cmd jobs srt` verb):

```json
POST /api/jobs/{job_id}/srt
{"size": "base", "words_per_line": 4, "source_guided": true}
```

- `words_per_line` 1–20, default **3**
- `size` Whisper size, default **base**
- `source_guided` default **true**

Writes `<stem>.srt` and `<stem>_timing.json` into the job directory and returns the payload inline.

Download:

- `GET /api/jobs/{job_id}/srt` — 404 if you have not generated yet
- `GET /api/jobs/{job_id}/srt/timing` — `{words:[{word,start,end},...], text, language}`

Agent helper:

```powershell
python E:\tts_server\skills\use-tts-server\scripts\tts_api.py POST /api/jobs/<id>/srt --data "{\"size\":\"base\",\"words_per_line\":4,\"source_guided\":true}"
```

Always `POST /api/models/whisper/unload` (or `tts.cmd model unload whisper` / `tts.cmd whisper unload base`) afterward.

## Generate SRT as part of TTS

On `POST /api/tts/{model}` or `/submit`:

```json
{
  "text": "The lantern glows beside us.",
  "generate_srt": true,
  "srt_words_per_line": 3,
  "srt_size": "base"
}
```

The pipeline installs/spawns Whisper if needed and waits for an available worker. If loading or transcription fails, audio can still complete with `srt.error` in the response and a warning in the log. Sync success includes `srt: {srt_path, line_count, word_count, language}`. With `deliver_to`, copies include `srt` and `timing` paths.

The Testing tab does **not** expose `generate_srt`; use HTTP, or generate first then call `/srt`.

## Whisper as verification (not captions)

Testing tab panel **Whisper Verification**, or API:

- `verify_whisper`: true
- `whisper_model`: tiny \| base \| small \| medium \| large
- `tolerance`: default 80.0 (percent)

The pipeline transcribes each chunk and compares to source text. Combined with `auto_retry`, failed similarity can re-run a chunk. `GET /api/jobs/{id}/verification` returns the per-chunk report, summary counts, and average similarity.

This needs Whisper installed and loaded (auto-spawn on generate if verification or SRT is on).

## Whisper sizes

| Size | Params | VRAM | Speed | Note |
| --- | --- | --- | --- | --- |
| tiny | 39M | ~1GB | ~10x | Fastest, least accurate |
| base | 74M | ~1GB | ~7x | Good balance (default) |
| small | 244M | ~2GB | ~4x | Better accuracy |
| medium | 769M | ~5GB | ~2x | Near-best accuracy |
| large | 1550M | ~10GB | ~1x | Best accuracy, slowest |

`WHISPER_MODEL_SIZE` default in config is `base`. `WHISPER_ENABLED` is true. `WHISPER_DEFAULT_TOLERANCE` is 80.0.

## Other Whisper entry points

| Surface | Call |
| --- | --- |
| Voices tab **Transcribe** | `POST /api/voices/{filename}/transcribe` → `<filename>.txt` sidecar |
| CLI | `tts.cmd voices transcribe me.wav --size base` |
| Generic | `POST /api/transcribe` with a source descriptor |
| Upload | `POST /api/transcribe/upload` multipart `file` ≤ 50MB |
| Load/unload | `POST /api/whisper/{size}/load` and `/unload`; `GET /api/whisper` |

`task`: `transcribe` (default) or `translate` (to English). `language`: ISO 639-1. `word_timestamps` default false on the transcribe endpoints; SRT generation always needs word times internally.

## Install Whisper

Setup tab card **Whisper**, or `tts.cmd install whisper --wait`. Pulls `base.pt` (~145MB). Other sizes download when loaded.

## Loading, unloading, and verification results

`whisper load <size>` waits until that size has loaded. `whisper unload <size>` removes only that size from each worker; other cached sizes stay available. CPU workers load sizes on CPU. Transcriptions reserve a worker until the full request finishes.

Unavailable Whisper verification fails explicitly. A chunk synthesized without verification has `verification_passed: null`. Speaker control markers are excluded from verification and source captions, and inferred caption spans are bounded by the audio duration.

## Related pages

- [engines-catalog.md](engines-catalog.md) (whisper section)
- [Jobs and projects](jobs-and-projects.md)
- [Voices tab](gui-voices.md)
- [Testing tab](gui-testing.md)
