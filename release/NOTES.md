# Portable TTS Server V2 - v2.0.1

19 speech engines plus Whisper, voice cloning where supported, subtitles, audio editing, a Windows CLI, and an authenticated HTTP API. Kokoro and its offline assets are bundled; other engines install into the portable folder when selected.

**Distro source: [aivrar/portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box).** This app originated as its child distro. The public V2 Linux runtime is rebuilt from clean Ubuntu with the speech dependencies included.

## Download and start

1. Download **Download-TTSServer.exe** below and open it.
2. Choose a writable local folder with **35 GiB free**. The helper downloads about **6.3 GiB**, verifies SHA256 checksums, extracts the app, and launches it. Allow time for the download and first import.
3. For later starts, open **Portable-TTS-Server-V2/TTSServer.exe**. Startup progress and errors appear in a window with a WSL setup guide button.
4. In **Server**, spawn a **Kokoro 82M** worker on CPU or an available GPU. In **Testing**, choose a built-in voice and generate speech.

**Host requirements:** Windows 10/11 x64, working WSL2, and virtualization. WSL setup can require administrator approval and a restart. The downloader does not enable Windows features. An NVIDIA GPU is optional and needs its Windows driver. The EXEs are unsigned, so Windows may display an unknown-publisher prompt.

The app contains Python, .NET, WebView2, Linux dependencies, PyTorch/CUDA user-space libraries, FFmpeg, and Kokoro, including pronunciation assets for its nine language groups. No separate app runtime installation is needed. Other engines need internet to install; gated models require access, Edge uses an online service, and Whisper weights are optional downloads.

## What's fixed in 2.0.1

- A clickable **TTSServer.exe is in the main app folder** again, with the TTS icon and visible first-start progress.
- **Download-TTSServer.exe** assembles the full portable release automatically. It is a small native Windows program with no separate .NET or VC runtime requirement.
- Startup checks for missing payload files and WSL readiness before importing the Linux image.
- Extraction validates checksums and archive paths, publishes only a completed app folder, and refuses to overwrite an existing installation.
- The README and wiki describe the engines, new download/start flow, and the Portable Linux in a Box lineage.

## Manual or offline transfer

Download all four `.zip.part001` through `.zip.part004` files, `portable-manifest.json`, and both `Extract-Portable-TTS` helpers into one folder. Double-click the CMD helper. `Extract-Portable-TTS.cmd -Download` can also fetch missing parts. GitHub's automatic **Source code** downloads do not contain the runtime.

Download cache remains in `.tts-download` beneath your chosen folder. Remove that cache after verifying the app works. Existing app folders are never overwritten; choose another destination for a fresh copy.

## Moving and preserving your installation

Run **Stop-TTSServer.cmd** before moving or copying the app. Once it confirms the disk is released, copy the entire folder, including `wsl/ext4.vhdx`. If WSL retains the disk handle, use **Copy-TTSServer.ps1** to transfer without stopping unrelated WSL apps. Keep the original until the destination is verified. See [the wiki](https://github.com/aivrar/portable-tts-server-V2/wiki) for commands and instructions.

## Verification and limits

The root EXE passed a native launch test from a folder with spaces: bundled WebView2, connected app, TTS icon, portable downloads, and graceful shutdown. Extraction checks passed for valid archives, damaged parts, unsafe paths, missing launchers, and preservation of existing destinations.

The actual v2.0.1 release parts also passed extraction through the native downloader, first-import startup from a new folder with spaces, and fresh CPU Kokoro generation: a 24 kHz, 5.66-second WAV. All 10 GitHub asset sizes and SHA256 digests matched. Anonymous downloads of the EXE, manifest and helpers matched the built files; all four archive URLs were reachable. Automatic approval review blocked an additional test that would execute the GitHub-downloaded EXE, without a detailed reason. The identical locally built EXEs passed the native tests described above.

The unchanged clean Linux image previously passed offline CPU synthesis for all nine Kokoro language groups, CPU and RTX 3060 API/CLI generation, and relocation with byte-for-byte preservation of saved audio. The existing regression suites passed 74 Python tests and 10 browser tests. Windows 10 build 19045 was the test host; Windows 11 and inference across all optional engines were not separately tested. The native same-folder duplicate-session scenario remains unverified; the backend duplicate guard was tested separately.

Source is MIT licensed; bundled components retain their own licenses, notices, inventories, and corresponding Ubuntu sources. Private development disks, voices, jobs, credentials, and browser profiles are excluded.
