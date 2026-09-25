"""Remove WSL-generated host identity from an exported tar without extracting it.

WSL can regenerate /etc/hosts while exporting, so checking only the live distro
before export is insufficient. The output is an uncompressed tar for pigz.
"""
import argparse
import io
from pathlib import Path
import tarfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
if args.source.resolve() == args.destination.resolve():
    raise SystemExit("Source and destination must differ")
replacements = {
    "etc/hosts": b"127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n",
    "etc/hostname": b"tts-server\n",
    "etc/machine-id": b"",
    "var/lib/dbus/machine-id": b"",
}
seen = set()
with tarfile.open(args.source, "r|*", bufsize=1024 * 1024) as source, \
     tarfile.open(args.destination, "w|", format=tarfile.PAX_FORMAT, bufsize=1024 * 1024) as output:
    for item in source:
        name = item.name.removeprefix("./").lstrip("/")
        if name in replacements and item.isfile():
            data = replacements[name]
            item.size = len(data)
            # PAX headers may otherwise restore the old size or sparse layout.
            item.pax_headers = {k: v for k, v in item.pax_headers.items() if k != "size" and not k.startswith("GNU.sparse.")}
            output.addfile(item, io.BytesIO(data))
            seen.add(name)
        elif item.isfile() and (name.startswith("root/.ssh/") or name in (
            "root/.bash_history", "root/.aws/credentials", "opt/tts_server/hf_token",
            "opt/tts_server/cache/home/.cache/huggingface/token", "opt/tts_server/env.conf")):
            raise SystemExit("Unexpected private/build state in exported archive: " + name)
        else:
            output.addfile(item, source.extractfile(item) if item.isfile() else None)
if not {"etc/hosts", "etc/hostname", "etc/machine-id"}.issubset(seen):
    raise SystemExit("Expected host-generated files were not found in the export")
print("Export sanitized: host network settings and machine identity replaced.", flush=True)
