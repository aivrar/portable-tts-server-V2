# Audio editor usage

The audio editor applies an ordered list of edits to a source WAV (or convertible file) and writes a **new** file. Existing free-file destinations require `overwrite: true` (HTTP 409 otherwise). Tracked job chunks and finals cannot be overwritten through rendering; save a new edit.

Three equivalent surfaces:

1. GUI **Editor** tab (library + waveform + effects rack + **Render**)
2. CLI `tts.cmd audio ...`
3. HTTP `/api/audio/*`

List the canonical effect manifest (names + defaults) at any time:

```cmd
tts.cmd audio effects
```

```http
GET /api/audio/effects
```

## Sources

| Kind | GUI | CLI spec | JSON |
| --- | --- | --- | --- |
| Job final mix | Library **FINAL** | `final:<job_id>` | `{"kind":"final","job_id":"..."}` |
| One chunk | **CHK N** | `chunk:<job_id>:<N>` | `{"kind":"chunk","job_id":"...","index":0}` |
| Previous edit | **EDIT** | `edit:<job_id>:<name>` | `{"kind":"edit","job_id":"...","name":"podcast_master.mp3"}` |
| Free file | (not in the job library) | `path:E:\tts_server\voices\me.wav` | `{"kind":"path","path":"/mnt/e/tts_server/voices/me.wav"}` |

`path` must sit under `VOICE_DIR`, `OUTPUT_DIR`, or `PROJECTS_OUTPUT`. Windows paths on the CLI are translated to `/mnt/<drive>/`. A `path` source **requires** `output_path` (also under those roots).

## Range operations

These use `start_sec` / `end_sec` on the current audio at that point in the ordered chain. The GUI maps waveform selections through earlier duration changes before submission. On the GUI they are the transport buttons, enabled only when a waveform selection exists.

| Type | Label | Behavior |
| --- | --- | --- |
| `cut` | Cut | Remove the selected range |
| `trim` | Trim to Selection | Keep only the selected range |
| `silence` | Silence | Mute the selected range |
| `fade_in` | Fade In | Linear ramp 0→1 across the range |
| `fade_out` | Fade Out | Linear ramp 1→0 across the range |

## Full-track effects

| Type | GUI label | Params | Implementation notes |
| --- | --- | --- | --- |
| `gain` | Gain | `db` | Linear amplitude from dB |
| `tempo` | Speed (Tempo) | `factor` (CLI 0.25–4, GUI 0.5–2) | pyrubberband time stretch; explicit error if unavailable |
| `pitch` | Pitch Shift | `semitones` (code clamp -24..24, GUI -12..12) | pyrubberband; explicit error if unavailable |
| `eq_highpass` | High-pass | `cutoff_hz` | scipy butter sosfiltfilt |
| `eq_lowpass` | Low-pass | `cutoff_hz` | same |
| `eq_shelf_low` | Bass Shelf | `cutoff_hz`, `gain_db` | |
| `eq_shelf_high` | Treble Shelf | `cutoff_hz`, `gain_db` | |
| `reverb` | Reverb | `room_size`, `damping`, `wet` | impulse-response |
| `echo` | Echo / Delay | `delay_sec`, `feedback`, `wet` | |
| `compressor` | Compressor | `threshold_db`, `ratio`, `attack_ms`, `release_ms`, `makeup_db` | |
| `noise_gate` | Noise Gate | `threshold_db`, `attack_ms`, `release_ms` | |
| `de_reverb` | De-reverb | `strength` 0–1 | Also a TTS post-process slider; keep 0 on clean speech |
| `de_ess` | De-esser | `strength` 0–1 | |
| `normalize_lufs` | LUFS Normalize | `target_lufs` | pyloudnorm; explicit error if unavailable |
| `normalize_peak` | Peak Normalize | `target` | |
| `pad` | (API/CLI; not in the GUI +Add menu) | `front_sec`, `end_sec` (aliases front_pad_sec / end_pad_sec), 0–60s | silence pad |

GUI slider ranges are listed on the [Editor tab](gui-editor.md) page. The Python pipeline clamps some values more widely (tempo 0.25–4). Missing effect libraries, invalid edits, and chains that remove all audio return a clear error. Empty edit chains preserve stereo channels.

## Render outputs

Job-bound sources default to auto-versioned files under `<job_dir>/edits/` when neither `output_name` nor `output_path` is set. `output_name` is a stem without extension; `output_format` is wav \| flac \| ogg \| mp3 (GUI also offers those four; wav/flac/ogg use soundfile subtypes PCM_16 / VORBIS). `output_name` and `output_path` are mutually exclusive. Format is ignored when `output_path` is given.

```cmd
tts.cmd audio render final:00052949-03bb-4630-8501-718b915d7513 --effect gain:db=3 --effect normalize_lufs:target_lufs=-16 --output-name podcast_master --format mp3

tts.cmd audio render path:E:\tts_server\voices\me.wav --effect pitch:semitones=-2 --output-path E:\tts_server\projects_output\me_lower.wav
```

```json
POST /api/audio/render
{
  "source": {"kind": "final", "job_id": "..."},
  "edits": [
    {"type": "eq_highpass", "params": {"cutoff_hz": 120}},
    {"type": "compressor", "params": {"threshold_db": -18, "ratio": 4}},
    {"type": "normalize_lufs", "params": {"target_lufs": -16}}
  ],
  "output_name": "podcast_master",
  "output_format": "wav",
  "overwrite": false
}
```

`--effect TYPE[:k=v,k=v]` is repeatable. `--chain-file` concatenates a JSON list of `{type, params}` with any `--effect` flags. `--timeout` default 600 seconds.

## Manage edits

```cmd
tts.cmd audio edits <job_id>
tts.cmd audio edit-get <job_id> podcast_master.mp3 --out C:\out\final.mp3
tts.cmd audio edit-delete <job_id> podcast_master.mp3
tts.cmd audio peaks final:<job_id> --buckets 1024 --json
```

Peaks return min/max (and RMS) buckets for canvas drawing. GUI default buckets 1024; CLI peaks default 512 (64–4096).

## Editor tab recap

Open **Editor**, Refresh, expand a job, click FINAL, drag a selection, **Fade Out**, **+ Add Effect** → Compressor / LUFS Normalize, pick **mp3**, **Render**. Play the new EDIT. **Save As...** if you need a specific stem. **Download** to copy bytes to Windows without browsing the job folder.

## Related pages

- [Editor tab](gui-editor.md)
- [Windows CLI (`tts.cmd`)](windows-cli.md)
- [Authenticated HTTP API](http-api.md)
- [Jobs and projects](jobs-and-projects.md)
