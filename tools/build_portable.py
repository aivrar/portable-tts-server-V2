"""Build a fresh Windows/WSL portable runtime; never export the working distro.

Windows build host: Git, Python 3.11+, .NET 8 SDK, WSL2, internet, 80+ GiB free.
Run from a source checkout: python tools/build_portable.py
Use --export-only after inspecting and testing an existing clean build.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "output/portable-build"
STAGE = WORK / "stage/Portable-TTS-Server-V2"
SPEC = json.loads((ROOT / "release/runtime-sources.json").read_text())

def run(*args, capture=False):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout

def sync_sources():
    names = run("git", "-C", ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "-z", capture=True)
    for name in sorted(set(names.split("\0")) - {""}):
        source = ROOT / name
        if source.is_file():
            target = STAGE / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

def download(entry):
    destination = WORK / "downloads" / entry["filename"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + ".partial")
        print("Downloading", entry["filename"], flush=True)
        urllib.request.urlretrieve(entry["url"], partial)
        partial.replace(destination)
    if hashlib.file_digest(destination.open("rb"), "sha256").hexdigest() != entry["sha256"]:
        raise RuntimeError(f"Download checksum mismatch: {destination}")
    return destination

def native_notices(runtime):
    destination = runtime / "licenses"
    destination.mkdir(parents=True, exist_ok=True)
    for name, url in {
        "dotnet-LICENSE.txt": "https://raw.githubusercontent.com/dotnet/runtime/v8.0.26/LICENSE.TXT",
        "dotnet-THIRD-PARTY-NOTICES.txt": "https://raw.githubusercontent.com/dotnet/runtime/v8.0.26/THIRD-PARTY-NOTICES.TXT",
        "winforms-LICENSE.txt": "https://raw.githubusercontent.com/dotnet/winforms/v8.0.26/LICENSE.TXT",
        "winforms-THIRD-PARTY-NOTICES.txt": "https://raw.githubusercontent.com/dotnet/winforms/v8.0.26/THIRD-PARTY-NOTICES.TXT",
        "wpf-LICENSE.txt": "https://raw.githubusercontent.com/dotnet/wpf/v8.0.26/LICENSE.TXT",
        "wpf-THIRD-PARTY-NOTICES.txt": "https://raw.githubusercontent.com/dotnet/wpf/v8.0.26/THIRD-PARTY-NOTICES.TXT",
        "Apache-2.0.txt": "https://www.apache.org/licenses/LICENSE-2.0.txt",
    }.items():
        urllib.request.urlretrieve(url, destination / name)
    shutil.copy2(Path.home() / ".nuget/packages/microsoft.web.webview2" / SPEC["webview2_sdk"] / "LICENSE.txt",
                 destination / "WebView2-SDK-LICENSE.txt")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args()
    STAGE.mkdir(parents=True, exist_ok=True)
    sync_sources()
    if args.sync_only:
        return
    if args.sources_only:
        state = json.loads((WORK / "state.json").read_text(encoding="utf-8-sig"))
        if Path(state["stage_root"]).resolve() != STAGE.resolve():
            raise RuntimeError("Build state points outside this staging directory")
        linux = run("wsl", "-d", state["distro"], "--exec", "wslpath", "-u", STAGE, capture=True).strip()
        run("wsl", "-d", state["distro"], "--exec", "/usr/bin/python3", linux + "/tools/collect_runtime_sources.py")
        return
    if args.export_only:
        state = json.loads((WORK / "state.json").read_text(encoding="utf-8-sig"))
        if Path(state["stage_root"]).resolve() != STAGE.resolve():
            raise RuntimeError("Build state points outside this staging directory")
        distro = state["distro"]
        linux = run("wsl", "-d", distro, "--exec", "wslpath", "-u", STAGE, capture=True).strip()
        # Python's binding/provenance checks run before any cleanup or export.
        run("wsl", "-d", distro, "--exec", "/usr/bin/python3", linux + "/tools/prepare_release_runtime.py", "--confirm-clean-build")
        run("wsl", "--terminate", distro)
        raw = WORK / "linux-rootfs.tar"
        run("wsl", "--export", distro, raw)
        sanitized = WORK / "linux-rootfs-sanitized.tar"
        run(sys.executable, ROOT / "tools/sanitize_rootfs_archive.py", raw, sanitized)
        sanitized.replace(raw)
        # Compress outside the exported filesystem. pigz exists in the build distro.
        raw_linux = run("wsl", "-d", distro, "--exec", "wslpath", "-u", raw, capture=True).strip()
        run("wsl", "-d", distro, "--exec", "pigz", "-6", "-p", "8", "-f", raw_linux)
        shutil.move(str(raw) + ".gz", STAGE / "runtime/linux-rootfs.tar.gz")
        print("Runtime exported:", STAGE / "runtime/linux-rootfs.tar.gz")
        return
    if (STAGE / "wsl/ext4.vhdx").exists():
        raise RuntimeError("Build disk already exists. Use --sync-only / --export-only, or choose a fresh checkout.")
    downloads = {key: download(entry) for key, entry in SPEC["downloads"].items()}
    runtime = STAGE / "runtime"
    for name in ("python", "webview2", "launcher"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(downloads["python"]) as archive:
        archive.extractall(runtime / "python")
    run("expand.exe", downloads["webview2"], "-F:*", runtime / "webview2")
    run("dotnet", "publish", ROOT / "launcher/TTSServer.csproj", "-c", "Release", "-r", "win-x64",
        "--self-contained", "true", "-o", runtime / "launcher")
    native_notices(runtime)
    distro = "TTS-Server-V2-" + hashlib.sha256(str(STAGE).lower().encode()).hexdigest()[:12]
    existing = run("powershell", "-NoProfile", "-Command",
                   "Get-ChildItem HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss | ForEach-Object { (Get-ItemProperty $_.PSPath).DistributionName }", capture=True)
    if distro in existing.splitlines():
        raise RuntimeError("The proposed fresh build name is already registered")
    (STAGE / "wsl").mkdir()
    run("wsl", "--import", distro, STAGE / "wsl", downloads["ubuntu"], "--version", "2")
    (WORK / "state.json").write_text(json.dumps({"distro": distro, "stage_root": str(STAGE), "version": SPEC["version"]}, indent=2))
    linux = run("wsl", "-d", distro, "--exec", "wslpath", "-u", STAGE, capture=True).strip()
    run("wsl", "-d", distro, "--exec", "bash", "-c",
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates python3 python3-venv python3-dev build-essential ffmpeg espeak-ng sox git libsndfile1 libsndfile1-dev portaudio19-dev rubberband-cli curl unzip locales pigz")
    run("wsl", "-d", distro, "--exec", "mkdir", "-p", "/opt/tts_server")
    run("wsl", "-d", distro, "--exec", "touch", "/opt/tts_server/.clean-release-build")
    run("wsl", "-d", distro, "--exec", "env", "TTS_BUNDLE_CUDA=1", "bash", linux + "/server/setup.sh")
    run("wsl", "-d", distro, "--exec", "bash", linux + "/server/install_model.sh", "kokoro")
    python = "/opt/tts_server/venv/bin/python3"
    run("wsl", "-d", distro, "--exec", python, "-m", "pip", "install", "misaki[en,ja,zh]==" + SPEC["kokoro"]["misaki"], SPEC["kokoro"]["spacy_model"])
    run("wsl", "-d", distro, "--exec", python, "-m", "unidic", "download")
    for flags in ([], ["--offline"]):
        run("wsl", "-d", distro, "--exec", python, linux + "/tools/check_kokoro_offline.py", *flags)
    run("wsl", "-d", distro, "--exec", python, linux + "/tools/runtime_inventory.py")
    print("Clean build ready for inspection and launcher/API tests. Then use --export-only.")

if __name__ == "__main__":
    main()
