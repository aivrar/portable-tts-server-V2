"""Collect exact Ubuntu corresponding source archives for redistribution.

Run in the clean build distro after export. Sources go beside the exported
runtime, not into the installed VHD. No source trees are compiled or executed.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--retry-only", action="store_true")
args = parser.parse_args()

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "server"))
from portable_runtime import validate_app_binding, distro_name
validate_app_binding(root)
if distro_name() == "linbox-TTS_Server" or not Path("/opt/tts_server/.clean-release-build").is_file():
    raise SystemExit("Use only the clean release build distro")
config = Path("/etc/apt/sources.list.d/ubuntu.sources")
text = config.read_text()
if "Types: deb\n" in text:
    config.write_text(text.replace("Types: deb\n", "Types: deb deb-src\n"))
if not args.retry_only:
    subprocess.run(["apt-get", "update", "-qq"], check=True)
inventory = root / "runtime/licenses/linux-packages.tsv"
specs = sorted({tuple(line.split("\t")[2:4]) for line in inventory.read_text().splitlines()})
destination = root / "runtime/sources/ubuntu"
destination.mkdir(parents=True, exist_ok=True)
if args.retry_only:
    specs = [(item["package"], item["version"]) for item in json.loads((destination / "failures.json").read_text())]

def archived_source(package, version, work):
    # Old packages in an official base image can have left current apt indexes.
    # Launchpad preserves the original signed source publication and its files.
    entries = []
    for status in ("Superseded", "Published", "Deleted"):
        query = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": package,
                                       "version": version, "exact_match": "true", "status": status})
        with urllib.request.urlopen("https://api.launchpad.net/1.0/ubuntu/+archive/primary?" + query, timeout=60) as response:
            entries = json.load(response)["entries"]
        if entries:
            break
    if not entries:
        raise RuntimeError("No original Ubuntu source publication found")
    with urllib.request.urlopen(entries[0]["self_link"] + "?ws.op=sourceFileUrls", timeout=60) as response:
        urls = json.load(response)
    for url in urls:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[1])
        if Path(name).name != name:
            raise RuntimeError("Invalid source archive filename")
        if not (work / name).exists():
            with urllib.request.urlopen(url, timeout=120) as source, (work / (name + ".partial")).open("wb") as output:
                while data := source.read(1024 * 1024):
                    output.write(data)
            (work / (name + ".partial")).replace(work / name)
    # Verify the archives against the checksums recorded in the original DSC.
    dsc = next(work.glob("*.dsc"))
    checks = dsc.read_text().split("Checksums-Sha256:\n", 1)[1]
    for line in checks.splitlines():
        if not line.startswith(" "):
            break
        digest, size, name = line.split()
        file = work / name
        with file.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest or file.stat().st_size != int(size):
            raise RuntimeError("Original source checksum mismatch: " + name)
    (work / "publication.json").write_text(json.dumps({"publication": entries[0]["self_link"], "urls": urls}, indent=2))

def fetch(spec):
    package, version = spec
    work = destination / package
    work.mkdir(exist_ok=True)
    result = subprocess.run(["apt-get", "source", "--download-only", "--only-source", f"{package}={version}"],
                            cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        try:
            archived_source(package, version, work)
            return spec, 0, "Recovered original source from Ubuntu Launchpad"
        except Exception as error:
            return spec, 1, result.stdout + "\nLaunchpad: " + str(error)
    return spec, result.returncode, result.stdout

failures = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for future in as_completed([pool.submit(fetch, spec) for spec in specs]):
        spec, code, output = future.result()
        if code:
            failures.append({"package": spec[0], "version": spec[1], "error": output})
            print("SOURCE FAILED:", *spec, flush=True)
        else:
            print("Source ready:", *spec, flush=True)
(destination / "failures.json").write_text(json.dumps(failures, indent=2))
files = []
for path in sorted(destination.rglob("*")):
    if path.is_file():
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": str(path.relative_to(destination)), "bytes": path.stat().st_size, "sha256": digest})
(root / "runtime/licenses/ubuntu-sources.json").write_text(json.dumps(files, indent=2))
if failures:
    raise SystemExit(f"{len(failures)} exact source packages unavailable; resolve before redistribution")
print(f"Collected {len(specs)} exact source packages.")
