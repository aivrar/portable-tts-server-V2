"""Exercise a running portable copy using only its bundled Windows Python/CLI."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import wave

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--device", default="cpu")
parser.add_argument("--bridge-port", type=int, default=19300)
parser.add_argument("--gateway-port", type=int, default=18300)
parser.add_argument("--verify-moved", action="store_true")
args = parser.parse_args()
root = args.root.resolve()
python = root / "runtime/python/python.exe"

def cli(*values):
    result = subprocess.run([str(python), str(root / "tts.py"), *values], cwd=root,
                            capture_output=True, text=True, timeout=360)
    if result.returncode:
        raise RuntimeError(f"CLI {values[0]} failed: {result.stderr[-4000:]}")
    return result.stdout

cli("wait-ready", "--timeout", "120")
registry = json.loads((root / "output/run/registry/tts_server.json").read_text())
base = registry["endpoints"]["api"]
assert base == f"http://127.0.0.1:{args.bridge_port}", base
headers = {registry["auth"]["header"]: registry["auth"]["token"]}
with urllib.request.urlopen(urllib.request.Request(base + "/api/config", headers=headers), timeout=30) as response:
    config = json.load(response)
assert config["bridge_port"] == args.bridge_port
assert config["gateway_port"] == args.gateway_port
assert registry["extra"]["wsl_distro"].startswith("TTS-Server-V2-")
print("Discovery and custom ports verified.", flush=True)
if args.verify_moved:
    evidence = root / "output/portable-checks/api-cpu.json"
    previous = json.loads(evidence.read_text())
    job = json.loads(cli("jobs", "get", previous["job_id"], "--json"))
    assert job["job_dir"].startswith(config["output_dir"] + "/jobs/"), job["job_dir"]
    destination = root / "output/portable-checks/moved-original.wav"
    cli("jobs", "output", previous["job_id"], "--out", str(destination))
    with destination.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == previous["sha256"]
    edit = json.loads(cli("audio", "render", "final:" + previous["job_id"],
                         "--effect", "gain:db=1", "--output-name", "moved_edit", "--json"))
    (root / "output/portable-checks/moved-job.json").write_text(json.dumps({"job_id": previous["job_id"],
        "job_dir": job["job_dir"], "audio_hash_verified": True, "edit": edit}, indent=2))
    print("Existing job audio, rebound metadata, and a new edit passed after relocation.", flush=True)
print(cli("model", "load", "kokoro", "--device", args.device), flush=True)
destination = root / "output/portable-checks" / ("api-" + args.device.replace(":", "-") + ".wav")
destination.parent.mkdir(parents=True, exist_ok=True)
started = time.monotonic()
generated = json.loads(cli("tts", "kokoro", "--text", "This portable speech studio is ready. Your voice stays with your projects.",
    "--voice", "af_heart", "--device", args.device, "--out", str(destination), "--request-timeout", "300", "--json"))
elapsed = round(time.monotonic() - started, 2)
with wave.open(str(destination), "rb") as audio:
    duration = audio.getnframes() / audio.getframerate()
    assert duration > 1 and any(audio.readframes(audio.getnframes())), "Empty audio output"
    result = {"device": args.device, "seconds": round(duration, 3), "elapsed": elapsed,
              "job_id": generated["job_id"], "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
              "sample_rate": audio.getframerate(), "distro": registry["extra"]["wsl_distro"],
              "bridge_port": args.bridge_port, "gateway_port": args.gateway_port}
(destination.with_suffix(".json")).write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
