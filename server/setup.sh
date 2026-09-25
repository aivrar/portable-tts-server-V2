#!/usr/bin/env bash
# =============================================================================
# TTS Server --- Master Setup Script
# Called by the native launcher during first run inside this WSL2 distro.
# Runs as root --- no sudo needed.
# =============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

# This app owns one dedicated WSL distribution whose VHDX lives beside the
# Windows app. Refuse to provision a default Ubuntu (or another Linbox)
# because /opt is distro-local and a wrong launch would silently duplicate the
# venv, package caches, and models into that distro's backing disk.
python3 "$APP_DIR/tools/configure_runtime.py"

# A distro name is global to the Windows user. If this folder was copied or
# moved while an older registration with the same name still exists, WSL would
# otherwise mount that older VHDX. Ask the host-side portable launcher to verify
# that the registered BasePath is this portable folder's wsl directory before any /opt write.
if ! command -v powershell.exe >/dev/null 2>&1; then
    echo "ERROR: Windows PowerShell interop is unavailable; cannot verify the Linbox VHDX location." >&2
    exit 78
fi
PREFLIGHT_WIN_PATH="$(wslpath -w "$APP_DIR/Start-TTSServer.ps1")"
if ! powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass \
        -File "$PREFLIGHT_WIN_PATH" -VerifyOnly >/dev/null; then
    echo "ERROR: Linbox registration preflight failed; refusing to write inside this distro." >&2
    exit 78
fi

OUTPUT_DIR="$APP_DIR/output"
RUN_DIR="$OUTPUT_DIR/run"
SECRETS_DIR="$APP_DIR/secrets"
DISCOVERY_DIR="$RUN_DIR/registry"

# All caches, model weights, pip wheels, and Python venv live INSIDE the WSL
# ext4 filesystem (under /opt/tts_server). Only user-facing artifacts (voices,
# outputs, project folders, run-time files like the API token) stay on the
# Windows-mounted app folder. This keeps the hot path off 9P/drvfs.
OPT_DIR="/opt/tts_server"
CACHE_DIR="$OPT_DIR/cache"
mkdir -p "$RUN_DIR" "$CACHE_DIR" "$SECRETS_DIR" "$DISCOVERY_DIR"
export HOME="$CACHE_DIR/home"
export XDG_CACHE_HOME="$CACHE_DIR/xdg-cache"
export XDG_DATA_HOME="$CACHE_DIR/xdg-data"
export XDG_CONFIG_HOME="$CACHE_DIR/xdg-config"
export XDG_STATE_HOME="$CACHE_DIR/xdg-state"
export PIP_CACHE_DIR="$CACHE_DIR/pip"
export TMPDIR="$CACHE_DIR/tmp"
export PATH="/opt/tts_server/venv/bin:/usr/local/cuda/bin:/usr/lib/wsl/lib:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONNOUSERSITE=1
export NUMBA_CACHE_DIR="$CACHE_DIR/numba"
export TRITON_CACHE_DIR="$CACHE_DIR/triton"
export CUDA_CACHE_PATH="$CACHE_DIR/cuda"
export TORCHINDUCTOR_CACHE_DIR="$CACHE_DIR/torchinductor"
export MPLCONFIGDIR="$CACHE_DIR/matplotlib"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" \
         "$XDG_STATE_HOME" "$PIP_CACHE_DIR" "$TMPDIR" "$NUMBA_CACHE_DIR" \
         "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH" "$TORCHINDUCTOR_CACHE_DIR" \
         "$MPLCONFIGDIR"

# Concurrency lock --- prevent parallel setup/install runs
LOCK_FILE="$RUN_DIR/tts_setup.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: Another setup/install process is already running"
    exit 1
fi

# Debug log
DEBUG_LOG="$RUN_DIR/tts_server_setup.log"
# tee runs as a process-substitution child that bash does NOT wait for on exit,
# so without this the tail of the log (often the final error before an abort) is
# frequently lost. Capture tee's PID, then on every exit path close our stdout/
# stderr so tee sees EOF and flushes, and wait for it before the process tears
# down. The trap is set BEFORE redirection so it is in place for all exit paths.
trap 'exec >&- 2>&-; [ -n "${TEE_PID:-}" ] && wait "$TEE_PID" 2>/dev/null' EXIT
exec > >(tee -a "$DEBUG_LOG") 2>&1
TEE_PID=$!

echo "============================================"
echo "  TTS Server --- Environment Setup"
echo "  $(date)"
echo "============================================"
echo "Debug log: $DEBUG_LOG"
echo "Shell: $SHELL / bash $BASH_VERSION"
echo "User: $(whoami) / UID=$(id -u)"
echo "Kernel: $(uname -r)"

# ---------------------------------------------------------------------------
# Step 1: Detect paths
# ---------------------------------------------------------------------------
echo "Server code: $SCRIPT_DIR"
echo "App directory: $APP_DIR"

# Persistent directories --- all inside WSL ext4 for speed and self-containment
VENV_DIR="$OPT_DIR/venv"
OVERRIDES_DIR="$OPT_DIR/overrides"
REPOS_DIR="$OPT_DIR/repos"
MODELS_DIR="$OPT_DIR/data"

# Windows-side directories (via /mnt/) --- user-facing only
VOICES_DIR="$APP_DIR/voices"
OUTPUT_DIR="$APP_DIR/output"
PROJECTS_DIR="$APP_DIR/projects_output"

mkdir -p "$OPT_DIR" "$OVERRIDES_DIR" "$REPOS_DIR" "$MODELS_DIR"

# configure_runtime.py already wrote literal paths and refreshed the server link.
# Readers parse env.conf as data; do not add shell escaping to path values.
echo "Portable runtime paths configured in $OPT_DIR/env.conf"

base_venv_ready() {
    [ -x "$VENV_DIR/bin/python3" ] || return 1
    "$VENV_DIR/bin/python3" - "$SCRIPT_DIR/requirements-base.lock" <<'PY' >/dev/null 2>&1
import importlib
import importlib.metadata
import sys
from pathlib import Path
required = [
    "torch", "torchaudio", "transformers", "accelerate", "fastapi", "uvicorn", "pydantic", "httpx",
    "numpy", "scipy", "librosa", "soundfile", "huggingface_hub",
    "python_multipart", "whisper", "pydub", "pyrubberband", "pyloudnorm", "noisereduce",
]
for module in required:
    importlib.import_module(module)
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    package, expected = line.split('==', 1)
    actual = importlib.metadata.version(package).split('+', 1)[0]
    if actual != expected:
        raise RuntimeError(f'{package}: expected {expected}, found {actual}')
PY
}

SETUP_READY_STAMP="$RUN_DIR/setup_complete.stamp"
if [ "${TTS_FAST_START:-1}" = "1" ] \
   && [ -f "$SETUP_READY_STAMP" ] \
   && [ -x "$VENV_DIR/bin/python3" ] \
   && [ -f "$OPT_DIR/server/tts_api_server.py" ] \
   && base_venv_ready; then
    echo "Fast start: setup already completed; server link refreshed."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 2: Install system dependencies (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "=== Installing system dependencies ==="

APT_UPDATED=false

install_if_missing() {
    local cmd="$1"
    local pkg="$2"
    if ! command -v "$cmd" > /dev/null 2>&1; then
        if [ "$APT_UPDATED" = false ]; then
            echo "Running apt-get update..."
            apt-get update -qq
            APT_UPDATED=true
        fi
        echo "Installing $pkg..."
        apt-get install -y -qq "$pkg"
    else
        echo "$cmd: already installed"
    fi
}

# Core tools
install_if_missing "ffmpeg" "ffmpeg"
install_if_missing "espeak-ng" "espeak-ng"
install_if_missing "sox" "sox"
install_if_missing "git" "git"

# Libraries needed by Python packages
for pkg in libsndfile1 libsndfile1-dev portaudio19-dev rubberband-cli python3-venv python3-dev build-essential; do
    if ! dpkg -s "$pkg" > /dev/null 2>&1; then
        if [ "$APT_UPDATED" = false ]; then
            echo "Running apt-get update..."
            apt-get update -qq
            APT_UPDATED=true
        fi
        echo "Installing $pkg..."
        apt-get install -y -qq "$pkg"
    else
        echo "$pkg: already installed"
    fi
done

# Check CUDA / GPU availability
echo ""
echo "=== Checking GPU ==="
if command -v nvidia-smi > /dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true
    echo "CUDA GPU detected --- models will use GPU acceleration"
    CUDA_AVAILABLE=true
else
    echo "WARNING: nvidia-smi not found. Models will run on CPU only."
    echo "For GPU support, install NVIDIA drivers for WSL2:"
    echo "  https://developer.nvidia.com/cuda/wsl"
    CUDA_AVAILABLE=false
fi

# ---------------------------------------------------------------------------
# Step 3: Create base virtual environment (skip if exists and working)
# ---------------------------------------------------------------------------
echo ""
echo "=== Setting up Python virtual environment ==="


if base_venv_ready; then
    echo "Base venv already has required shared packages --- skipping creation"
else
    # Only create venv from scratch if it doesn't exist; otherwise repair in place
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
        # Verify venv is functional
        if [ ! -x "$VENV_DIR/bin/python3" ] || [ ! -x "$VENV_DIR/bin/pip" ]; then
            echo "ERROR: venv creation produced broken environment, removing and retrying"
            rm -rf "$VENV_DIR"
            python3 -m venv "$VENV_DIR"
        fi
    else
        echo "Venv exists but required imports failed --- repairing in place..."
    fi

    # Keep pkg_resources available for older optional model runtimes.
    "$VENV_DIR/bin/pip" install --upgrade pip "setuptools<81" wheel -q

    # PyTorch --- install or reinstall
    if ! "$VENV_DIR/bin/python3" -c "import torch, torchaudio; assert torch.__version__.split('+')[0] == torchaudio.__version__.split('+')[0] == '2.6.0'" 2>/dev/null; then
        echo ""
        echo "Installing PyTorch (this may take several minutes)..."
        if [ "$CUDA_AVAILABLE" = true ] || [ "${TTS_BUNDLE_CUDA:-0}" = "1" ]; then
            "$VENV_DIR/bin/pip" install "torch==2.6.0" "torchaudio==2.6.0" \
                --index-url https://download.pytorch.org/whl/cu124
        else
            "$VENV_DIR/bin/pip" install "torch==2.6.0" "torchaudio==2.6.0" \
                --index-url https://download.pytorch.org/whl/cpu
        fi
        # Verify installation succeeded
        if ! "$VENV_DIR/bin/python3" -c "import torch; print(f'PyTorch {torch.__version__} OK')"; then
            echo "ERROR: PyTorch installation failed --- cannot continue"
            exit 1
        fi
    fi

    # Reproduce the shared Python environment captured in the child distro.
    "$VENV_DIR/bin/pip" install -q -c "$SCRIPT_DIR/requirements-base.lock" \
        -r "$SCRIPT_DIR/requirements-base.txt"

    echo "Base venv setup complete!"
fi

# Verify PyTorch (non-fatal --- don't let a print error kill setup)
echo ""
echo "=== Verifying installation ==="
"$VENV_DIR/bin/python3" -c "
import torch
print(f'PyTorch: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
" || echo "WARNING: Verification had errors (non-fatal)"

# Create user-facing directories on Windows side; cache/models stay in WSL ext4
mkdir -p "$VOICES_DIR" "$OUTPUT_DIR" "$OUTPUT_DIR/jobs" "$OUTPUT_DIR/logs" \
         "$RUN_DIR" "$SECRETS_DIR" "$PROJECTS_DIR"

# Installer downloads are not runtime assets.  Do not leave several gigabytes
# of reusable pip wheels or abandoned scratch files sitting in the portable
# VHDX after a successful provision.  Model/HuggingFace data is intentionally
# untouched because those files are runtime state.
echo "Clearing completed setup download caches..."
"$VENV_DIR/bin/pip" cache purge >/dev/null 2>&1 || true
# Shared TMPDIR may contain active worker/export files; installers do not sweep it.

echo ""
echo "============================================"
echo "  Setup complete!"
echo "  Choose additional engines in the Setup tab."
echo "============================================"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$SETUP_READY_STAMP"
