# Portable TTS Server V2 — v2.0.0

A portable Windows speech studio with its own WSL2 Linux distro, native desktop window, CLI, and authenticated HTTP API.

## Download and start

1. Download `Extract-Portable-TTS.cmd` and `Extract-Portable-TTS.ps1` into the same folder.
2. Run `Extract-Portable-TTS.cmd -Download` from a terminal. It downloads the manifest and four archive parts, verifies their SHA256 checksums, and extracts the complete app.
3. Run `Portable-TTS-Server-V2\Start-TTSServer.cmd`.
4. In **Server**, spawn a Kokoro worker on CPU or an available GPU; generate from **Testing**.

For manual downloading, get all four `.zip.part001`–`.zip.part004` files, `portable-manifest.json`, and both extraction helpers, then double-click the CMD helper. The approximately **6.3 GiB** download is split because GitHub limits individual release assets to under 2 GiB. The automatic **Source code** downloads do not contain the runtime.

Use Windows x64 with WSL2 and virtualization enabled. Allow at least **30 GiB free** for extraction, first-run import, and working room, plus space for retained archive parts. An NVIDIA GPU is optional; it requires the Windows driver. The launcher is unsigned.

## Included

- Offline-ready Kokoro, all built-in voices, English pronunciation model, Japanese dictionary, and Chinese pronunciation dependencies.
- Fresh Ubuntu 24.04.4 runtime with Linux Python, PyTorch/CUDA user-space libraries, FFmpeg, and audio dependencies.
- Windows Python, a self-contained .NET desktop launcher, and bundled WebView2 Fixed Version.
- New TTS icon, illustrated manual with eight real app screenshots, source code, license inventories, and corresponding Ubuntu package sources.

Other engines install into this copy's Linux disk when chosen. Those installations need internet; gated models require access. Edge uses an online service. Optional Whisper transcription weights are downloaded separately.

## Moving and preserving your installation

Run `Stop-TTSServer.cmd`. Once it confirms the disk is released, copy the entire folder, including `wsl/ext4.vhdx`. If older WSL keeps the disk locked, run `Copy-TTSServer.ps1 -Destination 'D:\Portable-TTS-Server-V2'`; this exports the app's Linux state without stopping unrelated WSL apps. Keep the original until you verify the destination. See the included `PORTABILITY.md` for snapshot space and cleanup instructions.

## Verification and limits

- 74 Python regression tests, 10 browser logic tests, and manual link/content checks passed.
- Fresh release-image import passed real CPU synthesis for all nine Kokoro language groups with Python network connections blocked. Real API/CLI speech passed on CPU and an RTX 3060.
- Transfer to a new folder preserved an existing job's audio byte-for-byte, allowed a new edit, and passed fresh GPU synthesis with correct discovery and paths. An unrelated WSL app stayed running. Incomplete transfers and nested destinations were refused.
- The bundled native browser displayed the connected app and TTS icon, and shut down successfully on Windows 10 build 19045. Final download-directory and duplicate-close changes are compile-verified and still need a native runtime retest.
- Optional engines and Windows 11 were not all separately tested. The release bundles app dependencies; Windows, WSL2, virtualization, and GPU drivers remain host requirements.

The public runtime was built from a fresh official Ubuntu image. Private development disks, personal voices, jobs, tokens, and browser profiles are excluded. Application source is MIT licensed; bundled components retain their own licenses and notices.
