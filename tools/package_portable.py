"""Package source allowlist + clean runtime; split below GitHub's 2 GiB limit."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "output/portable-build"
STATE = json.loads((WORK / "state.json").read_text(encoding="utf-8-sig"))
STAGE = Path(STATE["stage_root"])
VERSION = json.loads((ROOT / "app.json").read_text())["version"]
DEST = ROOT / "releases" / ("v" + VERSION)
DEST.mkdir(parents=True, exist_ok=True)
NAME = f"Portable-TTS-Server-V2-{VERSION}-win-x64.zip"
archive_path = WORK / NAME

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

for relative in ("TTSServer.exe", "runtime/linux-rootfs.tar.gz", "runtime/launcher/TTSServer.exe", "runtime/python/python.exe",
                 "runtime/licenses/linux-packages.tsv", "runtime/licenses/python-packages.json",
                 "runtime/licenses/ubuntu-sources.json"):
    if not (STAGE / relative).is_file():
        raise SystemExit("Missing release input: " + relative)
if json.loads((STAGE / "runtime/sources/ubuntu/failures.json").read_text()):
    raise SystemExit("Resolve missing corresponding source archives before packaging")
if not list((STAGE / "runtime/webview2").rglob("msedgewebview2.exe")):
    raise SystemExit("Bundled WebView2 is missing")
if list((STAGE / "runtime").glob("transfer-*")):
    raise SystemExit("Personal transfer state must never be included in a public release")
sources = set(subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode().split("\0")) - {""}
files = [(ROOT / name, name) for name in sorted(sources) if (ROOT / name).is_file()]
files.append((STAGE / "TTSServer.exe", "TTSServer.exe"))
files += [(path, path.relative_to(STAGE).as_posix()) for path in sorted((STAGE / "runtime").rglob("*")) if path.is_file()]
print(f"Packing {len(files)} files. No installed VHD, user output, voices, profiles, or secrets.", flush=True)
inventory = []
with zipfile.ZipFile(archive_path.with_suffix(".partial"), "w", allowZip64=True, compresslevel=6) as archive:
    for path, relative in files:
        if path.is_symlink():
            raise RuntimeError(f"Unexpected release symlink: {path}")
        method = zipfile.ZIP_STORED if relative.endswith((".gz", ".xz", ".bz2", ".zip", ".zst")) else zipfile.ZIP_DEFLATED
        archive.write(path, "Portable-TTS-Server-V2/" + relative, compress_type=method)
        inventory.append({"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)})
archive_path.with_suffix(".partial").replace(archive_path)
print("Archive built:", archive_path, archive_path.stat().st_size, flush=True)
manifest = {"version": VERSION, "archive": NAME, "archive_bytes": archive_path.stat().st_size,
            "archive_sha256": digest(archive_path), "folder": "Portable-TTS-Server-V2", "parts": []}
part_size = 1792 * 1024 * 1024
with archive_path.open("rb") as source:
    number = 1
    while source.tell() < archive_path.stat().st_size:
        name = NAME + f".part{number:03}"
        destination = DEST / name
        with destination.open("wb") as output:
            remaining = part_size
            while remaining:
                chunk = source.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        manifest["parts"].append({"name": name, "bytes": destination.stat().st_size, "sha256": digest(destination)})
        print("Part ready:", name, flush=True)
        number += 1
(DEST / "portable-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(DEST / "file-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
for name in ("Extract-Portable-TTS.ps1", "Extract-Portable-TTS.cmd"):
    shutil.copy2(ROOT / "release" / name, DEST / name)
shutil.copy2(WORK / "native/Download-TTSServer.exe", DEST / "Download-TTSServer.exe")
asset_names = [part["name"] for part in manifest["parts"]] + [
    "portable-manifest.json", "file-inventory.json", "Extract-Portable-TTS.ps1", "Extract-Portable-TTS.cmd", "Download-TTSServer.exe"]
(DEST / "SHA256SUMS.txt").write_text("".join(f"{digest(DEST / name)}  {name}\n" for name in sorted(asset_names)))
print("Release assets:", DEST)
