# Building a portable release

Build on Windows x64 with Git, Python 3.11+, .NET 8 SDK, Visual Studio 2022 Build Tools (Desktop development with C++ and a Windows SDK), working WSL2, internet, and at least 80 GiB free. Release users do not need these build tools.

## Fresh build

```powershell
python tools/build_portable.py
```

The builder copies the Git source candidate list to `output/portable-build/stage/Portable-TTS-Server-V2`, verifies official runtime downloads against `runtime-sources.json`, publishes the launcher, and imports fresh Ubuntu into a separate staging distro. It never uses the private development disk.

It provisions shared dependencies, CUDA-capable PyTorch (also usable on CPU), Kokoro, pronunciation dependencies, and UniDic. It runs warm synthesis, then repeats all nine language groups with Python network connections blocked. Version/license inventories go in `runtime/licenses`.

Inspect failed builds through `output/portable-build/state.json`. The builder refuses to overwrite an existing disk. Resolve the failed step in that isolated distro or start from a fresh checkout.

## Validate and export

1. Run backend and browser regression suites.
2. Start the staging native launcher; generate real Kokoro audio on CPU and GPU if available.
3. Verify the icon, bundled browser, shutdown, CLI discovery, and selected ports.
4. Export after confirming the fresh-build marker and registered backing disk:

```powershell
python tools/build_portable.py --export-only
python tools/build_portable.py --sources-only
python tools/package_portable.py
```

Export removes temporary build paths, machine identity, logs, and package download caches. It preserves pronunciation data and dependencies. Startup recreates folder-specific paths.

Packaging includes source candidates, the root `TTSServer.exe`, and `runtime/`, excluding the staging VHD and user state. It splits the archive into 1.75 GiB parts under `releases/v<version>`, with a manifest, file inventory, SHA256SUMS, extraction helpers, and `Download-TTSServer.exe`. Each part is below GitHub's 2 GiB asset limit.

For launcher-only changes against an existing verified clean runtime, run `python tools/build_portable.py --sync-only`, then `powershell -NoProfile -ExecutionPolicy Bypass -File tools/Build-Launchers.ps1`, and package again. The native C bootstrap and downloader use the static C runtime. The downloader embeds the version-matched extraction script. Never mix the downloader, manifest, and parts from different versions.

## Release acceptance

Use the extraction helper on the actual parts to extract into a new folder with spaces. Import the bundled image, verify offline CPU speech, and run the native launcher. Stop and relocate the installed folder, or use Copy-TTSServer.ps1 if WSL retains the VHD handle. Verify discovery, old job playback/editing, and new generation at the new path. Check the package contains no private development state. Personal transfer snapshots must never be used as public release inputs.

Keep package licenses and corresponding source materials; see [third-party notices](../THIRD_PARTY_NOTICES.md). Sources and inventories identify the build, but apt/transitive Python repositories can change, so rebuilds are not promised to be byte-identical.

The bundled Fixed Version browser and self-contained .NET runtime do not update through a system installation. Refresh their pinned versions, matching notices, and checksums when maintaining releases, then repeat launcher and portability checks.

Upload only final manifest assets, helpers, inventories, checksums, and corresponding sources. Never upload a working VHD, API tokens, personal voices, or model credentials.
