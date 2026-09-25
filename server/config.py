"""
TTS Server Configuration

Central configuration for the Linux-based TTS server.
Reads paths from /opt/tts_server/env.conf (written by setup.sh).
All models share one base venv with thin override directories for conflicts.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from portable_runtime import validate_app_binding

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
EXPECTED_APP_DIR = BASE_DIR.parent
LINUX_STATE_ROOT = Path("/opt/tts_server")

# Read environment config written by setup.sh
_ENV_CONF = Path("/opt/tts_server/env.conf")


def _read_env_conf() -> dict:
    """Read key=value pairs from env.conf (values may be quoted)."""
    conf = {}
    if _ENV_CONF.exists():
        for line in _ENV_CONF.read_text().strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                # configure_runtime.py wraps literal values in one outer quote pair.
                # Strip exactly that pair without shell evaluation.
                # Using shlex here was fragile: a value containing a lone
                # apostrophe (e.g. /home/o'brien/data) raised ValueError, and
                # it silently collapsed embedded quotes/spaces.
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                conf[k.strip()] = v
    return conf


_conf = _read_env_conf()

# Warn if env.conf exists but is missing critical keys
if _ENV_CONF.exists() and _conf:
    _REQUIRED_CONF_KEYS = ("VENV_DIR", "MODELS_DIR", "APP_DIR")
    _missing_keys = [k for k in _REQUIRED_CONF_KEYS if k not in _conf]
    if _missing_keys:
        print(f"WARNING: env.conf missing keys: {', '.join(_missing_keys)} — using defaults")

# Directories inside the Linux filesystem (fast I/O — ext4 inside WSL VHDX).
# Models, caches, pip wheels, Python venv, override packages, and cloned
# upstream repos all live here. None of this should ever land on /mnt/<drive>.
VENV_DIR = Path(_conf.get("VENV_DIR", "/opt/tts_server/venv"))
OVERRIDES_DIR = Path(_conf.get("OVERRIDES_DIR", "/opt/tts_server/overrides"))
REPOS_DIR = Path(_conf.get("REPOS_DIR", "/opt/tts_server/repos"))
MODELS_DIR = Path(_conf.get("MODELS_DIR", "/opt/tts_server/data"))
CACHE_DIR = Path(_conf.get("CACHE_DIR", "/opt/tts_server/cache"))

# User-facing artifacts on the Windows-visible app folder (via /mnt/<drive>/).
# These stay on the host so users can browse uploaded voices, finished outputs,
# and named project folders directly from Explorer.
_app_dir = _conf.get("APP_DIR", str(BASE_DIR.parent))
APP_DIR = Path(_app_dir)
OUTPUT_DIR = Path(_conf.get("OUTPUT_DIR", f"{_app_dir}/output"))
# Note: env.conf uses VOICES_DIR (plural), Python var is VOICE_DIR (singular)
VOICE_DIR = Path(_conf.get("VOICES_DIR", f"{_app_dir}/voices"))
PROJECTS_OUTPUT = Path(_conf.get("PROJECTS_DIR", f"{_app_dir}/projects_output"))
# Derive from the resolved OUTPUT_DIR (not raw _app_dir) so a single OUTPUT_DIR
# override propagates here, matching how JOBS_DIR/WORKER_LOG_DIR are built.
RUN_DIR = Path(_conf.get("RUN_DIR", str(OUTPUT_DIR / "run")))
SECRETS_DIR = Path(_conf.get("SECRETS_DIR", f"{_app_dir}/secrets"))
DISCOVERY_DIR = Path(_conf.get("DISCOVERY_DIR", str(RUN_DIR / "registry")))
APP_HOME_DIR = CACHE_DIR / "home"
XDG_CACHE_DIR = CACHE_DIR / "xdg-cache"
XDG_DATA_DIR = CACHE_DIR / "xdg-data"
XDG_CONFIG_DIR = CACHE_DIR / "xdg-config"
XDG_STATE_DIR = CACHE_DIR / "xdg-state"
TMP_DIR = Path(_conf.get("TMPDIR", str(CACHE_DIR / "tmp")))
PIP_CACHE_DIR = CACHE_DIR / "pip"
NUMBA_CACHE_DIR = CACHE_DIR / "numba"
TRITON_CACHE_DIR = CACHE_DIR / "triton"
CUDA_CACHE_DIR = CACHE_DIR / "cuda"
TORCHINDUCTOR_CACHE_DIR = CACHE_DIR / "torchinductor"
MPLCONFIG_DIR = CACHE_DIR / "matplotlib"

# Never inherit WSL's Windows-appended PATH. If a required Linux dependency is
# missing, fail diagnostics clearly instead of silently running a tool from C:
# or an unrelated Linbox app. /usr/lib/wsl/lib contains nvidia-smi in WSL2.
PORTABLE_EXEC_PATH = ":".join((
    str(VENV_DIR / "bin"),
    "/usr/local/cuda/bin",
    "/usr/lib/wsl/lib",
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
))

# Python executable inside the shared venv
PYTHON_PATH = VENV_DIR / "bin" / "python3"

# HuggingFace token storage
LEGACY_HF_TOKEN_FILE = Path("/opt/tts_server/hf_token")
HF_TOKEN_FILE = Path(_conf.get("HF_TOKEN_FILE", str(SECRETS_DIR / "hf_token")))


def _is_wsl_runtime() -> bool:
    """Return True only inside WSL; native development imports remain usable."""
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except (OSError, UnicodeDecodeError):
        return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def validate_runtime_location() -> None:
    """Validate this movable app's dedicated WSL disk before any writes."""
    if not _is_wsl_runtime():
        return

    errors: list[str] = []
    try:
        validate_app_binding(EXPECTED_APP_DIR)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        errors.append(f"host registration verification failed: {exc}")

    if APP_DIR.resolve(strict=False) != EXPECTED_APP_DIR.resolve(strict=False):
        errors.append(f"expected app directory {EXPECTED_APP_DIR}, got {APP_DIR}")

    host_paths = {
        "BASE_DIR": BASE_DIR,
        "OUTPUT_DIR": OUTPUT_DIR,
        "VOICE_DIR": VOICE_DIR,
        "PROJECTS_OUTPUT": PROJECTS_OUTPUT,
        "RUN_DIR": RUN_DIR,
        "SECRETS_DIR": SECRETS_DIR,
        "DISCOVERY_DIR": DISCOVERY_DIR,
        "HF_TOKEN_FILE": HF_TOKEN_FILE,
    }
    for name, path in host_paths.items():
        if not _is_within(path, EXPECTED_APP_DIR):
            errors.append(f"{name} escapes {EXPECTED_APP_DIR}: {path}")

    linux_paths = {
        "VENV_DIR": VENV_DIR,
        "OVERRIDES_DIR": OVERRIDES_DIR,
        "REPOS_DIR": REPOS_DIR,
        "MODELS_DIR": MODELS_DIR,
        "CACHE_DIR": CACHE_DIR,
        "TMP_DIR": TMP_DIR,
        "LEGACY_HF_TOKEN_FILE": LEGACY_HF_TOKEN_FILE,
    }
    for name, path in linux_paths.items():
        if not _is_within(path, LINUX_STATE_ROOT):
            errors.append(f"{name} escapes {LINUX_STATE_ROOT}: {path}")

    if errors:
        raise RuntimeError("Unsafe TTS Server launch refused: " + "; ".join(errors))


def app_environment() -> dict[str, str]:
    """Return environment variables that keep runtime caches app-owned.

    Every cache location resolves into the WSL ext4 filesystem (under
    /opt/tts_server) so model loads, pip wheels, HuggingFace blobs, Torch
    hub, Coqui caches, and even Python's tempfile module never round-trip
    through 9P/drvfs.
    """
    return {
        "HOME": str(APP_HOME_DIR),
        "XDG_CACHE_HOME": str(XDG_CACHE_DIR),
        "XDG_DATA_HOME": str(XDG_DATA_DIR),
        "XDG_CONFIG_HOME": str(XDG_CONFIG_DIR),
        "XDG_STATE_HOME": str(XDG_STATE_DIR),
        "PATH": PORTABLE_EXEC_PATH,
        "PYTHONNOUSERSITE": "1",
        "PIP_CACHE_DIR": str(PIP_CACHE_DIR),
        "NUMBA_CACHE_DIR": str(NUMBA_CACHE_DIR),
        "TRITON_CACHE_DIR": str(TRITON_CACHE_DIR),
        "CUDA_CACHE_PATH": str(CUDA_CACHE_DIR),
        "TORCHINDUCTOR_CACHE_DIR": str(TORCHINDUCTOR_CACHE_DIR),
        "MPLCONFIGDIR": str(MPLCONFIG_DIR),
        "HF_HOME": str(MODELS_DIR),
        "HF_HUB_CACHE": str(MODELS_DIR / "hub"),
        "HUGGINGFACE_HUB_CACHE": str(MODELS_DIR / "hub"),
        "HF_HUB_DISABLE_XET": os.environ.get("HF_HUB_DISABLE_XET", "1"),
        "HF_DATASETS_CACHE": str(MODELS_DIR / "datasets"),
        "HF_MODULES_CACHE": str(MODELS_DIR / "modules"),
        "TRANSFORMERS_CACHE": str(MODELS_DIR / "hub"),
        "TORCH_HOME": str(MODELS_DIR / "torch"),
        "COQUI_TTS_CACHE": str(MODELS_DIR / "coqui"),
        "TTS_HOME": str(MODELS_DIR / "coqui"),
        "COQUI_TOS_AGREED": "1",
        "TMPDIR": str(TMP_DIR),
        "TTS_SERVER_APP_DIR": str(APP_DIR.resolve()),
        "TTS_SERVER_MODELS_DIR": str(MODELS_DIR),
        "TTS_SERVER_RUN_DIR": str(RUN_DIR),
        "APPHUB_REGISTRY_DIR": str(DISCOVERY_DIR),
    }


def setup_environment():
    """Configure environment variables for HuggingFace, CUDA, tempfile, etc."""
    validate_runtime_location()
    for path in (
        APP_HOME_DIR, XDG_CACHE_DIR, XDG_DATA_DIR, XDG_CONFIG_DIR,
        XDG_STATE_DIR, RUN_DIR, SECRETS_DIR, DISCOVERY_DIR, TMP_DIR,
        PIP_CACHE_DIR, NUMBA_CACHE_DIR, TRITON_CACHE_DIR, CUDA_CACHE_DIR,
        TORCHINDUCTOR_CACHE_DIR, MPLCONFIG_DIR,
        MODELS_DIR / "hub", MODELS_DIR / "datasets",
        MODELS_DIR / "modules", MODELS_DIR / "torch", MODELS_DIR / "coqui",
    ):
        path.mkdir(parents=True, exist_ok=True)

    if LEGACY_HF_TOKEN_FILE.exists() and not HF_TOKEN_FILE.exists():
        try:
            HF_TOKEN_FILE.write_text(LEGACY_HF_TOKEN_FILE.read_text())
            HF_TOKEN_FILE.chmod(0o600)
        except OSError:
            pass

    os.environ.update(app_environment())

    # Re-bind Python's tempfile module to the app-owned tmp dir. Just setting
    # TMPDIR isn't enough — the tempfile module caches its choice on first use.
    tempfile.tempdir = str(TMP_DIR)

    # Unbuffered output for real-time log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"


# ---------------------------------------------------------------------------
# App metadata
# ---------------------------------------------------------------------------

# API Server settings
# Bind to loopback by default — this is a single-user local desktop app and the
# API exposes a bearer token; binding to 0.0.0.0 would expose it to the LAN.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8300
DEFAULT_BRIDGE_PORT = 9300

# Job management
JOBS_DIR = OUTPUT_DIR / "jobs"
MAX_RETRIES = 3
MAX_JOB_AGE_HOURS = 72

# Whisper verification
WHISPER_MODEL_SIZE = "base"
WHISPER_ENABLED = True
WHISPER_DEFAULT_TOLERANCE = 80.0
WHISPER_AVAILABLE_MODELS = {
    "tiny":   {"params": "39M",  "vram": "~1GB",  "speed": "~10x", "note": "Fastest, least accurate"},
    "base":   {"params": "74M",  "vram": "~1GB",  "speed": "~7x",  "note": "Good balance (default)"},
    "small":  {"params": "244M", "vram": "~2GB",  "speed": "~4x",  "note": "Better accuracy"},
    "medium": {"params": "769M", "vram": "~5GB",  "speed": "~2x",  "note": "Near-best accuracy"},
    "large":  {"params": "1550M","vram": "~10GB", "speed": "~1x",  "note": "Best accuracy, slowest"},
}

# Server — max concurrent pipeline threads.
# Each thread holds for an entire job (all chunks + post-processing).
# Since threads are I/O-bound (blocking on worker HTTP), set this high enough
# that loaded models don't block each other.  One per model is the minimum;
# two per model allows overlapping jobs for the same model.
MAX_INFERENCE_WORKERS = 16

# Worker management
WORKER_PORT_MIN = 8101
WORKER_PORT_MAX = 8200
WORKER_HEALTH_INTERVAL = 10
WORKER_STARTUP_TIMEOUT = 120
WORKER_MAX_HEALTH_FAILURES = 3
WORKER_AUTO_SPAWN = True
WORKER_LOG_DIR = OUTPUT_DIR / "logs"
# Auto-detect: default to cuda:0 if GPU available, else cpu.
# Deferred to first access to avoid blocking module import with subprocess.
_worker_default_device: str | None = None

def _detect_default_device() -> str:
    global _worker_default_device
    if _worker_default_device is not None:
        return _worker_default_device
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        if r.returncode == 0:
            _worker_default_device = "cuda:0"
            return _worker_default_device
    except Exception:
        pass
    _worker_default_device = "cpu"
    return _worker_default_device

class _LazyDevice(str):
    """String subclass that defers nvidia-smi detection to first access."""
    def __new__(cls):
        return str.__new__(cls, "")
    def __str__(self):
        return _detect_default_device()
    def __repr__(self):
        return repr(_detect_default_device())
    def __eq__(self, other):
        return _detect_default_device() == other
    def __ne__(self, other):
        return _detect_default_device() != other
    def __hash__(self):
        return hash(_detect_default_device())
    def __bool__(self):
        return bool(_detect_default_device())
    def startswith(self, prefix, *args):
        return _detect_default_device().startswith(prefix, *args)
    def split(self, *args, **kwargs):
        return _detect_default_device().split(*args, **kwargs)
    def __contains__(self, item):
        return item in _detect_default_device()
    def __add__(self, other):
        return _detect_default_device() + other
    def __radd__(self, other):
        return other + _detect_default_device()
    def format(self, *args, **kwargs):
        return _detect_default_device().format(*args, **kwargs)
    def __format__(self, format_spec):
        return format(_detect_default_device(), format_spec)
    # Common str transforms that would otherwise operate on the empty backing
    # string and silently mis-detect the device. (Note: json.dumps and other C
    # consumers read the str subclass's underlying buffer directly and cannot be
    # intercepted here — callers serializing this value must wrap it in str().)
    def lower(self, *args, **kwargs):
        return _detect_default_device().lower(*args, **kwargs)
    def upper(self, *args, **kwargs):
        return _detect_default_device().upper(*args, **kwargs)
    def replace(self, *args, **kwargs):
        return _detect_default_device().replace(*args, **kwargs)
    def strip(self, *args, **kwargs):
        return _detect_default_device().strip(*args, **kwargs)
    def lstrip(self, *args, **kwargs):
        return _detect_default_device().lstrip(*args, **kwargs)
    def rstrip(self, *args, **kwargs):
        return _detect_default_device().rstrip(*args, **kwargs)
    def encode(self, *args, **kwargs):
        return _detect_default_device().encode(*args, **kwargs)
    def __getitem__(self, key):
        return _detect_default_device()[key]
    def __len__(self):
        return len(_detect_default_device())
    def __iter__(self):
        return iter(_detect_default_device())

WORKER_DEFAULT_DEVICE = _LazyDevice()

# Per-model inference timeouts (seconds)
MODEL_INFER_TIMEOUT = {
    "qwen": 180.0, "higgs": 180.0, "vibevoice": 300.0,
    "dia": 180.0, "fish": 180.0,
    "bark": 600.0,       # slowest model — autoregressive 3-stage pipeline
    "xtts": 600.0,       # GPT decoder can be slow on CPU or long text
    "chatterbox": 600.0, # emotion processing adds overhead
    "outetts": 180.0,    # bounded 0.6B HF backend; timeout kills the worker
    "edge": 120.0,       # cloud API — fast but network-dependent
    "vits": 60.0,        # very fast, non-autoregressive
    "speecht5": 120.0,   # lightweight but transformer-based
    "parler": 90.0,      # bounded audio-code generation; timeout kills the worker
    "f5": 600.0,         # diffusion — nfe_step iterations
    "kokoro": 120.0,     # fast lightweight model
    "voxtral": 300.0,    # bounded vLLM generation; timeout kills the worker tree
    "voxcpm2": 300.0,    # diffusion-AR with timesteps loop
    "csm": 300.0,        # llama-style autoregressive
    "orpheus": 300.0,    # llama-style autoregressive + SNAC decode
}
DEFAULT_INFER_TIMEOUT = 300.0

# ---------------------------------------------------------------------------
# Model -> override mapping
# None = uses base venv only, string = override directory name
# ---------------------------------------------------------------------------
MODEL_OVERRIDE_MAP = {
    "xtts": "coqui",
    "bark": "coqui",
    "fish": "fish",
    "kokoro": None,
    "dia": "dia",
    "chatterbox": "chatterbox",
    "f5": "f5",
    "qwen": "qwen",  # tiny bitsandbytes-only override; reuse base transformers/torch
    "vibevoice": "vibevoice",
    "higgs": "higgs",
    "whisper": None,
    "speecht5": None,
    "parler": "parler",
    "outetts": "outetts",
    "vits": "coqui",
    "edge": None,
    "voxtral": "voxtral",
    "voxcpm2": "voxcpm2",
    "csm": None,  # native in the shared Transformers runtime
    "orpheus": "orpheus",
}

# ---------------------------------------------------------------------------
# Model metadata (for UI display and installation)
# ---------------------------------------------------------------------------
MODEL_SETUP = {
    "bark":       {"display": "Bark",            "desc": "Expressive TTS - laughter, music, emotions",       "weights_repo": "erogol/bark",               "weights_dir": "coqui/tts/tts_models--multilingual--multi-dataset--bark", "extra_weights_dirs": ["bark-voices"], "weights_size": "~12GB + voice prompts",
                   "ref_audio": False, "ref_text": False, "style_tags": True,  "builtin_voices": True},
    "chatterbox": {"display": "Chatterbox",      "desc": "Emotion control, voice cloning",                   "weights_repo": "ResembleAI/chatterbox",     "weights_dir": "chatterbox",  "required_weight_files": ["ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors", "tokenizer.json", "conds.pt"], "weights_size": "English runtime assets only",
                   "ref_audio": True,  "ref_text": False, "style_tags": False, "builtin_voices": False},
    "dia":        {"display": "Dia 1.6B",        "desc": "Dialogue TTS with [S1]/[S2] speaker tags",         "weights_repo": "nari-labs/Dia-1.6B-0626",   "weights_dir": "dia", "required_weight_files": ["config.json", "dia-v1.pth"], "extra_weights_dirs": ["dia-dac"], "weights_size": "~6.45GB selected checkpoint + local DAC decoder",
                   "ref_audio": True,  "ref_text": False, "style_tags": True,  "builtin_voices": False},
    "f5":         {"display": "F5-TTS",          "desc": "Diffusion TTS, reference audio cloning",           "weights_repo": "SWivid/F5-TTS",             "weights_dir": "f5-tts",      "required_weight_files": ["F5TTS_v1_Base/model_1250000.safetensors", "F5TTS_v1_Base/vocab.txt"], "weights_size": "~1.35GB selected checkpoint",
                   "ref_audio": True,  "ref_text": True,  "style_tags": False, "builtin_voices": False},
    "fish":       {"display": "Fish Speech",     "desc": "Fast TTS with voice cloning (OpenAudio S1-mini)", "weights_repo": "fishaudio/openaudio-s1-mini", "weights_dir": "fish-speech", "required_weight_files": ["model.pth", "codec.pth", "config.json", "tokenizer.tiktoken", "special_tokens.json"], "weights_size": "~3.4GB model + codec",
                   "ref_audio": True,  "ref_text": True,  "style_tags": True,  "builtin_voices": False},
    "higgs":      {"display": "Higgs Audio 3B",  "desc": "Boson AI ChatML (CPU supported)",                  "weights_repo": "bosonai/higgs-audio-v2-generation-3B-base", "weights_dir": "higgs-audio", "required_weight_files": ["config.json", "model.safetensors.index.json", "model-00001-of-00003.safetensors", "model-00002-of-00003.safetensors", "model-00003-of-00003.safetensors", "tokenizer.json"], "extra_weights_dirs": ["higgs-audio-tokenizer", "hubert_base"], "weights_size": "~12GB model + audio tokenizer + HuBERT",
                   "ref_audio": True,  "ref_text": True,  "style_tags": False, "builtin_voices": False},
    "kokoro":     {"display": "Kokoro 82M",      "desc": "Lightweight, fast, 54 built-in voices",            "weights_repo": "hexgrad/Kokoro-82M",        "weights_dir": "kokoro",      "weights_size": "~300MB",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": True},
    "qwen":       {"display": "Qwen Omni 7B",    "desc": "Two-voice speech output from Qwen Omni",            "weights_repo": "Qwen/Qwen2.5-Omni-7B",     "weights_dir": "qwen-omni", "required_weight_files": ["config.json", "model.safetensors.index.json", "model-00001-of-00005.safetensors", "model-00002-of-00005.safetensors", "model-00003-of-00005.safetensors", "model-00004-of-00005.safetensors", "model-00005-of-00005.safetensors", "spk_dict.pt", "tokenizer.json"], "weights_size": "~21GB selected runtime snapshot",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": True},
    "vibevoice":  {"display": "VibeVoice",       "desc": "Long-form turn-based TTS with up to four voices", "weights_repo": "microsoft/VibeVoice-1.5B",  "weights_dir": "vibevoice", "required_weight_files": ["config.json", "preprocessor_config.json", "model.safetensors.index.json", "model-00001-of-00003.safetensors", "model-00002-of-00003.safetensors", "model-00003-of-00003.safetensors"], "extra_weights_dirs": ["vibevoice-qwen-tokenizer"], "weights_size": "~5.41GB checkpoint + local Qwen tokenizer",
                   "ref_audio": True,  "ref_text": False, "style_tags": False, "builtin_voices": False},
    "whisper":    {"display": "Whisper",         "desc": "Speech recognition for verification",              "weights_repo": "openai/whisper-base",       "weights_dir": "whisper",     "required_weight_files": ["base.pt"], "weights_size": "~145MB base model; other sizes optional",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": False},
    "xtts":       {"display": "XTTS v2",         "desc": "Multilingual voice cloning, 58 built-in voices",   "weights_repo": "coqui/XTTS-v2",            "weights_dir": "xtts-v2",     "weights_size": "~1.8GB",
                   "ref_audio": True,  "ref_text": False, "style_tags": False, "builtin_voices": True},
    "speecht5":   {"display": "SpeechT5",       "desc": "Microsoft HF-native, multi-speaker, lightweight",  "weights_repo": "microsoft/speecht5_tts",    "weights_dir": "speecht5",    "weights_size": "~250MB",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": True},
    "parler":     {"display": "Parler-TTS",     "desc": "Describe the voice in text — style-prompted TTS",  "weights_repo": "parler-tts/parler-tts-mini-v1.1", "weights_dir": "parler-tts", "required_weight_files": ["model.safetensors", "config.json", "tokenizer.json"], "extra_weights_dirs": ["parler-desc-tokenizer"], "weights_size": "~3.6GB + tokenizer",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": True},
    "outetts":    {"display": "OuteTTS 1.0 0.6B", "desc": "14-language TTS with one bundled voice and reusable cloning profiles", "weights_repo": "OuteAI/OuteTTS-1.0-0.6B", "weights_dir": "outetts-1.0-0.6b", "required_weight_files": ["config.json", "model.safetensors", "tokenizer.json"], "extra_weights_dirs": ["outetts-dac"], "weights_size": "~1.22GB model + pinned 24kHz DAC",
                   "ref_audio": True,  "ref_text": True,  "style_tags": False, "builtin_voices": True},
    "vits":       {"display": "VITS",           "desc": "Fast lightweight single-speaker (Coqui, no GPU needed)", "weights_repo": None,                   "weights_dir": None,          "weights_size": "~150MB",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": False},
    "edge":       {"display": "Edge TTS",       "desc": "Microsoft neural voices — cloud, 300+ live-catalog voices, no GPU", "weights_repo": None,               "weights_dir": None,          "weights_size": "none",
                   "ref_audio": False, "ref_text": False, "style_tags": False, "builtin_voices": True},
    "voxtral":    {"display": "Voxtral 4B TTS",  "desc": "Mistral 4B TTS, 9 languages, voice cloning + 20 preset voices", "weights_repo": "mistralai/Voxtral-4B-TTS-2603", "weights_dir": "voxtral-4b-tts", "required_weight_files": ["consolidated.safetensors", "params.json", "tekken.json", "voice_embedding/casual_male.pt"], "weights_size": "~8.03GB selected checkpoint and preset embeddings",
                   "ref_audio": True,  "ref_text": False, "style_tags": False, "builtin_voices": True},
    "voxcpm2":    {"display": "VoxCPM2",          "desc": "OpenBMB diffusion-AR TTS, 30 languages, 48kHz, voice cloning + voice design",   "weights_repo": "openbmb/VoxCPM2",         "weights_dir": "voxcpm2", "required_weight_files": ["config.json", "model.safetensors", "audiovae.pth", "tokenizer.json"], "weights_size": "~4.96GB selected checkpoint",
                   "ref_audio": True,  "ref_text": True,  "style_tags": True,  "builtin_voices": False},
    "csm":        {"display": "Sesame CSM-1B",    "desc": "Conversational speech model, natural in dialogue, multi-speaker via context", "weights_repo": "sesame/csm-1b",           "weights_dir": "csm-1b", "required_weight_files": ["config.json", "transformers.safetensors.index.json", "transformers-00001-of-00002.safetensors", "transformers-00002-of-00002.safetensors", "preprocessor_config.json", "tokenizer.json"], "weights_size": "~7.15GB selected Transformers checkpoint",
                   "ref_audio": True,  "ref_text": True,  "style_tags": False, "builtin_voices": True},
    "orpheus":    {"display": "Orpheus 3B",       "desc": "Llama-based expressive English TTS with 8 preset voices and inline emotion tags",   "weights_repo": "unsloth/orpheus-3b-0.1-ft", "weights_dir": "orpheus-3b", "extra_weights_dirs": ["snac-24khz"], "required_weight_files": ["config.json", "model.safetensors.index.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors", "tokenizer.json"], "weights_size": "~6.72GB selected FP16 checkpoint + SNAC decoder",
                   "ref_audio": False, "ref_text": False, "style_tags": True,  "builtin_voices": True},
}

# Conservative planning estimates for external orchestrators. These are not
# hard CUDA limits: generation length, precision, attention backend, and voice
# cloning inputs can add transient allocations. Callers should compare these
# values with live /api/devices free VRAM and keep a safety reserve.
MODEL_VRAM_ESTIMATE_GB = {
    "bark": 6, "chatterbox": 4, "dia": 8, "f5": 3, "fish": 5,
    "higgs": 16, "kokoro": 1, "qwen": 16, "vibevoice": 7,
    "whisper": 10, "xtts": 3, "speecht5": 1, "parler": 2,
    "outetts": 3, "vits": 0, "edge": 0, "voxtral": 20,
    "voxcpm2": 8, "csm": 5, "orpheus": 8,
}

# Per-model recommended defaults
MODEL_DEFAULTS = {
    "xtts":       {"temperature": 0.65, "repetition_penalty": 2.0, "top_k": 50, "top_p": 0.85},
    "fish":       {"temperature": 0.8, "repetition_penalty": 1.1, "top_p": 0.8, "seed": 0},
    "kokoro":     {},
    "bark":       {"temperature": 0.7, "waveform_temperature": 0.7},
    "chatterbox": {"temperature": 0.8, "repetition_penalty": 1.2, "exaggeration": 0.5, "cfg_weight": 0.5},
    "f5":         {"nfe_step": 32, "cfg_scale": 2.0, "seed": 0},
    "dia":        {"temperature": 1.8, "cfg_scale": 3.0, "top_p": 0.90, "top_k": 50},
    "qwen":       {"temperature": 0.9, "top_p": 0.8, "top_k": 40, "seed": 0},
    "vibevoice":  {"cfg_scale": 1.3, "seed": 0},
    "higgs":      {"temperature": 0.3, "top_p": 0.95, "top_k": 50, "seed": 0},
    "speecht5":   {},
    "parler":     {"temperature": 1.0},
    "outetts":    {"temperature": 0.4, "repetition_penalty": 1.1, "top_p": 0.9, "top_k": 40, "seed": 0},
    "vits":       {},
    "edge":       {"pitch": 0, "volume": 0},
    "voxtral":    {"cfg_alpha": 1.2, "seed": 42},
    "voxcpm2":    {"cfg_scale": 2.0, "nfe_step": 10},
    "csm":        {"temperature": 0.9, "top_p": 0.95, "top_k": 50, "seed": 42},
    "orpheus":    {"temperature": 0.6, "top_p": 0.95, "repetition_penalty": 1.1, "seed": 42},
}

# Non-slider model-native controls exposed by the Testing tab.
# Word, chunk, and max-token controls stay hidden because the app owns
# chunking and text sizing.
MODEL_FIELDS = {
    "xtts": {
        "language": {
            "label": "Language",
            "type": "select",
            "default": "en",
            "options": [
                ["en", "English"],
                ["es", "Spanish"],
                ["fr", "French"],
                ["de", "German"],
                ["it", "Italian"],
                ["pt", "Portuguese"],
                ["pl", "Polish"],
                ["tr", "Turkish"],
                ["ru", "Russian"],
                ["nl", "Dutch"],
                ["cs", "Czech"],
                ["ar", "Arabic"],
                ["zh-cn", "Chinese"],
                ["ja", "Japanese"],
                ["ko", "Korean"],
                ["hu", "Hungarian"],
                ["hi", "Hindi"],
            ],
        },
    },
    "parler": {
        "voice_description": {
            "label": "Voice Description",
            "type": "text",
            "default": "",
            "placeholder": "Describe speaker, recording quality, pace, tone...",
        },
    },
    "higgs": {
        "voice_description": {
            "label": "Scene / Speaker Description",
            "type": "text",
            "default": "",
            "placeholder": "e.g. SPEAKER0: warm mature voice; moderate pace; quiet studio",
        },
    },
}

# Spinbox/slider config: param -> (label, min, max, step, format)
PARAM_CONFIG = {
    "temperature":          ("Temperature",  0.0,  3.0,  0.05, ".2f"),
    "speed":                ("Speed",        0.5,  2.0,  0.1,  ".1f"),
    "repetition_penalty":   ("Repetition Penalty", 0.5, 5.0, 0.1, ".1f"),
    "top_p":                ("Top P",        0.0,  1.0,  0.05, ".2f"),
    "top_k":                ("Top K",        1,    200,  1,    "d"),
    "cfg_scale":            ("CFG Scale",    0.0,  10.0, 0.5,  ".1f"),
    "exaggeration":         ("Exaggeration", 0.0,  1.0,  0.05, ".2f"),
    "cfg_weight":           ("CFG Weight",   0.0,  1.0,  0.05, ".2f"),
    "waveform_temperature": ("Waveform Temperature", 0.0, 3.0, 0.05, ".2f"),
    "seed":                 ("Seed",         0,    99999,1,    "d"),
    "nfe_step":             ("NFE Steps",    4,    64,   4,    "d"),
    "pitch":                ("Pitch Hz",    -50,   50,   5,    "d"),
    "volume":               ("Volume %",    -50,   50,   5,    "d"),
    "cfg_alpha":            ("CFG Alpha",    0.5,  3.0,  0.1,  ".1f"),
    "de_reverb":            ("De-reverb",    0.0,  1.0,  0.1,  ".1f"),
    "de_ess":               ("De-ess",       0.0,  1.0,  0.1,  ".1f"),
    "tolerance":            ("Tolerance",    0,    100,  5,    "d"),
}

# Per-model PARAM_CONFIG overrides — narrow ranges where a model rejects values
# that the global slider allows. Format: {model: {param: (min, max, step)}}.
# Only the fields you supply are overridden; others fall through to PARAM_CONFIG.
PARAM_OVERRIDES = {
    "fish":     {"temperature": (0.0, 1.0, 0.05)},  # ServeTTSRequest validates ≤ 1.0
}

TOOLTIPS = {
    "speed": "Playback speed multiplier. 1.0 = normal, 0.5 = half, 2.0 = double. Pitch-preserving time-stretch via pyrubberband.",
    "temperature": "Controls randomness. Lower = consistent, Higher = expressive.",
    "repetition_penalty": "Penalizes repeated words/sounds. Higher = less repetition.",
    "de_reverb": "Remove room echo. 0.0 = none, 1.0 = maximum.",
    "de_ess": "Reduce harsh sibilance. 0.0 = none, 1.0 = maximum.",
    "format": "Output audio format. WAV = lossless, MP3 = compressed.",
    "top_p": "Nucleus sampling — probability threshold for token selection.",
    "top_k": "Limits token selection to top K most likely options.",
    "cfg_scale": "Classifier-Free Guidance. Higher = follows text more closely.",
    "exaggeration": "Emotional intensity (Chatterbox only). 0 = neutral, 1 = max.",
    "cfg_weight": "CFG weight for Chatterbox voice matching.",
    "waveform_temperature": "Controls waveform variation in Bark.",
    "seed": "Random seed for reproducibility. 0 = random each time.",
    "nfe_step": "Diffusion/flow integration steps. VoxCPM2 defaults to 10; F5-TTS defaults to 32. Higher can improve quality but increases runtime.",
    "pitch": "Pitch adjustment in Hz for Edge-TTS voices. 0 = normal, +10 = higher, -10 = lower.",
    "volume": "Volume adjustment percentage for Edge-TTS voices. 0 = normal, +20 = louder, -20 = softer.",
    "language": "XTTS synthesis language. Pick the language that matches the input text.",
    "voice_description": "Parler voice/style prompt. Describe speaker identity, recording quality, pace, emotion, and tone.",
    "cfg_alpha": "Voxtral flow-matching guidance. Higher = stricter voice match, lower = more variation. 1.2 default.",
}
