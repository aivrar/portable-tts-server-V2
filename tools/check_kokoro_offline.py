"""Warm or verify bundled Kokoro language assets using actual CPU synthesis."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "server"))
from config import setup_environment, MODELS_DIR

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--offline", action="store_true")
args = parser.parse_args()
setup_environment()
if args.offline:
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[key] = "1"
    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network access attempted during the offline Kokoro check")
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked

import numpy as np
from kokoro import KModel, KPipeline

model = KModel(config=str(MODELS_DIR / "kokoro/config.json"),
               model=str(MODELS_DIR / "kokoro/kokoro-v1_0.pth"), repo_id="hexgrad/Kokoro-82M").to("cpu")
examples = {
    "af_heart": "Portable speech is ready. This sentence is generated on your own computer.",
    "bf_emma": "A clear voice makes every story easier to follow.",
    "ef_dora": "Esta voz funciona sin una conexi\u00f3n a internet.",
    "ff_siwis": "Cette voix fonctionne sans connexion internet.",
    "hf_alpha": "\u0928\u092e\u0938\u094d\u0924\u0947, \u092f\u0939 \u090f\u0915 \u0906\u0935\u093e\u091c\u093c \u0915\u093e \u092a\u0930\u0940\u0915\u094d\u0937\u0923 \u0939\u0948\u0964",
    "if_sara": "Questa voce funziona senza una connessione internet.",
    "jf_alpha": "\u3053\u3093\u306b\u3061\u306f\u3002\u3053\u308c\u306f\u97f3\u58f0\u306e\u30c6\u30b9\u30c8\u3067\u3059\u3002",
    "pf_dora": "Esta voz funciona sem uma conex\u00e3o com a internet.",
    "zf_xiaobei": "\u4f60\u597d\uff0c\u8fd9\u662f\u4e00\u4e2a\u8bed\u97f3\u6d4b\u8bd5\u3002",
}
results = []
for voice, text in examples.items():
    pipeline = KPipeline(lang_code=voice[0], model=model, repo_id="hexgrad/Kokoro-82M", device="cpu")
    chunks = [np.asarray(audio) for _, _, audio in pipeline(text, voice=str(MODELS_DIR / "kokoro/voices" / f"{voice}.pt"))]
    audio = np.concatenate(chunks)
    assert len(audio) > 2400 and np.isfinite(audio).all() and float(np.max(np.abs(audio))) > 0.001, voice
    results.append({"voice": voice, "seconds": round(len(audio) / 24000, 3), "offline": args.offline})
    print(json.dumps(results[-1]), flush=True)
evidence = root / "output/portable-checks"
evidence.mkdir(parents=True, exist_ok=True)
(evidence / ("kokoro-offline.json" if args.offline else "kokoro-warm.json")).write_text(json.dumps(results, indent=2), encoding="utf-8")
