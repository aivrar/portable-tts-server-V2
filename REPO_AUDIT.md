# Repository and portable release audit

Updated 2026-09-25 for **Portable TTS Server V2**, a Windows application using its own WSL2 Linux disk. The original private installation is preserved. The release runtime is built from a fresh official Ubuntu base in a separate staging distro.

## Source tree decisions

| Path | Decision | Reason |
| --- | --- | --- |
| `server/`, `bridge.py`, `tts.py`, `tts.cmd`, `app.json` | Keep | Active API, inference, installers, desktop interface, CLI, and metadata. |
| `launcher/`, `Start-TTSServer.*`, `Stop-TTSServer.*`, `Copy-TTSServer.ps1` | Keep | Source-built native host and portable disk lifecycle/transfer. |
| `assets/`, `server/static/tts*` | Keep | TTS icon master, Windows ICO, app header and favicon assets. |
| `manual/`, root documentation, `skills/use-tts-server/` | Keep | User wiki, genuine screenshots, architecture, integrations, and audit record. |
| `tools/`, `release/` | Keep | Source-controlled build, verification, image capture, extraction helpers, and pinned runtime sources. |
| `runtime/`, `releases/` | Ignore in Git; package as release assets | Large bundled dependencies and downloadable archives. |
| `wsl/`, `linux/`, VHD files | Ignore | Private original disk and obsolete inherited/bootstrap Linux images. Never use them as public release inputs. |
| `secrets/`, `output/`, `projects_output/`, `voices/`, `cache/` | Ignore | Tokens, jobs, recordings, models/caches, build staging, profiles, logs, and local state. Preserve the user's data. |
| Old root `TTSServer.exe`, `webview.dll` | Ignore; preserved in local backup | Superseded opaque launcher assets. Replaced by the C# project and self-contained release launcher. |
| Agent scratch, backups, pycache, pytest/ruff cache, bin/obj and editor artifacts | Ignore | Local or reproducible generated data. |

The original `linux/` contains inherited kernel/template and other architecture images that this V2 startup path does not use. The fresh release uses `runtime/linux-rootfs.tar.gz`. Neither private disk nor old bootstrap snapshot is uploaded to Git.

## Code and documentation

The detailed findings and repairs are in [CODE_AUDIT_FINDINGS.md](CODE_AUDIT_FINDINGS.md). The audit removed confirmed unused state/rules and redundant configuration helpers while retaining active fallbacks, framework handlers, and future extension points.

Portability no longer requires an E: path. Every copy is checked against its registered backing disk. The CLI reads the actual distro from discovery. Shutdown controls only its own copy. Public release images are built independently; package download caches and machine-specific state are cleaned before export.

The manual covers all 20 engines, CLI/API operations, six GUI tabs, editing, diagnostics, startup, optional downloads, and moving the stopped app. Examples using `E:\tts_server` are identified as examples. Kokoro is bundled offline; the remaining engines install when chosen. The new TTS icon is integrated into the native executable, window, browser, app header, and repository hero.

Eight genuine app screenshots document a complete Kokoro narration/editing workflow. The hero is composed from the actual Editor screenshot. Image capture notes record the source and avoid exposing saved credentials or unrelated user audio.

## Verification and boundaries

- 74 backend tests passed after portability changes, plus 10 browser logic tests.
- Manual link/content checks, Python unused-variable/undefined-name checks, and Windows script syntax checks passed.
- A fresh Linux environment installed successfully and generated real Kokoro audio.
- Offline CPU synthesis passed for all nine Kokoro language-code groups with Python network connections blocked.
- The actual multipart archive extracted and its image imported in a new folder with spaces. CPU generation passed. Export-based transfer to another folder preserved existing audio exactly, supported a new edit, refreshed discovery/paths, and passed fresh RTX 3060 generation while an unrelated WSL app remained running.
- The source-built WinForms host loaded the bundled WebView2 runtime and connected interface, displayed the icon, captured a smoke screenshot, and shut down successfully.
- Native validation ran on Windows 10 build 19045 with WSL 2.0.14. A later native retest was blocked by automatic approval review, so the final download-directory and duplicate-close changes are compile-verified only. Windows 11 was not separately tested. Optional engines remain available but are not all rebuilt or inference-tested in the fresh release.

## Distribution

Application source carries the same MIT license as the owner's existing project. Third-party components retain their own licenses, inventories, and corresponding source archives; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The portable archive contains a source allowlist and the clean `runtime/` payload. Installed VHDs, output, tokens, voices, and browser profiles are excluded. ZIP parts are kept below GitHub's 2 GiB per-asset limit and checked by SHA256 before extraction.

WSL2, hardware virtualization, Windows, and an optional GPU driver remain host requirements. Windows Python, .NET runtime, WebView2, Linux dependencies, and Kokoro are bundled. Windows still stores a per-user WSL registration outside the folder.

Final archive extraction, relocation, and publication results are recorded in the release notes and appended audit evidence.
