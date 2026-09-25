"""Sanitize only a verified fresh build distro before exporting its runtime."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "server"))
from portable_runtime import validate_app_binding, distro_name

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--confirm-clean-build", action="store_true", required=True)
parser.parse_args()
validate_app_binding(root)
if distro_name() == "linbox-TTS_Server":
    raise SystemExit("Never export the private legacy installation as a public runtime")
marker = Path("/opt/tts_server/.clean-release-build")
if not marker.is_file():
    raise SystemExit("Fresh-build provenance marker is missing")

# WSL may create empty credential directories. Empty directories carry no state.
for secret in ("/root/.ssh", "/root/.aws", "/root/.config/huggingface/token",
               "/opt/tts_server/cache/home/.cache/huggingface/token", "/opt/tts_server/hf_token"):
    path = Path(secret)
    if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
        path.rmdir()
    elif path.exists() or path.is_symlink():
        raise SystemExit(f"Unexpected private state in clean build: {secret}")

# These are download caches and transient build data, never linguistic assets.
for name in ("/root/.cache/pip", "/opt/tts_server/cache/pip", "/var/lib/apt/lists"):
    target = Path(name)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
subprocess.run(["apt-get", "clean"], check=True)
for name in ("/root/.bash_history", "/opt/tts_server/env.conf"):
    Path(name).unlink(missing_ok=True)
link = Path("/opt/tts_server/server")
if link.is_symlink():
    link.unlink()
Path("/etc/machine-id").write_text("")
Path("/etc/wsl.conf").write_text("[user]\ndefault=root\n[automount]\nenabled=true\n[interop]\nenabled=true\nappendWindowsPath=true\n")
for parent in (Path("/var/log"), Path("/tmp")):
    for item in parent.rglob("*"):
        if item.is_file() and not item.is_symlink():
            item.write_bytes(b"")
print("Fresh runtime sanitized; linguistic models and dictionaries retained.")
