"""Exercise the Windows extractor against valid, damaged and unsafe tiny archives."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "app.json").read_text())["version"]


def fixture(root, entries):
    root.mkdir(parents=True)
    shutil.copy2(ROOT / "release/Extract-Portable-TTS.ps1", root)
    name = f"Portable-TTS-Server-V2-{VERSION}-win-x64.zip"
    archive = root / name
    with zipfile.ZipFile(archive, "w") as out:
        for path, data in entries.items():
            out.writestr(path, data)
    data = archive.read_bytes()
    part = archive.with_name(name + ".part001")
    part.write_bytes(data)
    checksum = hashlib.sha256(data).hexdigest()
    (root / "portable-manifest.json").write_text(json.dumps({
        "version": VERSION, "folder": "Portable-TTS-Server-V2", "archive": name,
        "archive_bytes": len(data), "archive_sha256": checksum,
        "parts": [{"name": part.name, "bytes": len(data), "sha256": checksum}],
    }))
    archive.unlink()
    return part


def extract(root, destination):
    return subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(root / "Extract-Portable-TTS.ps1"), "-Destination", str(destination),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    tests = ROOT / "output/portable-build/extraction-checks"
    tests.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=tests))
    normal = {"Portable-TTS-Server-V2/TTSServer.exe": b"fixture",
              "Portable-TTS-Server-V2/folder with spaces/file.txt": b"preserved content"}
    passed = []
    good = run / "good cache"
    fixture(good, normal)
    destination = run / "App's destination with spaces"
    result = extract(good, destination)
    assert result.returncode == 0, result.stderr
    assert (destination / "Portable-TTS-Server-V2/folder with spaces/file.txt").read_bytes() == b"preserved content"
    passed.append("valid extraction with spaces and apostrophe")
    result = extract(good, destination)
    assert result.returncode != 0 and "Destination already exists" in result.stderr
    assert (destination / "Portable-TTS-Server-V2/TTSServer.exe").read_bytes() == b"fixture"
    passed.append("existing destination preserved")
    damaged = run / "damaged"
    part = fixture(damaged, normal)
    part.write_bytes(b"corrupt" + part.read_bytes()[7:])
    result = extract(damaged, run / "damaged destination")
    assert result.returncode != 0 and "Checksum failed" in result.stderr
    assert not (run / "damaged destination/Portable-TTS-Server-V2").exists()
    passed.append("damaged part refused before extraction")
    unsafe = run / "unsafe"
    fixture(unsafe, {**normal, "Portable-TTS-Server-V2/../../escape.txt": b"must not escape"})
    result = extract(unsafe, run / "unsafe destination")
    assert result.returncode != 0 and "escapes the app folder" in result.stderr
    assert not (run / "escape.txt").exists()
    passed.append("archive traversal refused")
    missing = run / "missing launcher"
    fixture(missing, {"Portable-TTS-Server-V2/readme.txt": b"incomplete"})
    result = extract(missing, run / "missing destination")
    assert result.returncode != 0 and "missing TTSServer.exe" in result.stderr
    passed.append("incomplete release refused")
    report = {"passed": passed, "fixtures": str(run)}
    (tests / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
