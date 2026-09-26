# Portable TTS Server V2: storage and moving

The complete release runs from a writable folder on a local Windows drive. Manual examples use `E:\tts_server`; that path is not required. UNC/network shares are unsupported.

## What stays in the folder

| Location | Contents |
| --- | --- |
| `TTSServer.exe` | Clickable launcher with visible setup progress |
| `runtime/launcher` | Self-contained Windows desktop host |
| `runtime/python` | Windows Python for the CLI |
| `runtime/webview2` | Fixed Version browser runtime |
| `runtime/linux-rootfs.tar.gz` | Clean Linux image, dependencies, and offline-ready Kokoro |
| `wsl/ext4.vhdx` | Installed Linux disk, created on first launch |
| `voices`, `projects_output`, `output` | Audio, projects, jobs, logs, and API discovery |
| `secrets` | Your Hugging Face token, if saved |
| `cache/webview2` | Desktop browser profile |

Inside the disk, `/opt/tts_server` holds Linux Python, weights, optional engine dependencies, and caches. These paths stay stable when the folder moves. Windows-visible paths and the source link are refreshed before every launch.

## Host requirements and space

Use Windows x64 with working WSL2 and virtualization. A GPU is optional for Kokoro; GPU inference needs a compatible Windows NVIDIA driver. WSL and drivers are host components. Python, .NET runtime, WebView2, FFmpeg, CUDA user-space libraries, and Kokoro are included.

Allow at least **35 GiB free for initial downloading, extraction, import, and working room**. Once the app works, remove the downloader's `.tts-download` cache to reclaim the space occupied by parts and the joined ZIP. Additional engines may consume tens of gigabytes. The VHD grows as models and caches are added. Use `tts.cmd disk` for actual sizes.

## Start, stop, and move

1. Extract the complete release and run `TTSServer.exe`.
2. Before moving, copying, or backing up, run `Stop-TTSServer.cmd`. It requests API shutdown and terminates only this folder's verified WSL distro.
3. If Stop confirms **the disk is released**, copy the whole folder, including `wsl/ext4.vhdx`, to the destination local drive or PC. If it reports a locked disk, use the transfer command below.
4. Run `TTSServer.exe` there. Packages and installed engines travel with the disk.

Never copy an active VHD. Keep a complete stopped backup before upgrading. Do not merge a fresh release over a running installation.

### Transfer while another WSL app is running

Some WSL versions keep a stopped distro's disk locked until the shared WSL virtual machine exits. Stop detects this condition. To preserve installed engines and user data without interrupting other WSL apps, run from this app's folder:

```powershell
.\Copy-TTSServer.ps1 -Destination 'D:\Portable-TTS-Server-V2'
```

Choose a new destination outside the source folder. Finish or cancel generation and installation first, and keep this app closed until the command finishes. The helper stops this copy, copies its Windows files, and uses WSL's tar export for the Linux state. The original is preserved. Allow enough space for the complete copy, an uncompressed Linux snapshot, and its subsequently imported VHD.

Start the destination normally. It imports `runtime/transfer-rootfs.tar` instead of the clean factory image. Once the new copy works, delete that snapshot to reclaim its space; retain the new `wsl/ext4.vhdx`. A failed transfer is marked incomplete and cannot start. This personal transfer includes saved tokens, voices, and projects; keep it private.

For a manual VHD copy instead, close all WSL applications and run `wsl --shutdown` yourself, then check Stop again before copying. **That command stops every WSL distro**, including Docker and unrelated apps; this app never runs it automatically. Microsoft documents [terminate, export, import, and shutdown](https://learn.microsoft.com/en-us/windows/wsl/basic-commands).

## Registration and containment

Each new folder gets a name `TTS-Server-V2-<12 hexadecimal characters>` derived from its full Windows path. Startup verifies that its WSL BasePath is exactly this folder's `wsl`. It rejects unrelated distributions and escaping paths. The legacy name `linbox-TTS_Server` is accepted only for an existing registration pointing to this folder's disk.

`.\Start-TTSServer.ps1 -VerifyOnly` shows the distro and disk without launching. `-PrepareOnly` imports/registers without opening the GUI. `-Headless` starts the backend; `-BridgePort` and `-GatewayPort` select unused ports for another copy.

WSL stores a registration in the current Windows account. A manual move can leave an old entry pointing to the previous path. The launcher never deletes registrations automatically. **Do not run `wsl --unregister` on a disk you want to keep; it deletes distro data.** The stop command does not unregister anything.

The source checkout has no runtime. See [build instructions](release/BUILD.md). Public images are built from a fresh base; private development disks, tokens, voices, generated audio, and browser profiles are excluded.
