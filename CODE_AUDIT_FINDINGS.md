# TTS Server code audit findings and repairs

## Repair status - 2026-09-25

The original five audit passes identified 130 bugs/gaps plus four coverage and reproducibility gaps. Code changes addressing all 130 are implemented. Nine additional issues found or exposed during repair are recorded with their fixes below. The resolution ledger maps each original finding to the change and affected files.

The later [live wiki capture pass](#live-wiki-capture-pass---2026-09-25) found and fixed two more UI issues, F140–F141, and corrected manual instructions. Its actual Kokoro/browser verification is recorded separately from the original repair pass below.

**Verification boundary:** automated tests exercise real gateway/job/audio code with simulated model inference, API requests through an in-process ASGI client, concurrency, CLI handling, and browser logic in Node. They do not establish successful inference for every installed engine. No real model inference, GPU workload, full clean installation, native launcher session, or screenshot capture was performed during this repair pass. The existing child distro was used for Python/audio tests; its model installations were not changed.

The following original findings are retained as historical evidence. Their inline line numbers refer to the pre-repair tree, not the current files. Stable IDs F001-F130 now connect them to the ledger. References already lost from older jobs cannot be reconstructed by these changes.

## First pass findings

### Critical

1. **[F001] The WSL bridge can disclose the master API token to clients that can reach it.** In WSL, the bridge binds to `0.0.0.0` by default (`bridge.py:104-110`) and proxies the root page to the gateway over localhost (`bridge.py:279-331`). The gateway sees the bridge as a loopback client and embeds the token in the HTML (`server/tts_api_server.py:6379-6408`). A nonlocal caller reaching the bridge can therefore obtain the token and use the API. Network reachability depends on the host and WSL configuration.

### High

2. **[F002] Uploaded or inline reference audio is unavailable for job recovery and chunk reruns.** The job manifest saves `params`, including the temporary reference path (`server/tts_api_server.py:2336-2340`). The temporary file is deleted after the initial run (`server/tts_api_server.py:2185-2189`, `2429-2432`, `2381-2388`), while recovery reuses the saved parameters (`server/tts_api_server.py:3097-3127`). A recovered voice cloning job can fail or use the wrong voice.

3. **[F003] A busy worker is treated as though no worker exists.** Selection considers only workers with status `ready` (`server/worker_registry.py:109-138`). A concurrent request can try to spawn another model instance (`server/tts_api_server.py:2229-2280`, `2807-2826`), potentially failing on GPU capacity while the existing worker is merely occupied. The inference path also gives up immediately when no ready worker is found (`server/tts_api_server.py:3237-3240`, `3318-3321`). There is no wait or queue for the occupied worker.

4. **[F004] `deliver_to` may write audio bytes under the wrong extension.** `_resolve_deliver_to` returns the destination extension as a format (`server/tts_api_server.py:2653-2681`), but its callers discard that value (`2315-2318`, `3010-3013`). A `.mp3` destination with the default WAV request, for example, receives WAV bytes named `.mp3`.

5. **[F005] Delivery failure is reported as a completed job.** An external copy failure is captured only in `delivered_to.error` (`server/tts_api_server.py:2999-3019`). The job is then marked completed and the response says `status: completed` (`3021-3058`). A caller that checks job status without inspecting the nested delivery result will miss the failed requested delivery.

### Medium

6. **[F006] Missing chunks can leave a job marked running indefinitely.** If assembly finds absent chunk files, the pipeline returns `status: incomplete` (`server/tts_api_server.py:2915-2928`) without updating the persisted job, which begins with `status: running` (`server/job_manager.py:75-85`). The synchronous handler returns HTTP 200 because the result has no `error` key (`server/tts_api_server.py:2416-2428`); asynchronous clients can keep polling a running job.

7. **[F007] The Testing tab cannot play or save a successful large output.** The gateway omits inline audio beyond its size limit and supplies `output_url` (`server/tts_api_server.py:3060-3072`). The tab handles only `audio_base64` or `audio` (`server/static/tab-testing.js:746-761`) and shows Play and Save actions only when it has an audio URL (`834-845`).

8. **[F008] Delivered jobs disappear from job listings and the Editor library.** Successful delivery archives a manifest and purges the job directory (`server/tts_api_server.py:3033-3040`), but `GET /api/jobs` reads only `JobManager.list_jobs` (`1241-1264`), whose index scans job directories and projects rather than `output/delivered_jobs` (`server/job_manager.py:430-457`). The job remains addressable by ID. The synchronous response also retains `saved_to` pointing to the purged work file (`server/tts_api_server.py:3046-3058`).

9. **[F009] Some early request failures leak temporary reference files.** Multipart upload writes the file before constructing the validated request object and entering `try/finally` (`server/tts_api_server.py:2122-2137`, `2184-2189`). Inline base64 is decoded to a file before worker, chunk, and output validation; its cleanup starts only when the pipeline is submitted (`2216-2218`, `2299-2318`, `2403-2432`).

10. **[F010] Cancellation after chunk synthesis can still result in completion.** Cancellation is checked in chunk processing (`server/tts_api_server.py:2445-2450`, `2537-2542`), but no check follows during assembly, SRT generation, delivery, and finalization (`2915-3028`). A request received in those stages can be acknowledged yet finish as completed.

## Second pass findings

### High

11. **[F011] Whisper verification can report a false pass when Whisper is unavailable.** After a requested verification cannot use a worker, the fallback returns `(True, 1.0, "(whisper unavailable)")` if local Whisper cannot be imported (`server/tts_api_server.py:3324-3386`). The chunk then records verification as passed (`2470-2481`). This reports unverified speech as a perfect match.

12. **[F012] Project deletion can remove a running job's files.** The project delete route recursively removes the whole project and later calls `delete_job(..., force=True)` (`server/tts_api_server.py:5942-5969`). It neither checks nor cancels running jobs. An active pipeline using that project can lose its chunks and manifest mid-run. The recursive delete ignores filesystem errors yet the endpoint still reports `deleted: true`.

13. **[F013] Job garbage collection can delete active jobs.** `cleanup_old_jobs` deletes `temp_` directories solely by age, without checking the job status or active-job registry (`server/job_manager.py:720-738`). The maintenance endpoint exposes a minimum threshold of one hour (`server/tts_api_server.py:6099-6107`), so a long job can have its work directory removed while it runs.

### Medium

14. **[F014] Reuploading a saved voice silently overwrites the audio but keeps the old transcript.** Upload writes directly to an existing destination with no conflict check or sidecar invalidation (`server/tts_api_server.py:4453-4474`). Voice listings then read the previous `.txt` sidecar as the new clip's transcription (`4432-4450`).

15. **[F015] Renaming a voice can change its extension without converting its audio.** The rename endpoint accepts any supported new audio suffix and simply renames the file (`server/tts_api_server.py:6022-6043`). For example, a WAV renamed to `.mp3` retains WAV bytes and is subsequently served as MPEG audio (`4488-4494`).

16. **[F016] Concurrent Whisper requests can mark a worker ready while it remains busy.** `_ensure_whisper_worker_ready` returns workers already marked `busy` (`server/tts_api_server.py:5680-5684`). Each transcription marks the same worker busy and independently marks it ready in `finally` (`4534-4568`, `4571-4623`, `4626-4661`). When the first concurrent request finishes, the registry advertises the worker as ready while the second is still transcribing; further work can be dispatched to it.

## Further second pass findings

### High

- **[F017] Concurrent cold starts can fail or hang across event loops.** WorkerManager shares asyncio.Lock instances per device (server/worker_manager.py:35, 39-46), while asynchronous submissions call spawn_worker through asyncio.run in executor threads (server/tts_api_server.py:2807-2826). Contention uses one asyncio lock from different loops and threads. An isolated standard-library probe reproduced a cross-thread RuntimeError and a stuck waiter; this has not been reproduced in the running app.
- **[F018] A model install can delete another operation's temporary files.** After any installation, install_model.sh:1146-1151 recursively clears the shared TMPDIR. Workers place scratch audio there (server/tts_worker.py:964-975, 1710-1731), and job/project ZIP exports are built there (server/tts_api_server.py:5362-5371, 5405-5413, 5972-5986). The gateway's own installer cleanup checks for active workers first (server/tts_api_server.py:4145-4161), but the shell script does not.
- **[F019] Testing history revokes audio URLs it still needs.** App.playAudio revokes the previous blob URL (server/static/app.js:298-305), while Testing retains that URL for Play and Save (server/static/tab-testing.js:763-766, 834-864). Clicking Play twice on the same result is sufficient to invalidate that result's playback and download.
- **[F020] A hidden saved voice can override another model's native voice.** Switching away from a cloning model hides the saved-reference control without clearing its selection (server/static/tab-testing.js:470-480). Generation always copies the hidden selection into the voice parameter (638-652), so switching to Kokoro can send a saved filename where Kokoro expects a native voice ID (server/tts_worker.py:1043-1062).
- **[F021] Install status can say a model is ready with incomplete assets.** The check accepts any nonempty weight directory without required file checks for models lacking a required_weight_files list; it also accepts any nonempty override directory as packages installed (server/tts_api_server.py:3770-3785, 3828-3859; server/config.py:479-501). A partial download or incomplete override can be shown as ready and skipped by install-all, then fail at load time.

### Medium

- **[F022] Whisper size management endpoints do not perform the requested size operation.** The load endpoint only ensures some Whisper worker exists and then claims the named size is loaded (server/tts_api_server.py:1210-1226), although the worker starts with an empty model-size cache (server/tts_worker.py:710-713). The unload endpoint ignores size and kills all Whisper workers (server/tts_api_server.py:1229-1235). The info endpoint's loaded list contains worker IDs rather than model sizes (1184-1206).
- **[F023] Plain device=cuda bypasses the GPU reserve check.** Device input is unconstrained (server/tts_api_server.py:706-721). Capacity checking and CUDA_VISIBLE_DEVICES remapping only run for a cuda: prefix (server/worker_manager.py:48-50, 107-113), but the worker resolves plain cuda onto cuda:0 (server/tts_worker.py:138-162).
- **[F024] The CLI can finish install-all waiting while installation continues.** The CLI decides completion solely from per-model installing statuses (tts.py:520-528), while the gateway tracks active_installs.all separately and has gaps between attempts (server/tts_api_server.py:3905-3913, 4213-4224). It also exits successfully when individual model installs have failed.
- **[F025] Fast start skips repair of a damaged base environment.** setup.sh:155-162 trusts a completion stamp, executable Python, and server file, exiting before the package check and repair path at setup.sh:229-245. Missing packages can make every subsequent normal launch fail until setup is forced.
- **[F026] The jobs-wait CLI ignores cancellation.** It treats only completed and failed as terminal (tts.py:979-1005), while the job manager persists cancelled (server/job_manager.py:276-287). A cancelled job waits until the timeout rather than returning a cancellation result.
- **[F027] The jobs-stream CLI can exit successfully on an unfinished job.** The gateway emits stream_timeout and closes when its stream deadline is reached (server/tts_api_server.py:5417-5440); the CLI does not treat that event or EOF as failure and falls through with exit code zero (tts.py:1368-1404).
- **[F028] Batch submission has a short async timeout and can exit successfully after failures.** A single async submission allows 1800 seconds for cold install (tts.py:819-824), while batch async requests allow only 60 seconds (tts.py:1133-1139). Per-job API failures are captured in results but the command returns normally, including when every submission failed (1140-1150).
- **[F029] Qwen's pinned torchvision can conflict with shared Torch.** Minimal Qwen installation pins torchvision 0.20.1 with no dependency resolution and assumes shared Torch 2.5.1 (server/install_model.sh:220-246, 887-894). Base setup permits any Torch from 2.2 up to but excluding 2.8 (server/setup.sh:263-272). A different resolved Torch version can leave an incompatible pair.
- **[F030] Editor range selections drift after earlier range edits.** The UI records later selections against the original waveform and sends the chain in order (server/static/tab-jobs.js:1105-1123, 1312-1318), while the backend applies each operation to the already modified array (server/audio_editor.py:82-95, 483-495). A Cut at 1-2 seconds followed by a Cut at 4-5 seconds targets the wrong source segment for the second cut.
- **[F031] Testing does not refresh saved voices after upload or deletion.** The Voices tab refreshes only its own list (server/static/tab-voices.js:211-245), while Testing loads saved voices only on initialization or model change (server/static/tab-testing.js:128-134, 470-481). Newly uploaded voices remain unavailable and deleted selections remain stale until another refresh trigger.
- **[F032] A transient initial config failure leaves model controls empty.** App.init fetches configuration once (server/static/app.js:329-340); subsequent polling retries workers, jobs, and setup but not configuration (358-367). If startup races the gateway, the page can later say Connected while Setup cards and the Server model selector remain empty (server/static/tab-server.js:133-140).
- **[F033] Editor source switching can leave playback bound to the previous track.** The tab changes its active source and label before awaiting new peaks but retains its previous audio object (server/static/tab-jobs.js:587-637). Playback during loading, or after a failed load, can use that previous track. An older request's unconditional finally can also hide the newer loading indicator during rapid switches.
- **[F034] The F5 requirement is omitted from client validation.** The Testing tab requires a reference only for VibeVoice (server/static/tab-testing.js:98-99), while the gateway requires one for F5 too (server/tts_api_server.py:2015-2035). Submitting F5 without one yields a preventable error; the tab converts the structured detail object to the text [object Object] (server/static/tab-testing.js:739-743).
- **[F035] Chunk editing leaves the job's source text stale.** The edit endpoint changes only the selected chunk text (server/tts_api_server.py:1312-1335), while the job's input_text and saved parameters retain the original text (server/job_manager.py:75-115). Recovery synthesizes the edited chunks (server/tts_api_server.py:3097-3126), but both pipeline SRT generation and later job SRT generation use the original text (2987-2992, 5732-5734). The job preview and source-guided captions can disagree with the new audio.
- **[F036] Chunk editing and rerun can mutate a job while it is still generating.** The edit endpoint does not reject an active job before rewriting chunk metadata (server/tts_api_server.py:1302-1335), while the pipeline already holds its own chunks list (2829-2836). The rerun endpoint also resets chunks before calling recover_job, whose in-flight guard can then return 409 (5463-5501, 1355-1364). A rejected rerun can still disturb the active job's manifest.
- **[F037] Audio renders can claim success after skipping requested edits.** Unknown edit names and operation exceptions are logged then ignored (server/audio_editor.py:483-501); tempo, pitch, and LUFS normalization also explicitly return unchanged audio when dependencies are unavailable (159-180, 424-430). The render API still returns status ok (server/tts_api_server.py:1844-1865), without telling the caller which edits were skipped.
- **[F038] Concurrent renders to one named destination share a temporary filename.** The output_name and output_path branches can target a common destination (server/tts_api_server.py:1781-1795, 1835-1847). render_to_file always writes its sibling .tmp.<destination> file before replacing the destination (server/audio_editor.py:526-547), so overlapping renders can interfere or expose a partial result despite the atomic-rename intent.

### Lower impact

- **[F039] Temporary reference cleanup misses non-WAV formats.** Startup and maintenance cleanup match only ref_*.wav in OUTPUT_DIR (server/tts_api_server.py:279-286, 6210-6220), while uploads and inline references can create ref_*.mp3, .flac, .ogg, or .m4a (2124-2135, 3501-3508). A leaked non-WAV reference from an early failure is not swept by these paths.
- **[F040] VibeVoice can leak an earlier trimmed reference when a later reference fails.** Trimming up to four references happens before the cleanup try/finally (server/tts_worker.py:1407-1431, 1462-1467).
- **[F041] A directly unloaded Whisper process can retain stale cache timestamps.** Model unload clears its model cache but not the access-time map (server/tts_worker.py:774-796). Reusing that process and loading more sizes can select an absent entry for LRU eviction (2152-2176). The public gateway normally kills the process instead.
- **[F042] Windows local peer listing tests WSL PIDs in the wrong namespace.** The registry reader accounts for WSL entries (tts.py:98-108), but peers --local applies Windows PID liveness checks to them (tts.py:565-581), potentially omitting a live peer.
- **[F043] Editor library exposes only the 50 newest jobs.** App.pollJobs calls the API without paging (server/static/app.js:249-254), the API defaults to 50 (server/tts_api_server.py:1241-1260), and the Editor has no offset control (server/static/tab-jobs.js:169-184, 294-316).
- **[F044] VibeVoice's multi-reference feature is absent from Testing.** The API accepts up to four ordered references (server/tts_api_server.py:615-616), but the tab offers one saved reference or upload and sends only a single voice (server/static/tab-testing.js:154-180, 638-652).
- **[F045] App audio and Editor audio can overlap.** App.playAudio stops only its own audio element (server/static/app.js:298-305); the Editor owns a separate audio element (server/static/tab-jobs.js:685-735, 1006-1015).
- **[F046] Install --wait --json prints non-JSON text after its JSON response.** It prints the initial JSON payload and then prints done after polling (tts.py:510-534), so the combined stdout is not one parseable JSON document.

## Third pass findings

### High

- **[F047] Concurrent recovery requests can run the same job twice.** `recover_job` checks `_running_jobs` under a lock, releases it to read and rewrite the manifest, then reacquires the lock to register the job (`server/tts_api_server.py:1355-1373`). Two requests can both pass the check and submit pipelines that write the same chunk files and `job.json`. The in-flight guard is therefore not atomic.
- **[F048] Kokoro uses the American-English pipeline for every voice.** The worker constructs `KPipeline(lang_code="a")` once (`server/tts_worker.py:309-311`). Inference derives a language code from the selected voice but only tries to pass it as a call argument (`1043-1062`); [Kokoro's pipeline API](https://github.com/hexgrad/kokoro/blob/main/kokoro/pipeline.py) selects language at construction and its call has no language argument. Non-English voices therefore use English text-to-phoneme processing.
- **[F049] Kokoro and XTTS installs stage weights where their loaders do not look.** Kokoro's installer downloads a local snapshot under `MODELS_DIR/kokoro` (`server/install_model.sh:759-763`), but `KPipeline` loads its model and voices through the Hugging Face cache (`server/tts_worker.py:309-311`; [upstream pipeline](https://github.com/hexgrad/kokoro/blob/main/kokoro/pipeline.py)). XTTS downloads to `MODELS_DIR/xtts-v2` (`server/install_model.sh:870-875`), while the worker invokes Coqui by model name (`server/tts_worker.py:178-180`), which uses its separate managed model path ([Coqui model manager](https://github.com/idiap/coqui-ai-TTS/blob/dev/TTS/utils/manage.py)). The installer passes `local_dir`, which [does not populate the Hugging Face cache](https://huggingface.co/docs/huggingface_hub/package_reference/file_download). These loads can download the weights again or fail offline despite an apparently completed install.
- **[F050] Clearing the temporary cache can delete files in active operations.** The maintenance endpoint wipes `CACHE_DIR/tmp` without checking jobs, workers, or downloads (`server/tts_api_server.py:6114-6169`). That is the default `TMP_DIR` (`server/config.py:85`), used for worker scratch audio (`server/tts_worker.py:964-975`) and job/project ZIP exports (`server/tts_api_server.py:5362-5383, 5405-5413, 5972-5986`). A concurrent clear can break inference or a download.

### Medium

- **[F051] Recovery loses the default retry count when synthesis fails.** New requests save `auto_retry: null` in `parameters` unless the caller sets it (`server/tts_api_server.py:623-633, 689-699, 2336-2340`). The initial run replaces that null with `MAX_RETRIES` (`2214`), but recovery uses `params.get("auto_retry", MAX_RETRIES)`, which returns null (`3103-3122`). On the first chunk error, `retry_count > max_retries` raises a `TypeError` instead of retrying (`2512`).
- **[F052] Rerunning or editing a completed job can expose its old audio and captions.** Chunk reset clears progress metadata but retains the old `final_file` and SRT fields/files (`server/tts_api_server.py:1302-1335, 5479-5501`; `server/job_manager.py:314-348`). Output and SRT download routes check file existence rather than job status (`server/tts_api_server.py:1398-1418, 5829-5849`). During a rerun, and after one fails, clients can receive the previous audio and subtitles as though they describe the current chunks.
- **[F053] The verification report marks unverified chunks as passed.** Successful synthesis without Whisper calls `update_chunk` without verification data (`server/tts_api_server.py:2501-2504`), but `JobManager.update_chunk` defaults `verification_passed` to true (`server/job_manager.py:185-188`). Ordinary synthesis errors default it to false (`199-204`). The report counts both as verification results (`server/tts_api_server.py:1481-1526`), despite saying unverified fields are null.
- **[F054] The Testing tab does not honor its selected worker.** It presents a worker ID selector and reads the selection (`server/static/tab-testing.js:420-450, 630-633`), but sends only its device (`704`). The gateway's inference selection and registry round robin choose any ready worker for that model, without worker ID or device filtering (`server/tts_api_server.py:3237-3240`; `server/worker_registry.py:115-138`). With multiple workers, generation can run on a different worker or GPU.
- **[F055] A device-specific model load can report success without loading that device.** `POST /api/models/{model}/load?device=cpu` immediately returns `already_loaded` if any ready worker of the model exists (`server/tts_api_server.py:1066-1084`). A ready GPU worker thus prevents the requested CPU worker from being created.
- **[F056] Delivered jobs cannot be streamed after their work directory is purged.** Successful external delivery archives the manifest and deletes the job directory (`server/tts_api_server.py:3033-3040`). `GET /api/jobs/{id}` reads that archive (`1290-1300`), but the SSE stream reads only `job_manager.get_job` (`5424-5448`). A stream opened after delivery gets 404; one already open can emit `gone` instead of `completed`, which the CLI treats as failure (`tts.py:1388-1400`).
- **[F057] Explicit zero values for Chatterbox controls are silently replaced.** The API and UI allow `exaggeration=0` and `cfg_weight=0` (`server/tts_api_server.py:648-649`; `server/config.py:612-613`), and parameter merging preserves them (`server/tts_api_server.py:689-699`). The worker reads each with `value or 0.5`, changing zero to 0.5 (`server/tts_worker.py:1158-1161`).
- **[F058] Voice transcripts collide when files share a stem.** `A.wav` and `A.mp3` both use `A.txt` for their transcript (`server/tts_api_server.py:4438-4448, 4663-4668`). Deleting either audio deletes the other's transcript (`4497-4505`); renaming an audio file can also move its sidecar onto another format's existing sidecar because only the audio destination is checked (`6022-6052`).
- **[F059] A format-conversion fallback leaves contradictory job metadata.** On a conversion `RuntimeError`, the pipeline serves a WAV and returns `format: wav` (`server/tts_api_server.py:2955-2969, 3045-3058`). `complete_job` records the WAV filename but does not update the manifest's requested `output_format` or `expected_files` (`server/job_manager.py:219-254`). Job listings can therefore say MP3 while the only final artifact is WAV.
- **[F060] A busy install response is treated as an install start.** When another model is installing, the per-model endpoint returns HTTP 200 with `status: busy` (`server/tts_api_server.py:4310-4324`). Setup treats every status other than `already_installing` as success and shows “install started” (`server/static/tab-setup.js:132-155`); `tts install --wait` also exits without waiting because it polls only statuses beginning `installing` (`tts.py:509-520`).
- **[F061] The Editor reports a running job deleted when the server refused.** Delete is offered for running jobs (`server/static/tab-jobs.js:461-478`). `JobManager.delete_job` refuses them and the API returns HTTP 200 with `deleted: 0` (`server/job_manager.py:478-494`; `server/tts_api_server.py:1981-1991`), but the Editor ignores the response, clears its local selection, and toasts “Job deleted” (`server/static/tab-jobs.js:529-542`).
- **[F062] ZIP download clients buffer the entire archive in memory.** The gateway writes potentially large ZIPs to disk (`server/tts_api_server.py:5362-5383`), but the CLI request helper reads the entire response before `jobs zip` or `projects zip` writes it (`tts.py:237-250, 1356-1365, 1449-1460`). The bundled skill client does the same (`skills/use-tts-server/scripts/tts_api.py:74-90`). Large projects can cause substantial client memory use or an out-of-memory failure.
- **[F063] A bridge proxy error can persist the master API token in its log.** The UI puts that token in the log EventSource query string (`server/static/app.js:44-54, 178-183`). Any proxy exception logs `self.path` (`bridge.py:386`), and `log` writes it to `RUN_DIR/tts_server_debug.log` (`bridge.py:113, 143-153`). A failed log stream request can therefore leave the token in persistent diagnostics.
- **[F064] An unknown job ID can block the gateway while it scans the project tree.** On a cache miss, `_find_job_file` recursively scans and parses every job manifest under the jobs and project roots (`server/job_manager.py:591-640`). `GET /api/jobs/{job_id}` calls that synchronous search directly in an async route (`server/tts_api_server.py:1290-1295`). On a large project library, a nonexistent ID can stall other requests on the event loop; repeated misses repeat the scan.

### Lower impact

- **[F065] Long `wait-ready` timeouts are cut short at ten minutes.** The CLI sends its remaining user timeout to `/api/ready` only once (`tts.py:375-391`), while that endpoint caps each wait at 600 seconds (`server/tts_api_server.py:752-777`). A requested hour-long wait can return timeout after ten minutes.
- **[F066] Deletion can report success when files remain.** `JobManager.delete_job` uses `shutil.rmtree(..., ignore_errors=True)` and then returns true without checking whether the directory was removed (`server/job_manager.py:478-507`). `/api/jobs/delete` can count a permission or filesystem failure as a completed deletion (`server/tts_api_server.py:1981-1991`); the same pattern inflates maintenance cleanup counts (`server/job_manager.py:720-738`).
- **[F067] The Editor can keep showing an outdated edit list after external changes.** It fetches a job's edits only when its cache has no entry (`server/static/tab-jobs.js:372-389, 441-451`). The Refresh button reloads job summaries, and the render signature omits edit data (`server/static/tab-jobs.js:157-172`). Edits created or deleted through the CLI/API after the first expansion can remain invisible or appear present until the page reloads.

## Fourth pass findings

This pass revisited startup and discovery, installation state, audio and subtitle paths, and job lifecycle. The findings below are source traced; the conditional races and resource limits have not been reproduced against a running service.

### High

- **[F068] A clean bridge startup can publish the direct gateway as the primary API.** During gateway lifespan startup, discovery probes the bridge's `/health` before the gateway serves requests (`server/tts_api_server.py:315-349`). The bridge proxies `/health` to that still-unavailable gateway, while only `/api/status` is local (`bridge.py:203-218, 279-300`). The probe therefore fails on a clean start and `endpoints.api` is set to port 8300 even if the bridge is already listening. The Windows CLI follows that endpoint (`tts.py:136-143`), bypassing bridge shutdown coordination.
- **[F069] Direct gateway shutdown can be reversed by the bridge supervisor.** The bridge stops supervision only after it proxies a successful `/api/shutdown` response (`bridge.py:292, 367-368, 451-460`). A request sent to the published direct gateway shuts down the gateway and its workers (`server/tts_api_server.py:6318-6367`), while a still-running bridge sees the missing health response and starts another gateway (`bridge.py:495-543`). This applies when the launcher has not also stopped the bridge.
- **[F070] A slow cold start can be repeatedly killed before it becomes ready.** The bridge gives a child 60 seconds to answer `/health` and then retries after 10 seconds without ending that child (`bridge.py:691-708, 529-543`). On the next attempt it retires the still-running child after three short probes and launches a replacement (`bridge.py:592-635`). A gateway that consistently needs longer than roughly 70 seconds to initialize can never reach readiness under this supervisor.
- **[F071] Concurrent gateway launches can overwrite each other's authentication state.** The app manifest starts port 8300 directly (`app.json:7-8`), and the bridge can launch the same gateway when its health check has not succeeded (`bridge.py:495-543, 592-603`). Every gateway process creates its own random token and writes the same `api_token` file before binding (`server/tts_api_server.py:103-110`); it also publishes the shared discovery record later (`329-350`). If both startup paths overlap, the losing process can leave a token file or registry entry that does not authenticate to the process serving requests. The CLI uses those values (`tts.py:118-164`).
- **[F072] The reachable bridge buffers unauthenticated uploads in unbounded request threads.** WSL defaults the bridge bind address to `0.0.0.0` (`bridge.py:104-110`), and the HTTP server creates a thread per request (`bridge.py:764-772`). It reads a whole body of up to 100 MB, including chunked bodies, before forwarding it for gateway authentication (`bridge.py:236-277, 279-325`). There is no client read timeout or concurrency cap in this path, so concurrent large or slow requests from a network peer can exhaust memory or threads where that bind is reachable.
- **[F073] Unbounded audio profile overrides can corrupt a successful result or fail late.** The pipeline request exposes plain numeric overrides and copies them into the processing profile without range checks (`server/tts_api_server.py:661-669, 2220-2227`). A negative `clipping` threshold reaches `np.clip(data, -threshold, threshold)`, which collapses samples to a constant (`server/audio_processing.py:189-197`); negative padding or pause values reach `np.zeros` during assembly (`server/audio_assembler.py:93-95`). The former can produce corrupt audio while the job reports completion, and the latter wastes completed inference before failing.
- **[F074] A failed parallel job becomes deletable while sibling chunks may still run.** On the first fatal chunk result, `_run_chunks_parallel` writes a failed manifest and returns from inside a `ThreadPoolExecutor` context (`server/tts_api_server.py:2599-2638`). The context waits for already-running siblings after the manifest says failed. `JobManager.delete_job` refuses only `running` manifests (`server/job_manager.py:478-507`), so a concurrent delete can remove the directory and cancellation marker before those sibling tasks finish writing.
- **[F075] Crash recovery can leave model child processes holding GPU memory.** Workers start as process-group leaders, and normal cleanup kills the whole group because engine children can outlive the worker (`server/worker_manager.py:130-153, 460-485`). After a gateway crash, `kill_orphan_workers` instead finds only `tts_worker.py` processes and sends SIGKILL to each matching PID (`590-627`). Engine children with different command lines can survive and will not match the next orphan scan.
- **[F076] An open WebView cannot recover authentication after a gateway restart.** Each gateway process generates a new random API token by default (`server/tts_api_server.py:103-110`) and injects it into the index page only when that page is served (`6393-6408`). Browser API requests and the log EventSource keep using that one global token (`server/static/app.js:35-53, 176-209`). If bridge supervision restarts the gateway while the page remains open, polling gets 401 and SSE keeps reconnecting with the old token until the page is reloaded. A fixed `TTS_API_TOKEN` avoids this particular trigger.

### Medium

- **[F077] Unloading a worker leaves it registered as ready.** The gateway forwards `/unload` and returns success without changing registry status (`server/tts_api_server.py:902-919`). The worker reports an idle unloaded model afterward (`server/tts_worker.py:2236-2264, 2284-2287`), but health polling updates only memory metrics (`server/worker_manager.py:685-724`). Ready/model-status responses can claim that model is loaded, and the next inference can unexpectedly reload it.
- **[F078] Concurrent worker cleanup can return a newly allocated port to the pool.** Gateway `_finalize_worker` unregisters a failed worker and unconditionally releases its port (`server/tts_api_server.py:3179-3181, 3190-3200`). Manager kill and health cleanup release only if they performed the unregister (`server/worker_manager.py:302-306, 668-677`). If the gateway races either manager path, a second release can re-add a port already allocated to a replacement. The OS bind check is only advisory before the new worker binds (`server/worker_registry.py:47-76`), so another spawn can receive the same port.
- **[F079] Install all omits a weight file its own status check requires.** The aggregate installer always skips Whisper as an on-demand download (`server/tts_api_server.py:4191-4204`), while installation status requires `models/whisper/base.pt` (`server/config.py:497`; `server/tts_api_server.py:3828-3838`). The dedicated Whisper installer downloads that file (`server/install_model.sh:957-967`). A fresh install-all run can therefore end with Whisper still not ready by the server's own definition.
- **[F080] An install cancel can be lost before process creation or between retries.** The endpoint marks a model active and awaits worker teardown before starting its install thread (`server/tts_api_server.py:4316-4332`). Cancel sets a flag but returns `not_running` when no subprocess exists (`4275-4294`); the install thread does not check the flag before `Popen` (`3958-4024`). In install-all, the 30-second retry delay likewise has no subprocess, and the retry checks the aggregate cancel flag but not the per-model one (`4213-4224`). The canceled model can still start or restart.
- **[F081] The setup repair check can approve a broken base environment.** Even on a full setup run, `base_venv_ready` checks only that ten module specs exist, not that they import or that other required packages exist (`server/setup.sh:229-244`). It omits `python-multipart` and Whisper, installed only in the skipped repair branch (`288-321`). For example, removing `python-multipart` while the ten checked modules remain makes setup report the venv ready, although the gateway declares `Form` and `UploadFile` routes (`server/tts_api_server.py:2064-2083`) that require it.
- **[F082] Setup and model installation do not coordinate their environment mutations.** Base setup uses `tts_setup.lock` (`server/setup.sh:74-80`), while model installation uses `tts_install.lock` (`server/install_model.sh:97-102`). A forced or manual setup can therefore run alongside a model install and change the same virtual environment, caches, and model tree concurrently.
- **[F083] Install-all status says completed after model failures.** The aggregate loop logs a model's failed final attempt and continues (`server/tts_api_server.py:4213-4226`), then marks `all` as `completed` whenever aggregate cancellation was not requested (`4227-4233`). This is distinct from the earlier CLI exit-status issue: the API's own aggregate status does not report partial failure.
- **[F084] M4A is accepted in paths that read it directly through SoundFile.** M4A is an allowed voice/reference format (`server/tts_api_server.py:586`; `server/tts_worker.py:855-888`), yet worker reference trimming and Editor peaks/render call `sf.read` directly (`server/tts_worker.py:945-978`; `server/audio_editor.py:515-517, 579-580`). The [libsndfile format list](https://libsndfile.github.io/libsndfile/formats.html) and [SoundFile documentation](https://python-soundfile.readthedocs.io/en/latest/) do not list MP4/M4A decoding. A valid M4A can be accepted and then fail when those features are used; confirm against the bundled library version before a fix.
- **[F085] Source-guided subtitles can include unspoken speaker controls.** `_subtitle_source_text` strips VoxCPM2 and Orpheus controls, but leaves Dia `[S1]`/`[S2]` and VibeVoice `Speaker N:` prefixes (`server/tts_api_server.py:5550-5567`). Both engines use those prefixes as script controls (`server/text_utils.py:189-239`), while pipeline and later SRT generation feed source text into caption alignment (`server/tts_api_server.py:2684-2745, 5710-5779`). Captions can display the controls and allocate spoken-word timing to them.
- **[F086] An empty successful voice transcription leaves its old transcript visible.** The transcribe endpoint overwrites the sidecar only when Whisper returns nonempty text (`server/tts_api_server.py:4626-4668`). If a previously transcribed voice now transcribes to empty text, its old `.txt` remains and the voice listing still returns that stale transcription (`4432-4448`).
- **[F087] Canceling a queued asynchronous job can still cold-start its model.** The background pipeline calls `_ensure_pipeline_worker_ready_sync` before processing chunks (`server/tts_api_server.py:2829-2860`); its first cancellation check is later in chunk processing (`2441-2450`). A job canceled immediately after submission can still spawn and load a model, using startup time and GPU memory before cancellation is noticed.
- **[F088] Speaker-marker-only text creates a job with no chunks.** The Dia and VibeVoice chunkers discard turns without spoken content (`server/text_utils.py:189-239`), so inputs such as `[S1]` or `Speaker 1:` yield an empty chunk list. The pipeline rejects blank raw text and too many chunks, but never rejects zero chunks before creating a job (`server/tts_api_server.py:2206-2209, 2299-2339`). It can start a worker and then fail at assembly instead of returning an immediate validation error.
- **[F089] Local peer JSON output exposes other apps' authentication tokens.** `tts peers --local --json` reads raw discovery records and outputs each whole record (`tts.py:565-583`), including `auth.token`. The gateway's peer endpoint deliberately removes `auth` (`server/tts_api_server.py:783-802`). Sharing this seemingly informational CLI output can therefore disclose peer API credentials.
- **[F090] More accepted zero-valued model controls are silently replaced.** The gateway allows `cfg_scale=0`, `waveform_temperature=0`, and `speaker_idx=0` (`server/tts_api_server.py:645-656`). F5, Dia, and VoxCPM2 read zero `cfg_scale` through `or` defaults; Bark changes zero waveform temperature to 0.7; SpeechT5 changes speaker index zero to 7306 (`server/tts_worker.py:1126-1127, 1228-1229, 1279-1282, 1548-1551, 1965-1969`). This extends the earlier Chatterbox zero-control finding to other engines.
- **[F091] A failed Shutdown request leaves the browser without status or log updates.** The Shutdown button clears the polling timer and closes its EventSource before sending the request (`server/static/app.js:105-118`). On a network failure or rejected request, the catch restores only the button (`119-125`). If the app remains open, workers, jobs, setup status, and logs stop refreshing until page reload.
- **[F092] Editing during an Editor render can discard unsent changes.** `_render` snapshots the edit chain before its API request and disables only Render and Save As (`server/static/tab-jobs.js:1321-1349`). Effect and range controls remain usable while the request is pending. When the response arrives for the same source, the Editor clears the entire current chain (`1351-1356`), including edits added or changed after the snapshot that were never sent.
- **[F093] Editor navigation during render completion can be overwritten.** The render handler checks whether the source changed once, then awaits `_fetchEdits` before loading the render result (`server/static/tab-jobs.js:1351-1364`). A user who selects another source during that await passes the earlier check; the finished render then replaces the newer selection.

### Lower impact

- **[F094] Cutting the full audio duration fails the render request.** A full-range Cut produces an empty array (`server/audio_editor.py:82-87`). `apply_edits` then calls `np.max` on that array outside its per-edit exception handler (`483-501`), so the render endpoint fails instead of returning an empty result or a validation error (`server/tts_api_server.py:1844-1858`).
- **[F095] Waveform peaks can omit almost half of a short clip.** For `n >= buckets`, `extract_peaks` uses `n // buckets` samples per bucket and discards the remainder while still reporting the full duration (`server/audio_editor.py:603-619`). With 2,047 samples and 1,024 buckets, the final 1,023 samples have no peaks; a loud tail can be invisible in the Editor.
- **[F096] A model-specific cancel path can cancel a different model's job.** When a job ID is supplied, `/api/tts/{model}/cancel` calls `request_cancel` without validating the path model or comparing it with the job model (`server/tts_api_server.py:1994-2008`). A typo or wrong model path can cancel an unrelated active job.
- **[F097] The bridge debug log can exceed its configured cap during a long run.** Rotation occurs only before the persistent log handle is opened (`bridge.py:126-153`); that handle stays open until shutdown (`159-168`). A noisy long-running bridge can grow beyond `TTS_LOG_MAX_BYTES` until the next process start.
- **[F098] The Windows wrapper may fail despite a working `python` on PATH.** `tts.cmd` picks `py -3` whenever the `py` launcher exists and returns its error directly (`tts.cmd:8-13`). It never tries the later `python` fallback when the launcher exists but has no registered Python 3 interpreter (`14-27`).
- **[F099] Overlapping browser polls can restore older job or install status.** Unlike worker polling, `pollJobs` and `loadSetupStatus` have no in-flight or response-generation guard (`server/static/app.js:249-255, 285-292`). The timer starts another request every 6 or 12 seconds without waiting (`361-367`). If an older request finishes after a newer one, its stale snapshot overwrites the newer jobs or install state in the page.
- **[F100] Rapid voice playback clicks can play the earlier selection last.** The Voices tab fetches a full authenticated blob before calling `App.playAudio`, without checking whether that click is still current (`server/static/tab-voices.js:147-155`). If voice A's fetch finishes after a later click on voice B, A replaces B as the audible clip.
- **[F101] Critical worker log records are hidden in the Log tab.** The gateway streams Python log levels unchanged in lowercase (`server/tts_api_server.py:4684-4694`), and the worker manager emits a `critical` record for a health-loop failure (`server/worker_manager.py:763-768`). The Log tab has filters only for info, success, error, and warning and drops other levels (`server/static/tab-log.js:6-7, 73-82`), so that critical message is absent even with every visible filter enabled.
- **[F102] A failed voice-list refresh appears as an empty library.** The Voices tab replaces its list with `[]` on any `/api/voices` error, then removes saved selections (`server/static/tab-voices.js:49-61`). Its empty state says no voice files were found (`70-74`) rather than showing the request failure, so a transient gateway error can look like lost voice data until another successful refresh.

## Fifth pass findings

This pass concentrated on paths less examined in earlier passes: discovery and request forwarding, CLI behavior, job and project persistence, file export, model-specific validation, and Whisper device and verification behavior. Findings are source traced. Conditional races and model outcomes were not reproduced against the live app.

### Critical

- **[F103] A CLI URL override can send this server's master token to another host.** `Config` resolves `--url` or `TTS_API_URL` independently of the token and still falls back to the local registry or token file when no explicit token is given (`tts.py:130-164`). Every request then adds that local token to the chosen URL as `X-TTS-API-Token` (`tts.py:188-213`). A user pointing the CLI at a different server can disclose this server's credential to that endpoint. Even `--token ""` does not suppress fallback because the code tests the argument for truthiness (`149-164`).

### High

- **[F104] A negative chunk size bypasses the bridge's 100 MB request limit.** The bridge parses each chunk size with `int(size_str, 16)` but never rejects negative values (`bridge.py:243-269`). A malformed chunk header of `-1` makes `total` smaller and passes the cap check, then calls `self.rfile.read(-1)`, which reads until EOF without that limit. On the reachable WSL bridge, an unauthenticated client can make one request hold a thread and consume unbounded body memory. This is a specific bypass beyond the fourth-pass bounded-upload resource issue.
- **[F105] An allowed-root `save_path` can create a job outside every allowed root.** `_resolve_output_paths` permits a path equal to `OUTPUT_DIR` or `PROJECTS_OUTPUT`, and permits bare `.` resolving to the latter, but creates the job beneath that path's parent (`server/tts_api_server.py:3514-3563`). With default paths, `save_path=.` creates `<app>/projects_output_<id>` beside `projects_output`, and an absolute `save_path=OUTPUT_DIR` creates `<app>/output_<id>` beside `output` (`server/config.py:69-74`). `JobManager.get_job_dir` rejects the resulting job, and restart indexing never scans it (`server/job_manager.py:430-456, 543-557`).
- **[F106] A Whisper worker requested on CPU can still load the model on GPU.** The worker records its requested device, but lazy transcription calls `whisper.load_model` without passing that device (`server/tts_worker.py:109-113, 2151-2161`). [Whisper's loader](https://github.com/openai/whisper/blob/main/whisper/__init__.py) selects CUDA by default when available. An explicitly CPU-configured Whisper worker on a CUDA machine can therefore consume GPU memory and report a device that differs from where its ASR model runs.
- **[F107] Whisper verification can reject correct scripted speech.** The pipeline compares raw Dia and VibeVoice chunks, including `[S1]` and `Speaker 1:` controls, with the ASR transcript (`server/tts_api_server.py:2469-2474, 3344-3352`; `server/text_utils.py:189-239`). The sanitizer converts those controls into ordinary tokens rather than removing them (`server/text_utils.py:252-262`). For a short correctly spoken `Hi`, `[S1] Hi` versus `Hi` scores about 67% and `Speaker 1: Hi` versus `Hi` about 50%, below the default 80% threshold. Orpheus and VoxCPM2 controls can similarly enter this comparison.

### Medium

- **[F108] F5 accepts a reference list that its worker never uses.** The shared requirement check treats any `reference_audios` entry as a valid reference for both F5 and VibeVoice (`server/tts_api_server.py:2015-2034`). F5 inference reads only `voice` or singular `reference_audio` and raises when neither is present (`server/tts_worker.py:1199-1221`). A request with only `reference_audios=["saved.wav"]` passes validation, can create a job and start F5, then fails during inference instead of returning an immediate 400.
- **[F109] VibeVoice list references bypass gateway validation.** `_normalize_reference_inputs` validates a singular `voice` or `reference_audio` but never visits `reference_audios` (`server/tts_api_server.py:3455-3511`). VibeVoice validates each list entry only inside the worker after job creation and model startup (`server/tts_worker.py:1399-1418`). A missing or disallowed path therefore becomes a failed synthesis job rather than a request validation error; the worker's containment check still prevents reading outside allowed roots.
- **[F110] Dry-run can approve a request that actual submission rejects.** `/api/tts/{model}/dryrun` describes itself as request validation but checks model, text, chunks, and optional save path only (`server/tts_api_server.py:5260-5321`). The real handler additionally validates reference inputs, rejects `save_path` with `deliver_to`, and validates the delivery path (`2216-2218, 2308-2318`). A dry-run with both output fields or an invalid reference can return a normal plan while the same request fails at submission.
- **[F111] The Whisper VRAM estimate blocks small transcription models on an 8 GiB GPU.** `MODEL_VRAM_ESTIMATE_GB` assigns Whisper 10 GiB (`server/config.py:521-530`), and worker capacity checking treats that estimate plus a 1.5 GiB default reserve as a start requirement (`server/worker_manager.py:48-69`). The normal request size is `base`, whose configured checkpoint is about 145 MB, and the worker starts with no size loaded (`server/config.py:497`; `server/tts_api_server.py:4524-4531`; `server/tts_worker.py:710-713`). On a machine whose detected default is an 8 GiB CUDA device, even base transcription is refused before its worker starts.
- **[F112] Long compressed reference clips are fully decoded before trimming.** `_read_and_trim_ref_audio` and `_trim_ref_audio_path` call `sf.read` without a frame bound and only then slice to 15 or 30 seconds (`server/tts_worker.py:939-978`). The gateway accepts references up to 50 MB and saved file paths without a duration limit (`server/tts_api_server.py:588, 3483-3499, 4453-4474`). A valid long MP3 or FLAC can expand to hundreds of MB or more before the intended OOM-safety trim.
- **[F113] Waveform extraction loads an entire source to return a small envelope.** `extract_peaks` reads the complete audio into memory, converts or reshapes it, then returns at most 4,096 buckets (`server/audio_editor.py:569-619`). `/api/audio/peaks` exposes this for saved job and project audio (`server/tts_api_server.py:1731-1745`). Opening a sufficiently long output in the Editor can consume substantial gateway memory or fail before the small waveform is returned.
- **[F114] A render can replace a tracked job artifact.** `output_path` may target any audio file under the voice, output, or project roots; with `overwrite=true`, existence is the only additional check (`server/tts_api_server.py:1618-1641, 1758-1794`). `render_to_file` then replaces that file (`server/audio_editor.py:515-547`). Targeting a job chunk or final audio can change an active or published job's bytes while its manifest, verification, and subtitles continue to describe the previous audio.
- **[F115] Re-uploading a voice exposes a partial file to concurrent readers.** The upload endpoint writes directly over the saved destination after reading the request (`server/tts_api_server.py:4453-4474`). A worker trimming that voice or a concurrent transcription can open it while it is truncated or partly rewritten (`server/tts_worker.py:939-978`). This is separate from the already recorded stale-transcript sidecar after re-upload.
- **[F116] Job and project ZIP endpoints can return successful incomplete archives.** The shared builder catches `OSError` for each source file and silently skips that file (`server/tts_api_server.py:5362-5387`). Both job and project ZIP routes return the archive without reporting omissions (`5397-5414, 5972-5986`). A permission error or file disappearing during export can leave a ZIP missing artifacts while the download appears complete.
- **[F117] Audio rendering downmixes stereo even when no edits were requested.** `render_to_file` calls `_to_mono` on every source before `apply_edits`, and `_to_mono` averages channels (`server/audio_editor.py:66-69, 515-517`). Rendering a stereo source with an empty edit chain therefore irreversibly changes it to mono instead of preserving the input audio.
- **[F118] Different-format renders can collide on a temporary WAV filename.** A WAV destination `same.wav` uses `.tmp.same.wav` as its output temporary file, while an MP3 destination `same.mp3` uses that same path for its intermediate WAV (`server/audio_editor.py:526-547`). Concurrent renders of different destination files can overwrite or delete one another's temporary audio. The earlier report covered collisions when both renders target the same destination; this one needs only a shared stem.
- **[F119] CLI `--out` does not determine the generated audio format.** The CLI sets `output_format` only when `--format` is present (`tts.py:796-809`) and writes the returned bytes to `--out` without checking its extension (`tts.py:831-855`). For example, `--out speech.mp3` without `--format mp3` stores default WAV bytes in a file named `.mp3`.
- **[F120] CLI `--out` is silently ignored when combined with `--save-path`.** The gateway omits inline audio and `output_url` for a saved job (`server/tts_api_server.py:3059-3072`). The CLI still chooses the local `--out` destination, but if neither audio field exists it sets `out_path=None` and exits successfully without writing the requested local file (`tts.py:835-868`).
- **[F121] CLI job listing filters only the gateway's default first 50 rows.** `tts jobs list` requests bare `/api/jobs` and applies its status and limit options locally (`tts.py:873-887`). The gateway defaults to 50 and supports server-side status, limit, and offset (`server/tts_api_server.py:1241-1286`). Older matching jobs are invisible, and asking the CLI for more than 50 cannot return them.
- **[F122] Synchronous CLI batches retain and can print every full audio response.** `cmd_batch` stores each complete TTS result, including `audio_base64`, in its `results` list and JSON mode prints them all (`tts.py:1125-1149`). The gateway can inline up to 25 MB per job (`server/tts_api_server.py:591, 3060-3068`). A moderate `wait=true` batch can hold hundreds of MB of base64 in memory and emit unexpectedly large JSON, unlike single-job CLI output, which strips the audio field (`tts.py:861-868`).
- **[F123] An `incomplete` synthesis can make the CLI exit successfully without audio.** The pipeline can return `status: incomplete` with HTTP 200 when chunks are missing (`server/tts_api_server.py:2915-2928, 2416-2428`). `cmd_tts` treats that response as a normal result, prints its summary, and returns without an error exit even when it writes no file (`tts.py:831-870, 2368-2399`). This is a client failure on top of the earlier persisted-running-job issue.
- **[F124] Long format conversion can silently change the requested format to WAV.** `convert_format` has a fixed 120-second FFmpeg timeout regardless of input length (`server/audio_assembler.py:120-158`). The pipeline permits up to 100,000 characters and 500 chunks (`server/tts_api_server.py:589-590`) and treats a conversion `RuntimeError` as a successful WAV fallback (`2958-2969`). A legitimate long MP3, OGG, FLAC, or M4A job can return WAV when encoding exceeds that fixed cap.
- **[F125] Source-guided captions can extend past the end of the audio.** For a trailing source word omitted by Whisper, alignment synthesizes a future span and caps it only if `audio_duration > left` (`server/tts_api_server.py:5656-5665`). If the last matched word already ends at `audio_duration`, an omitted trailing word receives a timestamp after the media ends; SRT construction writes it unchanged (`5521-5542`).
- **[F126] Local Whisper fallback forces the request's default English setting.** Worker-based verification omits `language` and lets Whisper auto-detect (`server/tts_api_server.py:3338-3342`), while the local fallback passes the pipeline's `language`, which defaults to `en`, into transcription (`619, 2471-2474, 3383`; `server/audio_processing.py:282-325`). Non-English speech can therefore verify differently, or fail, only when the worker path falls back to local Whisper.

### Lower impact

- **[F127] Delivered jobs cannot be removed through job deletion.** Delivered manifests remain retrievable through `_get_job_or_delivered` after their work directories are purged (`server/tts_api_server.py:470-526, 3033-3040`). `/api/jobs/delete` calls only `JobManager.delete_job`, which searches active job directories and cannot find those archived manifests (`server/tts_api_server.py:1981-1991`; `server/job_manager.py:478-507`).
- **[F128] Job-list pagination silently stops at 10,000 before filtering.** `/api/jobs` asks `JobManager.list_jobs(limit=10_000)` and applies filters and offset only to that truncated result (`server/tts_api_server.py:1260-1286`; `server/job_manager.py:426-428`). On a library with more than 10,000 jobs, older matches disappear and the reported `total` is too small.
- **[F129] A saved voice upload accepts empty or nonaudio bytes.** The upload route checks only filename extension and size, then writes the content (`server/tts_api_server.py:4453-4474`). `/api/voices` lists that file as a usable voice (`4432-4450`), but worker audio readers fail when it is selected (`server/tts_worker.py:939-978`).
- **[F130] Audio edit listing can fail during a concurrent deletion.** It stats every entry in the sort key before entering the later per-file `OSError` handler (`server/tts_api_server.py:1883-1890`). If a render or delete removes an entry between directory enumeration and sorting, `/api/audio/edits/{job_id}` returns a server error instead of skipping it.

## Coverage and reproducibility gaps

- The four test modules cover text chunking, sidecar paths, one bridge shutdown path, and documentation. They do not exercise the gateway pipeline, worker concurrency, delivery, recovery, or browser generation flow (`server/test_text_utils.py`, `server/test_sidecar_paths.py`, `server/test_bridge_shutdown.py`, `server/test_manual.py`).
- Base setup dependencies have open-ended version ranges or bare package names without a lockfile; the setup script calls out this reproducibility and supply-chain gap (`server/setup.sh:280-300`).
- Several model weight downloads use the moving default revision rather than an immutable commit (`server/install_model.sh:580-644, 759-763, 870-875, 880-885`). Repeating an install at a later date can produce different model files even if the Python environment is unchanged.
- Fish Speech's runtime-override reuse hash includes the source directory name and dependency strings, but not the pinned repository HEAD (`server/install_model.sh:183-199, 785-801`). If its pinned commit changes and installation reruns, `clone_repo` updates the checkout while `install_override_runtime_only` can reuse Python code installed from the prior commit unless force reinstall is set (`server/install_model.sh:686-710`).

## Historical audit verification (before repairs)

- An isolated standard-library concurrency probe reproduced the shared asyncio.Lock failure mode; it did not import or run application code.
- All 21 Python files parsed successfully with `ast.parse`.
- All seven browser JavaScript files passed `node --check`.
- Current upstream Kokoro, Coqui, and Hugging Face source/docs were checked for the model-loading findings. Installed dependency versions were not inspected, so those integration findings should be confirmed against the resolved packages before a fix.
- No live model, gateway, bridge, or browser behavior has been reproduced in this audit.
- The fourth pass used additional static call-path and state-transition tracing only; no live services, installations, or model downloads were run.
- The fifth pass likewise made no live requests or model runs; the Whisper device-default behavior was checked against upstream source.

## Repair resolution ledger

Status for each row: **implemented**. Testing scope and limits are recorded above and in the verification section below.

| ID | Change | Files |
| --- | --- | --- |
| F001 | Bridge forwards the actual client IP and gateway bootstrap rejects nonlocal clients; HTML is not cached. | `bridge.py`, `server/tts_api_server.py` |
| F002 | Reference inputs are validated and copied into each new job for recovery and reruns. | `server/tts_api_server.py` |
| F003 | Busy/starting workers are recognized, and inference waits for atomic reservation instead of immediately spawning or failing. | `server/tts_api_server.py`, `server/worker_registry.py` |
| F004 | Delivery extension resolves the encoder before job creation. | `server/tts_api_server.py` |
| F005 | Delivery failure fails the job and retains its work files. | `server/tts_api_server.py` |
| F006 | Missing chunks persist a failed state and produce an error response. | `server/tts_api_server.py` |
| F007 | Testing fetches output_url when inline audio is omitted. | `server/static/tab-testing.js` |
| F008 | Listings merge archived delivery manifests; Editor resolves delivered audio and stores edits separately; saved_to names the delivered file. | `server/tts_api_server.py` |
| F009 | Reference staging is covered by cleanup during validation and submission failures, including multipart Pydantic errors. | `server/tts_api_server.py` |
| F010 | Cancellation is checked between final stages and completion is serialized with cancellation under the job lock. | `server/tts_api_server.py`, `server/job_manager.py` |
| F011 | Unavailable Whisper verification raises an explicit error rather than recording a perfect pass. | `server/tts_api_server.py` |
| F012 | Project deletion rejects active jobs and propagates filesystem failures. | `server/tts_api_server.py` |
| F013 | Garbage collection reads job status and skips running jobs. | `server/job_manager.py` |
| F014 | Voice replacement invalidates the prior transcript. | `server/tts_api_server.py` |
| F015 | Voice rename preserves the actual extension and checks destination/sidecar conflicts. | `server/tts_api_server.py` |
| F016 | Whisper calls atomically reserve a worker until transcription finishes. | `server/tts_api_server.py`, `server/worker_registry.py` |
| F017 | Device load serialization uses a threading lock with async polling, shared across event loops. | `server/worker_manager.py` |
| F018 | Installers use private temporary directories; shared temporary-tree wipes were removed. | `server/install_model.sh`, `server/tts_api_server.py` |
| F019 | Playback no longer revokes history-owned blobs. | `server/static/app.js` |
| F020 | Reference files are sent only for engines exposing reference-audio support. | `server/static/tab-testing.js` |
| F021 | Readiness checks require completed package markers and required nonempty weights or a validated download inventory. | `server/tts_api_server.py`, `server/install_model.sh` |
| F022 | Whisper size load/unload calls operate on the actual worker cache and health reports loaded sizes. | `server/tts_worker.py`, `server/tts_api_server.py` |
| F023 | Device input canonicalizes cuda to cuda:0 before capacity checks and worker matching. | `server/worker_manager.py`, `server/tts_api_server.py` |
| F024 | CLI waits for the aggregate installation and evaluates requested models and retained installer outcomes. | `tts.py` |
| F025 | Fast setup validates imports and the pinned base dependency versions before exiting. | `server/setup.sh` |
| F026 | CLI wait recognizes cancelled/incomplete states as terminal failures. | `tts.py` |
| F027 | CLI streaming fails on premature EOF or timeout before completion. | `tts.py` |
| F028 | Batch submission allows installation time, records per-job failures, and returns a failing exit status. | `tts.py` |
| F029 | Qwen installs torchvision 0.21.0 against the pinned Torch 2.6.0 and rejects an incompatible base with repair instructions. | `server/install_model.sh`, `server/setup.sh` |
| F030 | GUI range coordinates are mapped through earlier cuts, trims, tempo changes, and padding. | `server/static/tab-jobs.js` |
| F031 | Voice refresh repopulates Testing saved-voice selectors. | `server/static/tab-voices.js`, `server/static/tab-testing.js` |
| F032 | Configuration is retried during polling; successful recovery rebuilds dependent controls. | `server/static/app.js` |
| F033 | Source switching clears old audio/peaks and generation guards reject stale loads. | `server/static/tab-jobs.js` |
| F034 | Testing enforces F5 reference audio before submission and formats structured API errors readably. | `server/static/tab-testing.js` |
| F035 | Chunk text edits update input_text and persisted parameters.text. | `server/tts_api_server.py` |
| F036 | Chunk edits and reruns reject active jobs before mutating metadata or files. | `server/tts_api_server.py` |
| F037 | Missing effect dependencies, unknown effects, and processing errors fail explicitly. | `server/audio_editor.py`, `server/tts_api_server.py` |
| F038 | Each audio render gets a unique temporary output path. | `server/audio_editor.py` |
| F039 | Startup and maintenance reference cleanup includes every supported audio extension. | `server/tts_api_server.py` |
| F040 | VibeVoice reference preparation is inside the cleanup scope. | `server/tts_worker.py` |
| F041 | Whisper eviction chooses only cached model keys and unload clears access timestamps. | `server/tts_worker.py` |
| F042 | Windows peer discovery does not apply Windows PID checks to WSL process IDs. | `tts.py` |
| F043 | Editor job polling retrieves all pages with deduplication. | `server/static/tab-jobs.js` |
| F044 | Testing offers ordered saved references for VibeVoice Speakers 1 through 4. | `server/static/tab-testing.js` |
| F045 | Shared playback pauses Editor playback and ownership of temporary voice blobs is explicit. | `server/static/app.js`, `server/static/tab-jobs.js`, `server/static/tab-voices.js` |
| F046 | Install waiting emits one final JSON document. | `tts.py` |
| F047 | Recovery admission and active-job registration occur under the same running-job lock. | `server/tts_api_server.py` |
| F048 | Kokoro chooses and caches pipelines by the selected voice's language prefix. | `server/tts_worker.py` |
| F049 | XTTS and Kokoro load the staged local model assets and Kokoro voice files. | `server/tts_worker.py` |
| F050 | Cache/temp maintenance rejects active users and installs, blocks new API work during mutation, and protects ZIP transfers with export leases. | `server/tts_api_server.py` |
| F051 | Recovery resolves missing/None retry settings to MAX_RETRIES. | `server/tts_api_server.py` |
| F052 | Editing/recovery invalidate final and caption metadata; artifact routes refuse stale outputs. | `server/job_manager.py`, `server/tts_api_server.py` |
| F053 | Unverified chunks retain verification_passed null; ordinary inference success does not overwrite verification. | `server/job_manager.py` |
| F054 | Testing sends worker_id; atomic reservation honors it and recovery can replace an exited worker while preserving device selection. | `server/static/tab-testing.js`, `server/tts_api_server.py`, `server/worker_registry.py` |
| F055 | Model-load reuse and error recovery filter by the requested device; pending workers return loading. | `server/tts_api_server.py` |
| F056 | Job streams resolve archived delivery manifests and terminate on their final status. | `server/tts_api_server.py` |
| F057 | Chatterbox parameter defaults distinguish None from explicit zero. | `server/tts_worker.py` |
| F058 | Transcript filenames retain the audio extension; ambiguous legacy stem sidecars are not reused. | `server/tts_api_server.py` |
| F059 | Encoding failure is explicit; completion stores the actual final format and expected filename. | `server/audio_assembler.py`, `server/tts_api_server.py`, `server/job_manager.py` |
| F060 | Setup UI and CLI reject a busy installer response. | `server/static/tab-setup.js`, `tts.py` |
| F061 | Editor deletion checks per-job results before reporting success. | `server/static/tab-jobs.js` |
| F062 | CLI ZIP exports stream to a temporary file and replace the destination after success. | `tts.py` |
| F063 | Bridge error logs use the URL path without query credentials. | `bridge.py` |
| F064 | Job indexing is cached, unknown IDs do not trigger repeated rescans, and initial/list/get scans run off the API event loop. | `server/job_manager.py`, `server/tts_api_server.py` |
| F065 | Readiness polling continues until the caller's deadline, including backend waits longer than 60 seconds. | `tts.py`, `server/tts_api_server.py` |
| F066 | Job/project deletion reports filesystem failure; failed deletions are not counted as success. | `server/job_manager.py`, `server/tts_api_server.py` |
| F067 | Expanded Editor entries refresh their edit listings after changes. | `server/static/tab-jobs.js` |
| F068 | Bridge exposes an independent bridge-health endpoint used during discovery publication. | `bridge.py`, `server/tts_api_server.py` |
| F069 | Intentional shutdown writes a shared marker that stops bridge supervision. | `bridge.py`, `server/tts_api_server.py` |
| F070 | The bridge preserves a living child through its cold startup grace period. | `bridge.py` |
| F071 | A gateway lifetime lock is acquired before token/discovery mutation and released on startup failure or shutdown. | `server/tts_api_server.py` |
| F072 | Bridge authenticates API requests before buffering bodies, limits request threads, and applies read deadlines. | `bridge.py` |
| F073 | Audio profile fields enforce finite numeric values and usable bounds. | `server/tts_api_server.py` |
| F074 | Parallel chunk failure cancels queued work and drains running siblings before publishing a terminal job state. | `server/tts_api_server.py` |
| F075 | Orphan scanning recognizes app-owned engine descendants by inherited worker identity and reaps their process group. | `server/worker_manager.py` |
| F076 | The saved API token is reused across gateway restarts; duplicate starts cannot overwrite it. | `server/tts_api_server.py` |
| F077 | Unload atomically claims the worker, marks it idle on success, and health does not advertise an unloaded process as ready. | `server/worker_registry.py`, `server/tts_api_server.py`, `server/worker_manager.py` |
| F078 | Only the successful unregistering owner releases a worker port, including startup failure, cleanup, and inference timeout paths. | `server/worker_manager.py`, `server/tts_api_server.py` |
| F079 | Install All includes Whisper. | `server/install_model.sh`, `server/tts_api_server.py` |
| F080 | Per-model cancellation remains set through retry gaps and is checked before spawning an installer. | `server/tts_api_server.py` |
| F081 | Base readiness imports multipart, Whisper, audio effects, and other required modules and checks the dependency lock. | `server/setup.sh` |
| F082 | Setup and model installation share one advisory lock. | `server/setup.sh`, `server/install_model.sh` |
| F083 | Aggregate installation records failed_models and publishes failed/cancelled rather than completed. | `server/tts_api_server.py` |
| F084 | Shared audio reading decodes M4A through FFmpeg for references and Editor operations. | `server/audio_io.py`, `server/audio_editor.py`, `server/tts_worker.py` |
| F085 | Caption source preparation strips model speaker/control markup. | `server/tts_api_server.py` |
| F086 | An empty transcription overwrites the prior transcript with an empty sidecar. | `server/tts_api_server.py` |
| F087 | Queued jobs check cancellation before model installation or cold loading. | `server/tts_api_server.py` |
| F088 | Preparation rejects text that produces no spoken chunks before job creation or worker loading. | `server/tts_api_server.py` |
| F089 | Local peer listings remove auth secrets before JSON or table output. | `tts.py` |
| F090 | Numeric model parameters use None-only defaults, preserving supported explicit zero values. | `server/tts_worker.py` |
| F091 | Failed shutdown restores polling and log streaming. | `server/static/app.js` |
| F092 | Render completion only clears the chain that was actually submitted. | `server/static/tab-jobs.js` |
| F093 | Render completion rechecks source/chain generation after awaiting edit refresh before changing the selected source. | `server/static/tab-jobs.js` |
| F094 | A chain that removes every audio sample returns an explicit validation error. | `server/audio_editor.py`, `server/tts_api_server.py` |
| F095 | Peak buckets cover the complete frame range, including trailing partial buckets. | `server/audio_editor.py` |
| F096 | Cancellation checks that the job belongs to the named model. | `server/tts_api_server.py` |
| F097 | Bridge log writes rotate the live log under a lock at the configured size limit. | `bridge.py` |
| F098 | The Windows wrapper probes usable interpreters and preserves the actual command exit code. | `tts.cmd` |
| F099 | Editor and Setup pollers refuse overlapping refreshes. | `server/static/tab-jobs.js`, `server/static/tab-setup.js` |
| F100 | Voice playback uses a generation guard and revokes discarded blobs. | `server/static/tab-voices.js` |
| F101 | Critical log events are included by the error-level filter. | `server/static/tab-log.js` |
| F102 | Voice-list refresh errors retain the previous list and show an error. | `server/static/tab-voices.js` |
| F103 | Unrelated CLI URL overrides do not inherit a local discovery token; explicit empty tokens remain empty. | `tts.py` |
| F104 | Bridge chunked-body parsing rejects negative/invalid sizes, truncated bodies, and invalid CRLF framing. | `bridge.py` |
| F105 | Output path containment is checked after the final job destination is constructed. | `server/tts_api_server.py` |
| F106 | Whisper loads every requested size on the worker's selected device. | `server/tts_worker.py` |
| F107 | Whisper verification compares spoken text after removing model control markup. | `server/tts_api_server.py` |
| F108 | F5 rejects list-only reference inputs; its required single reference must be present. | `server/tts_api_server.py` |
| F109 | Every list reference is normalized and validated before worker work and job creation. | `server/tts_api_server.py` |
| F110 | Dry run shares generation preparation and reference validation without creating a job. | `server/tts_api_server.py` |
| F111 | Base Whisper worker admission uses a base-size estimate while size-specific planning metadata remains conservative. | `server/worker_manager.py` |
| F112 | Reference trimming reads a bounded frame range and M4A decoding is bounded by reference duration. | `server/audio_io.py`, `server/tts_worker.py` |
| F113 | Waveform peak extraction streams bounded blocks instead of decoding the full file into memory. | `server/audio_editor.py` |
| F114 | Render output validation rejects replacement of tracked job chunks/finals outside the edits directory. | `server/tts_api_server.py` |
| F115 | Voice uploads use a staging file followed by atomic replacement. | `server/tts_api_server.py` |
| F116 | ZIP export aborts and removes its temporary archive if any source cannot be included; symlinks are rejected. | `server/tts_api_server.py` |
| F117 | Rendering preserves channel count and applies effects per channel. | `server/audio_editor.py` |
| F118 | MP3 intermediate WAVs are derived from a per-render unique filename. | `server/audio_editor.py` |
| F119 | CLI infers output_format from --out and rejects an explicit format mismatch. | `tts.py` |
| F120 | Saved-job responses include output_url; CLI also downloads by job ID when necessary. | `server/tts_api_server.py`, `tts.py` |
| F121 | CLI job listing sends server filters and paginates until its requested limit is reached. | `tts.py` |
| F122 | Batch results discard inline audio before accumulation and JSON output. | `tts.py` |
| F123 | Single-job CLI requires completed status before writing/reporting audio success. | `tts.py` |
| F124 | Conversion timeout scales with audio duration and conversion failure never silently changes format. | `server/audio_assembler.py`, `server/tts_api_server.py` |
| F125 | Source-guided caption timing clamps all generated spans to audio duration. | `server/tts_api_server.py` |
| F126 | Local verification uses language auto-detection consistently with the worker path. | `server/tts_api_server.py` |
| F127 | Job deletion removes delivered manifests and their saved edits while preserving externally delivered artifacts. | `server/tts_api_server.py` |
| F128 | Filtering/pagination use the full indexed library without the 10,000-row truncation. | `server/job_manager.py`, `server/tts_api_server.py` |
| F129 | Voice upload rejects empty/undecodable audio before publishing it. | `server/tts_api_server.py` |
| F130 | Edit listing handles disappearing entries inside the stat error boundary. | `server/tts_api_server.py` |

## Additional findings and fixes during repair

| ID | Finding | Resolution |
| --- | --- | --- |
| F131 | urllib redirects could forward API credentials to another origin even when the original URL was trusted. | CLI and skill helper reject cross-origin redirects. |
| F132 | The bridge streamed the log SSE route but buffered per-job SSE, delaying job progress until the stream ended. | All job `/stream` routes use the streaming proxy path. |
| F133 | Kokoro loading did not consistently honor the worker's requested device. | The explicitly constructed local KModel is moved to the resolved worker device. |
| F134 | Persistent Hugging Face `.lock` files could be treated as evidence of an incomplete download. | Readiness checks incomplete downloads and required artifacts/inventories; persistent lock files alone do not invalidate completed weights. |
| F135 | A transcription finishing after a saved voice replacement could attach the previous clip's text to the new audio. | Compare file identity/size/mtime after transcription; return HTTP 409 for replacement or removal. |
| F136 | Model removal and cache clearing could report success after failed filesystem deletion; removal could also race active jobs/installers. | Propagate model deletion failures, report per-cache errors, and refuse model removal during active work. |
| F137 | Installation wait validation needed to distinguish intentional VITS packages-only state and retained failed/cancelled installer outcomes. | Setup exposes weights_on_demand; CLI honors that state plus aggregate/per-model terminal results. Cold inference avoids reinstalling a valid VITS package layer. |
| F138 | Large single-job CLI output still used a full-memory HTTP read despite streamed ZIP downloads. | Generated output_url downloads use the same temporary-file streaming path. |
| F139 | Inline reference audio without the optional reference_audio_name passed None to Path and failed before synthesis. | A missing/None filename defaults to WAV; failed staging writes are cleaned up and path references must be regular files. A regression test covers nameless inline reference audio through the pipeline. |

## Coverage and reproducibility resolutions

| ID | Resolution | Remaining verification limit |
| --- | --- | --- |
| C1 | Added `server/test_audit_regressions.py` and `server/test_browser_regressions.cjs` for gateway/job lifecycle, references, delivery, cancellation, worker reservation, Whisper cache/device handling, audio/M4A, CLI, and browser races. Existing tests remain. | Inference is simulated; browser tests exercise logic without a live browser DOM or launcher. |
| C2 | Added exact base requirements and a transitive constraint lock from the child distro; setup pins Torch/torchaudio and validates imports/versions on its fast path. Resolver dry-run and actual readiness check passed. | No clean environment install was performed. Existing optional model package conflicts are not proof of a clean reinstall result. |
| C3 | Former moving Hugging Face defaults now resolve through immutable commits in `server/model_revisions.json`; existing explicit revision pins remain. Downloads create a completion inventory. | No weight downloads were run in this repair pass. |
| C4 | Fish runtime reuse hash includes the checked-out repository HEAD. | No Fish reinstall/inference was performed. |

## Repair verification

- **65 Python tests passed** in the child distro (43.99 seconds): all five test modules, including the new regression suite. Coverage includes complete simulated pipelines, external delivery and failure, recovery, cancellation, archived-job editing/deletion, worker/device reservation, Whisper sizes/LRU/CPU placement, multipart/inline references, M4A decoding, waveform tails, stereo rendering, CLI installation states, and gateway locking.
- **9 browser logic tests passed** with Node: edit-range mapping, playback ownership, paginated polling, render/navigation races, voice playback races, selected-worker generation with large-output download, ordered VibeVoice references, and F5 reference validation.
- Python compilation, all seven browser JavaScript syntax checks, both installer shell syntax checks, and Ruff undefined-name/local-variable checks passed.
- The actual base readiness function passed in the existing child environment. `pip install --dry-run -c server/requirements-base.lock -r server/requirements-base.txt` accepted the pinned base dependency set without installing it.
- The Windows `tts.cmd --help` smoke check passed.
- The XTTS local-load arguments were checked against the [Coqui Python API](https://github.com/idiap/coqui-ai-TTS/blob/dev/TTS/api.py) and its [synthesizer](https://github.com/idiap/coqui-ai-TTS/blob/dev/TTS/utils/synthesizer.py). This source check does not substitute for real XTTS inference. Kokoro's installed loader/pipeline source was inspected during repair.

Reproduction commands are in [README.md](README.md#checks). Test-only pytest dependencies were installed under `/opt/tts_server/cache/audit-test-deps`; the base/model package installation was not modified. Full clean-install, GPU inference across engines, and native GUI/launcher verification remain outside the completed automated checks.

The manual and API/CLI documentation were updated for persistent tokens, Whisper size operations, reference snapshots, delivered-job editing/deletion, strict effect failures, voice sidecars, pagination, install outcomes, and maintenance guards.

## Live wiki capture pass - 2026-09-25

This follow-up exercised the running app to produce the manual screenshots and repository hero.

| ID | Finding | Fix and evidence |
| --- | --- | --- |
| F140 | Testing initialization referenced an undefined `model` after saved voices loaded, raising a real browser `ReferenceError`. The optional VibeVoice speaker controls also lacked their model-change visibility update. | Removed the misplaced reference from `init()` and moved the speaker visibility update into `_onWorkerChange()`. A regression test covers initialization with saved voices and switching from VibeVoice to Kokoro. The real Chromium capture completed with no uncaught page errors. |
| F141 | Setup always displayed a made-up `/mnt/.../models` location and told every packages-only engine that weights arrived on first use. Most engines require explicit weight installation before worker loading. | Store and show the real `models_dir` returned by `/api/config`. Show **Install Weights** for packages-only engines unless the backend explicitly declares `weights_on_demand`. The live Setup screenshot shows `/opt/tts_server/data` and the corrected labels; clicking Kokoro's Install Weights completed successfully. |

Documentation corrections found during the walkthrough:

- Editor **Save As** has Name and Format controls; the manual incorrectly described an absolute-path field. Absolute destinations are CLI/API features. The corrected instructions were exercised with `studio_demo_master.wav`.
- Range selection alone changes no audio and does not restrict full-track effects. Rendering preserves the original source; Play/Download use the loaded source. The illustrated walkthrough explains these steps and the newly loaded EDIT after saving.
- Testing documentation now names both F5 and VibeVoice as requiring reference audio. Setup documentation matches the real directory and weight-install labels.
- Added an illustrated first narration, screenshots for all six tabs, Save As and rendered-edit views, and image capture/provenance instructions.

Verification in this pass:

- Installed Kokoro using the actual Setup control, then spawned its worker on the RTX 3090 (`cuda:1`). The existing installed child distro was used; this was not a clean distro installation.
- Generated real 24 kHz speech with Kokoro `af_heart`: 18.6 seconds of audio, 17.6 seconds generation/processing in the captured session. Downloaded it through Testing, uploaded the synthetic demo WAV through Voices, loaded the real waveform, and saved a rendered edit through the Editor dialog.
- Chromium exercised all six tabs and reported **zero uncaught page errors**. Eight app screenshots and the composed repository hero were inspected visually. Saved token fragments were masked; unrelated voice/job content was excluded through cropping and the real Library filter.
- **10 Node browser regression tests passed**, including the new saved-voice initialization and model-switch test. **Both manual link/content tests passed**, covering the added page and image references. Changed JavaScript and capture-script syntax checks passed.

This session does not establish working inference for the other engines, Whisper transcription, a clean environment install, or the native WebView2 launcher. Capture automation and metadata are in `tools/capture_wiki.py` and `manual/images/README.md`; runtime demo artifacts remain ignored by Git.


## Portable V2 release work - 2026-09-25

This section supersedes earlier portability and release-coverage limits where explicitly tested below. Older sections describe their original audit sessions.

| ID | Finding | Resolution |
| --- | --- | --- |
| F142 | Startup, setup, bridge, and gateway required a fixed E: folder and global distro name, preventing relocation. | A folder-derived distro name and verified registered BasePath bind each copy to its own VHD. Startup refreshes host paths and source links; Stop shuts down only that verified copy before moving. Legacy registrations are accepted only for their own disk. |
| F143 | Shutdown used a global executable-name kill, which could close another portable copy. | Removed global taskkill. The source-built native host manages its own window and backend; fallback termination is limited to its verified distro. |
| F144 | API configuration advertised fixed port numbers even when a copy used other ports. | Configuration reports the actual bridge and gateway ports. |
| F145 | CLI Explorer paths could guess the old distro name from an app-folder substring. | Use the actual distro published in discovery; no fallback to another disk. |
| F146 | Kokoro weights alone did not provide offline pronunciation assets for every language group. | Fresh release includes English spaCy data, Japanese UniDic, Chinese dependencies and all Kokoro voices. Actual CPU synthesis passed for all nine groups with Python socket connections blocked. |
| F147 | Existing opaque native binaries lacked build source, and a source checkout had no portable dependencies. | Added a C# launcher with self-contained .NET, bundled WebView2, Windows Python, verified runtime downloads, a fresh Linux build/export workflow, and checksummed multipart release packaging. Original binaries were preserved in ignored local backup storage. |
| F148 | Setup redundantly rewrote env.conf with shell escaping, although its readers treat values as literal data. Paths containing dollar signs/backticks could be changed by those extra backslashes. | One configure_runtime.py writer now creates literal path values and the server link; removed the redundant shell writer and unused compatibility wrappers. |
| F149 | WSL export can include host-generated hosts/hostname files even after cleaning the live distro. | Added a streaming export sanitizer that replaces hosts, hostname and machine identity in the final tar, and rejects private/build-state paths before compression. |
| F150 | Different ports could start two bridge sessions against the same folder; a failed native start could then shut down an existing headless session through shared discovery. | A per-folder Linux process lock rejects the second bridge before gateway startup. Native close skips shutdown when its own backend has already exited. |
| F151 | Stop could trust stale copied discovery or fail early on malformed registry data. | It verifies the advertised distro and live API output folder before requesting shutdown; failures still terminate only the already-verified local WSL disk. |
| F152 | Job metadata and saved reference paths could retain the previous folder after copying or moving, potentially reading another copy's audio. | Reads rebind the actual job directory, its reference snapshots, and legacy references under app-owned voices/output/projects roots. Built-in voice IDs, input text, and external delivery paths stay literal. Three relocation/containment regression tests cover these cases. |
| F153 | Headless startup returned before custom-port discovery existed; an immediately launched CLI could cache the default URL and time out. | Headless startup now waits for a healthy bridge and discovery file before returning, and detects a backend that exits during startup. |
| F154 | Stop claimed the folder was safe to move even when older WSL retained a VHD handle while another distro was running. | Stop checks exclusive disk access before reporting it released. Added an export-based Copy-TTSServer.ps1 transfer that preserves the source and other running distros, plus incomplete-transfer startup protection. Documentation explains the extra snapshot space and manual global-shutdown alternative. |
| F155 | An Explorer-launched Start window disappeared on errors; Stop could lose its failure status after pausing, and locked-disk warnings were not kept visible. | CMD wrappers retain the PowerShell exit code and pause on failures. Stop returns failure when a manual move remains unsafe; the export helper explicitly allows the stopped-but-locked state. |

Verification during preparation:

- Fresh Ubuntu 24.04.4 build successfully installed the pinned shared environment and Kokoro, without exporting the private development distro.
- Nine Kokoro voice/language groups generated finite, nonempty real audio on CPU, first online to warm pronunciation data and again with Python network connections blocked.
- Native WinForms/WebView2 smoke check passed using the bundled browser: connected app, six tabs, TTS icon, screenshot, and graceful shutdown. The screenshot was visually inspected.
- 71 backend tests passed after portability changes (99.27 seconds); 10 browser regression tests and both manual checks passed. Undefined-name/local-variable checks passed.
- Windows embedded-Python CLI help and PowerShell syntax checks passed. The actual native test host is Windows 10 build 19045, WSL 2.0.14. Windows 11 was not separately tested. Automatic approval review blocked a later native duplicate-session retest without giving a detailed reason; final download-directory and duplicate-close changes are compile-verified only.

Other optional engines have not been rebuilt or inference-tested in the fresh release environment. Offline Kokoro does not imply offline Edge, gated-model access, or preinstalled Whisper weights. Release extraction and actual folder-move results are recorded separately when completed.


Additional clean-build evidence:

- The bundled Windows CLI discovered bridge 19300/gateway 18300 and generated 24 kHz, 5.66-second WAVs on both CPU and RTX 3060 CUDA. CPU request elapsed 37.06 seconds; GPU request elapsed 19.47 seconds during cold/loading work. These are observations, not benchmark promises.
- The complete browser walkthrough was repeated against the clean build, including a fresh 18.6-second narration, synthetic voice upload, Editor effects, and saved edit. The warmed narration took 2.1 seconds in that session. All eight wiki images were refreshed with the TTS icon and visually inspected; zero uncaught browser errors.
- Stop-TTSServer.ps1 completed graceful shutdown and terminated only the verified clean-build distro. No original working distro was stopped by that command.
- No common Hugging Face/GitHub token or private-key patterns were found in source candidates. All 11 representative private/runtime/build exclusion checks passed.

- After the saved-job relocation fix, the full Python suite passed **74 tests in 46.69 seconds**, including copied-reference ownership, legacy voice paths, and traversal rejection.

Actual release-image and transfer acceptance:

- The first four-part candidate archive was verified and extracted into a new folder containing spaces. Its bundled sanitized Linux image imported successfully. All nine Kokoro language groups passed real offline CPU synthesis again inside that imported image.
- The bundled Windows CLI generated a 24 kHz, 5.66-second CPU WAV in 7.57 seconds. A second bridge against the same folder on different ports exited 78; the first session remained healthy.
- Direct folder movement correctly exposed the old WSL retained-handle issue (F154). The new export-based transfer completed into a different folder, preserved the original, and left an unrelated WSL app running. An incomplete transfer was refused before startup; source/nested destinations were also refused.
- At the destination, a new folder-bound distro imported the transferred image. Headless startup returned only after discovery and custom ports 19300/18300 were ready. An existing job's downloaded WAV matched its original SHA256 exactly, its metadata resolved under the new folder, and a new 5.66-second audio edit was saved there.
- Fresh destination GPU synthesis on the RTX 3060 produced a nonempty 24 kHz, 5.66-second WAV in 14.37 seconds. The source and transferred copies remain in ignored local build output as verification evidence.
- The final hero image was visually inspected. PowerShell start/stop/copy/extractor syntax, both manual checks, 10 browser tests, and source secret-pattern/ignore checks passed after the transfer changes.


## First-launch and release usability follow-up - 2026-09-25

| ID | Finding | Resolution |
| --- | --- | --- |
| F156 | The source-built desktop host lived under runtime/launcher, while the original root EXE had been moved to ignored legacy build output. The release had no obvious root EXE, and initial preparation could be invisible. | Added a statically linked native C TTSServer.exe with the TTS icon, progress window, startup log, and actionable errors. The package explicitly includes this ignored build artifact. Start checks WSL readiness and the required payload. A real native launch from a folder with spaces passed connected app, portable download, and graceful shutdown. Testing caught and corrected the bootstrap's initial command-line quoting error before publication. |
| F157 | New users had to collect split assets or use a terminal to get the full release. Extraction published directly into the final destination, leaving an unusable folder after interruption. | Added Download-TTSServer.exe with an embedded version-matched extractor and folder picker. It downloads, checks SHA256, extracts into a temporary directory, and launches the root EXE. Explicit archive path checks prevent escape. Existing destinations are preserved. Five fixture checks passed: valid extraction with spaces/apostrophe, existing destination, corrupted part, archive traversal, and missing launcher. |

README/wiki instructions now distinguish the downloader, root app EXE, source-only archive, WSL host setup, cache cleanup, and the Portable Linux in a Box origin. The repository description and search topics name speech engines and capabilities.

The single-session native test also verified the final portable WebView2 download-directory change that was previously compile-only. The previously blocked native duplicate-session scenario was not retried; its runtime coverage limitation remains. No unrelated WSL app was stopped.
