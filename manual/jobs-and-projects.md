# Jobs and projects

A **job** is one pipeline run: original text, chunk list, per-chunk WAVs, assembled final audio, parameters (for recovery), and optional SRT/edits. A **project** is a named folder under `E:\tts_server\projects_output` that keeps jobs until you delete it. Unnamed jobs live under `E:\tts_server\output\jobs\<uuid>\` and are eligible for 72-hour garbage collection.

## Job directory

```text
{jobs_dir}/{job_id}/
    job.json
    chunk_000.wav
    chunk_001.wav
    {stem}_final.{wav|mp3|ogg|flac|m4a}
    {stem}.srt                 (after SRT)
    {stem}_timing.json
    edits/                     (Editor tab renders)
```

`job.json` is written atomically. Startup indexes `output/jobs` and `projects_output` and can recover from the first incomplete or failed chunk.

Statuses you will see: `running`, `completed`, `failed`, `cancelled`, plus GUI `partial` when some chunks exist.

## How a job is created

- Testing tab **Generate** → sync `POST /api/tts/{model}` → job on disk + optional inline audio.
- `tts.cmd tts ...` sync or `--async` (`POST /api/tts/{model}/submit`).
- `tts.cmd batch batch.json`.
- HTTP with `save_path` (folder under `projects_output` / `output`) or `deliver_to` (copy finals out, purge work dir).

`save_path` and `deliver_to` are mutually exclusive. `save_path` of `demo/hello` creates `E:\tts_server\projects_output\demo\` (or a jobs subfolder named from that path) containing the job folder. `deliver_to` is an absolute path; the response includes `delivered_to: {audio, srt?, timing?}` and the work directory is removed on success. Delivered manifests can appear under `output\delivered_jobs\`.

Dry run (`POST /api/tts/{model}/dryrun` / `tts.cmd dryrun`) shows the chunk plan and resolved path **without** creating a job.

## Inspect

```cmd
tts.cmd jobs list --status completed --limit 50
tts.cmd jobs get <job_id>
tts.cmd jobs chunks <job_id>
tts.cmd jobs manifest <job_id>
tts.cmd jobs output <job_id> --out final.wav
tts.cmd jobs chunk-audio <job_id> 0 --out chunk0.wav
tts.cmd jobs stream <job_id>
tts.cmd jobs zip <job_id> --out job.zip
```

HTTP list filters: `model`, `status` (`running` | `completed` | `failed` | `cancelled`), `since` / `until` ISO-8601, `limit` 1–500 (default 50), `offset`.

`GET /api/jobs/{id}/output/info` returns duration/format plus `artifact.ref` = `tts://jobs/{job_id}/output`. Peer apps should store that ref.

The Editor tab library is this same job list: expand a row to load FINAL / CHK / EDIT.

## Recover instead of restarting

If chunk 7 failed on a 20-chunk job, do **not** resubmit the whole text.

1. `tts.cmd jobs manifest <id>` and `GET /api/jobs/{id}/verification` if Whisper was on.
2. Fix text: `tts.cmd jobs edit-chunk <id> 7 --text "corrected sentence."` (`PUT`, 1–50000 chars). This resets chunk 7 **and every later chunk**.
3. Re-render: `tts.cmd jobs rerun-chunk <id> 7`.
4. Or resume: `tts.cmd jobs recover <id>` from the first incomplete chunk.
5. Cancel: `tts.cmd jobs cancel <id>` (`POST /api/tts/{model}/cancel`; omit job_id on the API to cancel all running jobs for that model).

## Delete and retention

```cmd
tts.cmd jobs delete <id> <id2>
tts.cmd maintenance gc-jobs --older-than 72
```

`POST /api/jobs/delete` **refuses running jobs**. `gc-jobs` `older_than_hours` is 1–8760, default 72 (`MAX_JOB_AGE_HOURS`). Named projects are **not** swept by this retention; delete them explicitly.

## Projects

```cmd
tts.cmd projects
tts.cmd project get myproject
tts.cmd project zip myproject --out myproject.zip
tts.cmd project delete myproject --yes
```

`GET /api/projects` lists subdirectories of `projects_output` with job counts and mtime. `GET /api/projects/{name}` lists jobs and files inside one folder. **DELETE** removes the directory and purges every job under it (irreversible). ZIP is the handoff bundle.

Use `--save-path myproject/scene_01` on generate so the job is born inside the project instead of copying later.

## Limits

- Request text up to `TTS_SERVER_MAX_TEXT_CHARS` (default 100000).
- Chunks up to `TTS_SERVER_MAX_CHUNKS` (default 500).
- Per-engine chunk character caps still apply (bark 200, edge 3000, …); the pipeline splits for you.
- `MAX_INFERENCE_WORKERS` 16 pipeline threads (I/O-bound on worker HTTP).
- `MAX_RETRIES` 3 at the job-manager layer; request `auto_retry` 0–10.

## Output, delivery, and recovery

- Reference audio is copied into each new job so recovery does not depend on an uploaded temporary file or a later voice replacement. Already missing references in older jobs cannot be reconstructed.
- Editing or rerunning a chunk requires an inactive job. It updates the source text and invalidates the previous final audio and captions until recovery completes.
- A delivery extension chooses the actual encoder. Encoding and delivery failures mark the job failed and retain its work files.
- Delivered jobs remain in listings and the Editor. Their edits live under `output/delivered_jobs/<job_id>/edits`. Deleting the library entry removes its manifest and edits; externally delivered audio and captions remain at their destination.
- Garbage collection skips running jobs. Project deletion is refused while any contained job is active.
- The API paginates the full library, including delivered jobs; the Editor loads all pages.

## Related pages

- [Storage, layout, and ports](storage-layout-and-ports.md)
- [Editor tab](gui-editor.md)
- [SRT and Whisper](srt-and-whisper.md)
- [Authenticated HTTP API](http-api.md)
