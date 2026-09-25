# Compact API workflow

Read `output/run/registry/tts_server.json` for `endpoints.api`,
`auth.header`, and `auth.token`. Send the token in the named header.
Alternatively, call the bundled helper from the app root:

```powershell
python skills/use-tts-server/scripts/tts_api.py GET /api/devices
python skills/use-tts-server/scripts/tts_api.py POST /api/tts/kokoro/dryrun --data '@request.json'
```

## Discover

```text
GET /api/live
GET /api/setup/status
GET /api/devices
GET /api/schema/tts/{model}
GET /api/tts/{model}/capabilities
GET /api/tts/{model}/voices
```

The schema includes engine-specific `usage`, `example`, tuned defaults, field
definitions, and parameter ranges. The compact capabilities endpoint includes
the same usage hint plus voices; Edge also returns live voice metadata.

## Preview and generate

```json
POST /api/tts/kokoro/dryrun
{
  "text": "Hello from the API.",
  "voice": "af_heart",
  "output_format": "wav"
}
```

Use the same body at `POST /api/tts/kokoro/submit`, poll
`GET /api/jobs/{job_id}`, then fetch `GET /api/jobs/{job_id}/output`.
Omitted inference fields resolve to the selected engine's tuned defaults; read
`effective_params` from dry-run when reproducibility matters.

## Timed subtitles

```json
POST /api/jobs/{job_id}/srt
{"size":"base","words_per_line":4,"source_guided":true}
```

Download the SRT from `GET /api/jobs/{job_id}/srt` and exact word timing from
`GET /api/jobs/{job_id}/srt/timing`. Source-guided mode is the default for TTS
jobs: Whisper supplies acoustic timing while the known script supplies exact
caption text. The timing JSON also retains raw ASR words and text for auditing.
Set `source_guided` to `false` when captions should reflect raw ASR instead.

## Cleanup

```text
POST /api/models/{model}/unload
POST /api/models/whisper/unload
POST /api/maintenance/clear-cache  {"kinds":["tmp","home","xdg","xdg-config","xdg-data","xdg-state"]}
GET  /api/workers
```
