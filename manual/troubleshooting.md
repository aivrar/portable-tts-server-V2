# Troubleshooting

Failures in this product are usually identity (wrong distro or path), auth (stale token), resources (VRAM/disk), or engine-specific (missing reference, offline Edge, gated Hub). Work the list in that order.

## The app will not start

| Symptom | Cause | What to do |
| --- | --- | --- |
| Extract onto a local Windows drive | UNC/network path | Move the complete stopped folder to a local drive. Spaces in folder names are supported. |
| Bundled launcher or Linux runtime is missing | Source-only or incomplete download | Extract the complete release, including every ZIP part. |
| Name is registered to another location | Registration conflict | Stop and inspect `Start-TTSServer.ps1 -VerifyOnly`. Never unregister a disk you need; unregister deletes its data. |
| Missing backing disk | Installed VHD was removed | Restore `wsl/ext4.vhdx` from your stopped backup. |
| Stop warns that Windows still holds the disk | WSL retains a VHD handle while its shared VM is running | Use `Copy-TTSServer.ps1 -Destination <new-folder>`; see [transfer instructions](../PORTABILITY.md#transfer-while-another-wsl-app-is-running). Do not force-copy the VHD. |
| This portable transfer did not finish | Copy/export failed or was interrupted | Keep the original and repeat the transfer into a new folder. Check free space and permissions. |
| VerifyOnly says not registered | First run | Run Start normally, or use `-PrepareOnly` to import/register without the GUI. |
| Refusing to import over files | Nonempty `wsl` folder without its VHD | Keep the incomplete folder for recovery and extract a fresh release elsewhere. |
| Window never appears | Incomplete browser/launcher runtime | Check `output/run/desktop.log`; restore the complete `runtime` directory. The bundled browser is used, not an Evergreen install. |
| WSL import fails | WSL2/virtualization unavailable or insufficient disk | Check `wsl --status`, enable required Windows components, restart if requested, and check free space. |
| Wrong dedicated distro / BasePath | Another WSL distro was selected | Start with the wrapper belonging to this folder. Use the name returned by VerifyOnly for diagnostics. |
| Port already in use | Another copy/service is running | Stop it or choose unused `-BridgePort` and `-GatewayPort` values. |

The native executable with no arguments invokes the start wrapper. It is a GUI application; use `tts.cmd --help` for command-line help.

## CLI cannot see the server

```
error: server unreachable at http://127.0.0.1:9300
```

Exit code **2**. Start `Start-TTSServer.cmd`. The CLI never auto-launches the GUI.

```
HTTP 401 Missing or invalid API token
```

The supplied token does not match the server. Normal restarts reuse the saved token. Remove a stale `--token` / `TTS_API_TOKEN` override and let discovery re-read `output\run\registry\tts_server.json`. `tts.cmd token` prints the live value.

```
tts.cmd: 'python' not found
```

Restore `runtime/python` from the complete release. Source-only checkouts can use a separately installed Python; the wrapper falls back to `py`, `python`, then `python3`, with exit **127** if none exists.

```
discovery.publish warnings
```

`E:\tts_server\output\run\registry` is not writable. The server will **not** fall back to `%LOCALAPPDATA%`. Fix NTFS permissions on the app tree.

`wait-ready` exit **3** = timeout. Increase `--timeout` or spawn the worker first. Install/job wait exit **4**. Job ended failed = exit **5**.

## Install problems

- **Gated Hub / 401 from Hugging Face** — Save a token on the Setup tab. Accept the model license on the Hub page. Fine-grained tokens need read access to gated repos you can access.
- **Disk full** — Weights are on the VHDX. `tts.cmd disk`. Qwen is ~21GB; Install All is unsafe on a small disk.
- **Stuck Installing...** — Log tab and `tts.cmd logs tail --file setup --lines 500`. `POST /api/setup/cancel/{model}` (or `all`) requests cancellation. `output\run\tts_setup.lock` means another setup still holds the flock.
- **packages_only** — Choose **Install Weights** for a packages-only engine. VITS specifically supports weights on first load; that first call needs network.
- **Remove did not free the Coqui override** — bark, xtts, and vits share `coqui`. The override stays while any of them remain registered.

## Spawn / VRAM

- Toast `Spawn failed: ...` with a reserve message — free VRAM is below `MODEL_VRAM_ESTIMATE_GB` + `TTS_SERVER_GPU_RESERVE_GB` (1.5). Unload other engines (and other Linbox apps). Re-run `tts.cmd devices` immediately before the next load; another app can allocate between clicks.
- Worker stays **loading** then dies — Log tab; `tts.cmd maintenance kill-stale`; confirm Setup status is still `ready`; try Precision Auto or fp16.
- GPU not listed — `nvidia-smi` inside `wsl -d linbox-TTS_Server`. WSL2 NVIDIA drivers must be installed on Windows. Default device falls back to `cpu`.
- Host RAM < 12 GiB free or CPU stuck ≥85% — stop generating; unload; do not run parallel synthesis.

## Generate failures

- `Select a worker first` — Server tab spawn; wait for **ready**.
- VibeVoice needs a reference — pick Saved Voice or Upload.
- F5 / Fish / OuteTTS / CSM / Higgs clone sounding wrong — missing or inexact `reference_text`; use Voices **Transcribe** then paste.
- Fish gibberish — wrong tokenizer era; this app pins S1-mini. Reinstall fish rather than mixing S2 assets.
- Dia cuts off speaker two — shorten turns; keep `[S1]`/`[S2]` on each chunk (the chunker repeats tags, but a single huge turn can still exhaust the token budget).
- Edge fails — no internet, or catalog timeout. Fallback names still work if the package is installed (`en-US-JennyNeural`).
- Parler / OuteTTS / Voxtral worker killed at timeout — infer hit 90/180/300s. Shorten text, keep defaults, unload and retry.
- Acronyms on SpeechT5 — spell them.
- Bark inexact transcript — expected; use kokoro/xtts/edge for narration accuracy.
- Inline audio missing on a huge sync response — over 25MB base64 omission. Download `jobs output <id>`.
- `de_reverb` smearing clean speech — set it back to 0.

## SRT / Whisper

- `GET /srt` 404 — you have not posted `/api/jobs/{id}/srt` and `generate_srt` was not on.
- Captions do not match the script — you passed `source_guided: false`.
- SRT skipped on generate — inspect the response `srt.error` and server log for a Whisper load or transcription failure. Run `tts.cmd install whisper` and `whisper load base` before retrying.
- Verification always fails — lower `tolerance` only after you listen; 80 is the default for a reason. Tiny Whisper mishears on purpose.

## Editor / audio

- Waveform empty — click the Editor tab (canvas was zero-sized while hidden), Refresh, pick FINAL.
- Render reports an unavailable tempo/pitch effect — repair pyrubberband and the Rubber Band executable in the distro, then retry. Run `diagnose` to inspect the environment.
- Render 409 — file exists; `--overwrite` or a new `output_name`.
- `path` render 400 — output or source escaped `voices` / `output` / `projects_output`.

## Shutdown / leftover processes

- Shutdown toast failed — button re-enables; try again; check Log. Do not `wsl --shutdown` unless you intend to stop every distro.
- Registry file still present after a crash — next start replaces it; delete only if a stale file is confusing the CLI while nothing is listening (rare).
- VRAM still occupied — `tts.cmd workers list`; `maintenance kill-stale`; `model unload <id>`; last resort Restart workers.

## Identity reminder

This is a **linux distro for windows users**. If you find yourself in `wsl -d Ubuntu` installing pip packages, you are in the wrong place. Use the dedicated distro reported by `Start-TTSServer.ps1 -VerifyOnly`, backed by this portable folder's `wsl/ext4.vhdx`. Launch through `Start-TTSServer.cmd`. Other local folder paths are supported.

## Related pages

- [Identity and requirements](identity-and-requirements.md)
- [Start, stop, and portability](start-stop-and-portability.md)
- [Diagnostics and maintenance](diagnostics-and-maintenance.md)
- [A to Z knowledge page](a-to-z.md)
