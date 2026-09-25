"""Write runtime package and licence inventories inside a clean release build."""
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
destination = root / "runtime/licenses"
destination.mkdir(parents=True, exist_ok=True)
packages = []
for dist in metadata.distributions():
    packages.append({"name": dist.metadata["Name"], "version": dist.version,
                     "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", ""),
                     "home_page": dist.metadata.get("Home-page", ""),
                     "project_urls": dist.metadata.get_all("Project-URL", [])})
(destination / "python-packages.json").write_text(json.dumps(sorted(packages, key=lambda p: p["name"].lower()), indent=2), encoding="utf-8")
for filename, command in [
    ("linux-packages.tsv", ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n"]),
    ("python-freeze.txt", [sys.executable, "-m", "pip", "freeze", "--all"]),
]:
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    (destination / filename).write_text(result.stdout, encoding="utf-8")
(destination / "README.txt").write_text(
    "Linux package copyright and licence texts are retained inside the bundled distro under /usr/share/doc/<package>/copyright.\n"
    "Python package licence texts are retained under /opt/tts_server/venv/lib/python3.12/site-packages, including *.dist-info/licenses.\n"
    "Ubuntu corresponding source packages are indexed by the exact source package/version pairs in linux-packages.tsv.\n"
    "Ubuntu source archive: https://archive.ubuntu.com/ubuntu/pool/\n"
    "Kokoro model repository/revision and native runtime download sources are pinned in release/runtime-sources.json.\n", encoding="utf-8")
