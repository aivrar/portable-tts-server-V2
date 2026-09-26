# Start, stop, and portability

This is the operational loop for Portable TTS Server V2, the linux distro for windows users. The complete release includes its Linux image and app dependencies. A source checkout contains source only; see [build instructions](../release/BUILD.md).

## Before first launch

- Use Windows x64 with WSL2 enabled. `wsl --status` should succeed. Restart Windows if enabling WSL requires it.
- Open `Download-TTSServer.exe` from the release and select a local destination. It downloads, verifies, extracts, and starts the complete app. For offline transfer, download every release part, the manifest, and both extraction helpers, then run the CMD helper.
- Choose a writable local folder. `E:\tts_server` is an example; spaces are supported. UNC shares are unsupported.
- Confirm root-level `TTSServer.exe`, `runtime/launcher/TTSServer.exe`, `runtime/python/python.exe`, `runtime/webview2`, and `runtime/linux-rootfs.tar.gz` exist.
- Allow at least 35 GiB free for the download cache, extraction, import, and working room. Remove `.tts-download` after verifying the app works to recover the space occupied by parts and the joined ZIP.

## Starting

Double-click **TTSServer.exe** in the app folder. A progress window displays first-launch setup and any errors. The WSL setup guide button opens Microsoft's installation instructions. WSL setup can require administrator approval and a reboot. Startup logs are saved in `output/run/startup.log`.

For terminal use, `Start-TTSServer.cmd` is also supported, or run from the extracted folder:

```powershell
.\Start-TTSServer.ps1
```

The wrapper resolves this folder, finds or creates its WSL registration, and verifies that BasePath points to this folder's `wsl`. First launch imports the clean runtime archive. Later launches reuse `wsl/ext4.vhdx`. It refuses to import over other files or use another copy's disk.

Startup refreshes `/opt/tts_server/env.conf` and the source link for the current Windows path. The native launcher starts the bridge; the bridge starts the gateway. Bundled Kokoro needs no package installation. The GUI uses bundled WebView2 and stores its profile in `cache/webview2`.

Expect a window titled **Portable TTS Server V2**, with the TTS icon, six tabs, a Connected badge, and Shutdown. Initial content size is 1440 by 900 pixels; Windows DPI scaling affects its displayed size.

Desktop downloads default to `output/downloads` inside the portable folder. You can choose another export location in the download dialog. A separate browser follows its own download settings.

Kokoro is installed but initially unloaded. In **Server**, choose Kokoro and CPU or an available GPU, then **Spawn Worker**. Use **Testing** to generate with a built-in voice. See the [illustrated quickstart](quickstart.md).

## Verify and use the CLI

```powershell
.\Start-TTSServer.ps1 -VerifyOnly
.\tts.cmd health
.\tts.cmd wait-ready --timeout 60
.\tts.cmd about
.\tts.cmd diagnose
```

`-VerifyOnly` returns AppRoot, Distro, BasePath, VhdPath, and Ready. It does not import or start anything; a fresh unregistered copy reports that first launch is needed. `-PrepareOnly` registers/imports without opening the GUI.

The release includes Windows Python; `tts.cmd` needs no separate installation. It discovers the server and its `X-TTS-API-Token` in `output/run/registry/tts_server.json`. It does not start the app. Treat the registry and `output/run/api_token` as private.

Default bridge: `http://127.0.0.1:9300`; gateway: port 8300. For another copy, choose unused ports:

```powershell
.\Start-TTSServer.ps1 -BridgePort 19300 -GatewayPort 18300
```

Add `-Headless` for CLI/API-only use. Discovery records the selected ports; use that copy's `tts.cmd`.

## Stopping

Click **Shutdown**. Workers unload, discovery is removed, the bridge stops supervising the gateway, and the window closes. Closing the native window also requests graceful shutdown. If the API request fails, the GUI reports the failure and resumes connection polling.

Before moving, copying, backing up, or unplugging the drive, run:

```powershell
.\Stop-TTSServer.cmd
```

This requests API shutdown and then terminates only this folder's verified WSL distro. It preserves registration and disk. Finish or cancel active generation/installation first. `wsl --shutdown` affects every WSL distro and is not this app's stop command.

## Moving the complete app

Once Stop confirms **the disk is released**, copy the entire folder to another local path or Windows PC. Include `wsl/ext4.vhdx`: it contains dependencies, weights, and dictionaries. Include voices, projects, output, secrets, and runtime files when preserving your working installation. Start at the new location.

If Stop warns that Windows still holds the disk, use the export-based transfer helper:

```powershell
.\Copy-TTSServer.ps1 -Destination 'D:\Portable-TTS-Server-V2'
```

The destination must not exist and must be outside the source. Keep this app closed until copying finishes. The helper preserves the original, copies Windows data, and exports Linux state without stopping unrelated WSL apps. First launch at the destination imports `runtime/transfer-rootfs.tar`; after checking the new copy, delete that tar to reclaim space. It includes your private data. Allow room for both the snapshot and imported disk. See [transfer details and manual shutdown alternative](../PORTABILITY.md#transfer-while-another-wsl-app-is-running).

New copies use `TTS-Server-V2-<path hash>`. The legacy name `linbox-TTS_Server` works only for an existing registration pointing to this exact folder's disk. The destination must have WSL2 and any GPU driver you need.

A move may leave an unused Windows registration at the old path. The wrapper never unregisters another distro. Avoid `wsl --unregister` on a disk you want to keep: it deletes distro data. The new copy does not need the old entry.

## Maintenance

Optional engines install under `/opt/tts_server` in this copy's VHD. They need internet during installation; Edge also needs internet for generation. Gated models need your Hugging Face permissions. Transcription and subtitle recognition may download optional Whisper weights.

Maintainers run `server/setup.sh` inside a verified TTS build distro. It provisions dependencies, checks containment, logs in `output/run`, and serializes setup with a lock. Normal users launch the prebuilt runtime.

See [requirements](identity-and-requirements.md), [storage](storage-layout-and-ports.md), [troubleshooting](troubleshooting.md), and [portability](../PORTABILITY.md).
