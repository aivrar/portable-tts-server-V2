#!/usr/bin/env bash
# =============================================================================
# TTS Server — Per-Model Installer
# Usage: bash install_model.sh <model_id>
#
# Installs model-specific packages (into base venv or override directory)
# and downloads model weights via HuggingFace Hub with xet.
# =============================================================================

set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Concurrency lock — prevent parallel install runs
if [ "$(id -u)" -ne 0 ]; then
    echo "WARNING: Not running as root. Some operations may fail."
fi

MODEL="$1"
if [ -z "$MODEL" ]; then
    echo "Usage: $0 <model_id>"
    echo "Models: kokoro, dia, fish, f5, bark, xtts, chatterbox, qwen, vibevoice, higgs, whisper, speecht5, parler, outetts, vits, edge, voxtral, voxcpm2, csm, orpheus"
    exit 1
fi

# Read environment config.
# Do not `source` env.conf — that executes it as bash and
# makes the installer's safety depend entirely on setup.sh's quote_env_value()
# never regressing. Parse it non-executingly instead, mirroring config.py's
# _read_env_conf (strip exactly one outer quote pair; no shell evaluation).
ENV_CONF="/opt/tts_server/env.conf"
if [ ! -f "$ENV_CONF" ]; then
    echo "ERROR: $ENV_CONF not found. Run setup.sh first."
    exit 1
fi
while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in
        *=*) ;;            # only key=value lines
        *) continue ;;
    esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    # Trim surrounding whitespace from key and value (matches config.py .strip()).
    _key="${_key#"${_key%%[![:space:]]*}"}"; _key="${_key%"${_key##*[![:space:]]}"}"
    _val="${_val#"${_val%%[![:space:]]*}"}"; _val="${_val%"${_val##*[![:space:]]}"}"
    # Strip exactly one outer matching quote pair, as config.py does.
    if [ "${#_val}" -ge 2 ]; then
        _first="${_val:0:1}"; _last="${_val: -1}"
        if { [ "$_first" = '"' ] || [ "$_first" = "'" ]; } && [ "$_first" = "$_last" ]; then
            _val="${_val:1:${#_val}-2}"
        fi
    fi
    # Only accept identifier-like keys; assign by name without eval.
    case "$_key" in
        [A-Za-z_]*) printf -v "$_key" '%s' "$_val" ;;
    esac
done < "$ENV_CONF"
unset _line _key _val _first _last

# Defaults match setup.sh — everything inside WSL ext4 (/opt/tts_server/...)
# so /mnt/<drive>/... is never on the hot install path.
CACHE_DIR="${CACHE_DIR:-/opt/tts_server/cache}"
MODELS_DIR="${MODELS_DIR:-/opt/tts_server/data}"
RUN_DIR="${RUN_DIR:-$OUTPUT_DIR/run}"
SECRETS_DIR="${SECRETS_DIR:-$APP_DIR/secrets}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$SECRETS_DIR/hf_token}"
LEGACY_HF_TOKEN_FILE="/opt/tts_server/hf_token"
mkdir -p "$RUN_DIR" "$CACHE_DIR" "$SECRETS_DIR" "$MODELS_DIR"
export HOME="$CACHE_DIR/home"
export XDG_CACHE_HOME="$CACHE_DIR/xdg-cache"
export XDG_DATA_HOME="$CACHE_DIR/xdg-data"
export XDG_CONFIG_HOME="$CACHE_DIR/xdg-config"
export XDG_STATE_HOME="$CACHE_DIR/xdg-state"
export PIP_CACHE_DIR="$CACHE_DIR/pip"
export TMPDIR="${TMPDIR:-$CACHE_DIR/tmp}"
export HF_HOME="$MODELS_DIR"
export HF_HUB_CACHE="$MODELS_DIR/hub"
export HUGGINGFACE_HUB_CACHE="$MODELS_DIR/hub"
export HF_DATASETS_CACHE="$MODELS_DIR/datasets"
export HF_MODULES_CACHE="$MODELS_DIR/modules"
export TRANSFORMERS_CACHE="$MODELS_DIR/hub"
export TORCH_HOME="$MODELS_DIR/torch"
export COQUI_TTS_CACHE="$MODELS_DIR/coqui"
export TTS_HOME="$MODELS_DIR/coqui"
export COQUI_TOS_AGREED=1
export PATH="$VENV_DIR/bin:/usr/local/cuda/bin:/usr/lib/wsl/lib:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONNOUSERSITE=1
export NUMBA_CACHE_DIR="$CACHE_DIR/numba"
export TRITON_CACHE_DIR="$CACHE_DIR/triton"
export CUDA_CACHE_PATH="$CACHE_DIR/cuda"
export TORCHINDUCTOR_CACHE_DIR="$CACHE_DIR/torchinductor"
export MPLCONFIGDIR="$CACHE_DIR/matplotlib"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" \
         "$XDG_STATE_HOME" "$PIP_CACHE_DIR" "$TMPDIR" "$HF_HUB_CACHE" \
         "$HF_DATASETS_CACHE" "$HF_MODULES_CACHE" "$TORCH_HOME" "$COQUI_TTS_CACHE" \
         "$NUMBA_CACHE_DIR" "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH" \
         "$TORCHINDUCTOR_CACHE_DIR" "$MPLCONFIGDIR"

LOCK_FILE="$RUN_DIR/tts_setup.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: Another install process is already running"
    exit 1
fi

# Own only this invocation's temporary directory.
INSTALL_TMP="$(mktemp -d "$CACHE_DIR/tmp/install.XXXXXX")"
export TMPDIR="$INSTALL_TMP"
trap 'rm -rf -- "$INSTALL_TMP"' EXIT

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python3"
export PIP_DISABLE_PIP_VERSION_CHECK=1

clean_invalid_pip_distributions() {
    local py_ver
    local base_sp
    py_ver="$("$PYTHON" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    if [ -z "$py_ver" ]; then
        return 0
    fi
    base_sp="$VENV_DIR/lib/$py_ver/site-packages"
    if [ -d "$base_sp" ]; then
        # pip can leave "~package" directories after an interrupted uninstall.
        # They are not importable packages, but pip warns on every install and
        # the noise hides real setup errors.
        find "$base_sp" -maxdepth 1 -name '~*' -print -exec rm -rf {} + 2>/dev/null || true
    fi
}

clean_invalid_pip_distributions

echo "============================================"
echo "  Installing model: $MODEL"
echo "============================================"

# ---------------------------------------------------------------------------
# Helper: install into override directory
# ---------------------------------------------------------------------------
commit_override_stage() {
    # Swap a fully-built staging directory into place while keeping the old
    # runtime available for rollback until the rename succeeds.
    local target="$1"
    local staging="$2"
    local backup="${target}.backup.$$"
    rm -rf "$backup"
    if [ -d "$target" ]; then
        mv "$target" "$backup"
    fi
    if mv "$staging" "$target"; then
        rm -rf "$backup"
        return 0
    fi
    echo "ERROR: Could not activate staged override at $target; restoring previous runtime" >&2
    rm -rf "$staging"
    if [ -d "$backup" ]; then
        mv "$backup" "$target"
    fi
    return 1
}

install_override() {
    local name="$1"
    shift
    local target="$OVERRIDES_DIR/$name"
    local requested_hash
    requested_hash="$(printf '%s\n' "$@" | sha256sum | awk '{print $1}')"
    if [ -f "$target/.install_complete" ] \
        && [ -f "$target/.requirements_hash" ] \
        && [ "$(cat "$target/.requirements_hash")" = "$requested_hash" ] \
        && [ "${TTS_FORCE_REINSTALL:-0}" != "1" ]; then
        echo "Reusing completed override packages at $target."
        return 0
    fi
    local staging
    staging="$(mktemp -d "$OVERRIDES_DIR/.${name}.stage.XXXXXX")"
    echo "Installing override packages to staging directory $staging..."
    # 9>&- closes the inherited flock fd in the child so a package post-install
    # step that daemonizes cannot keep the install lock held.
    if ! "$PIP" install --ignore-installed --target="$staging" "$@" 9>&-; then
        rm -rf "$staging"
        echo "ERROR: Override install failed; previous runtime left intact at $target" >&2
        return 1
    fi
    printf '%s\n' "$requested_hash" > "$staging/.requirements_hash"
    touch "$staging/.install_complete"
    commit_override_stage "$target" "$staging"
}

install_override_runtime_only() {
    # Install a source package without its declared convenience/UI dependency
    # tree, after installing an explicit inference-only dependency set.  This
    # keeps packages such as Dia from pulling Gradio/pandas into a worker that
    # never imports them.
    local name="$1"
    local package="$2"
    shift 2
    local target="$OVERRIDES_DIR/$name"
    local requested_hash
    local source_revision=""
    if [ -d "$package/.git" ]; then
        source_revision="$(git -C "$package" rev-parse HEAD)"
    fi
    requested_hash="$(printf '%s\n' "$package" "$source_revision" "$@" | sha256sum | awk '{print $1}')"
    if [ -f "$target/.install_complete" ] \
        && [ -f "$target/.requirements_hash" ] \
        && [ "$(cat "$target/.requirements_hash")" = "$requested_hash" ] \
        && [ "${TTS_FORCE_REINSTALL:-0}" != "1" ]; then
        echo "Reusing completed runtime-only override at $target."
        return 0
    fi
    local staging
    staging="$(mktemp -d "$OVERRIDES_DIR/.${name}.stage.XXXXXX")"
    echo "Installing inference runtime packages to staging directory $staging..."
    if ! "$PIP" install --ignore-installed --target="$staging" "$@" 9>&-; then
        rm -rf "$staging"
        echo "ERROR: Runtime dependency install failed; previous runtime left intact at $target" >&2
        return 1
    fi
    echo "Installing source package without UI/convenience dependencies..."
    if ! "$PIP" install --ignore-installed --no-deps --target="$staging" "$package" 9>&-; then
        rm -rf "$staging"
        echo "ERROR: Source package install failed; previous runtime left intact at $target" >&2
        return 1
    fi
    printf '%s\n' "$requested_hash" > "$staging/.requirements_hash"
    touch "$staging/.install_complete"
    commit_override_stage "$target" "$staging"
}

install_override_minimal() {
    # Install an explicitly complete inference set without dependency
    # resolution. This lets selected workers reuse the app-owned base
    # Torch/CUDA stack instead of storing another multi-gigabyte copy.
    local name="$1"
    shift
    local target="$OVERRIDES_DIR/$name"
    local requested_hash
    requested_hash="$(printf '%s\n' "$@" | sha256sum | awk '{print $1}')"
    if [ -f "$target/.install_complete" ] \
        && [ -f "$target/.requirements_hash" ] \
        && [ "$(cat "$target/.requirements_hash")" = "$requested_hash" ] \
        && [ "${TTS_FORCE_REINSTALL:-0}" != "1" ]; then
        echo "Reusing completed minimal override at $target."
        return 0
    fi
    local staging
    staging="$(mktemp -d "$OVERRIDES_DIR/.${name}.stage.XXXXXX")"
    echo "Installing dependency-complete minimal override at staging directory $staging..."
    if ! "$PIP" install --ignore-installed --no-deps --target="$staging" "$@" 9>&-; then
        rm -rf "$staging"
        echo "ERROR: Minimal override install failed; previous runtime left intact at $target" >&2
        return 1
    fi
    printf '%s\n' "$requested_hash" > "$staging/.requirements_hash"
    touch "$staging/.install_complete"
    commit_override_stage "$target" "$staging"
}

patch_higgs_cuda_load() {
    local engine="$OVERRIDES_DIR/higgs/higgs-audio/boson_multimodal/serve/serve_engine.py"
    local audio_tokenizer="$OVERRIDES_DIR/higgs/higgs-audio/boson_multimodal/audio_processing/higgs_audio_tokenizer.py"
    local dac_module="$OVERRIDES_DIR/higgs/higgs-audio/boson_multimodal/audio_processing/descriptaudiocodec/dac/model/dac.py"
    local dac_quantize="$OVERRIDES_DIR/higgs/higgs-audio/boson_multimodal/audio_processing/descriptaudiocodec/dac/nn/quantize.py"
    if [ ! -f "$engine" ]; then
        echo "WARNING: Higgs serve_engine.py not found for CUDA load patch: $engine"
        return 0
    fi
    HIGGS_ENGINE="$engine" HIGGS_AUDIO_TOKENIZER="$audio_tokenizer" \
        HIGGS_DAC_MODULE="$dac_module" HIGGS_DAC_QUANTIZE="$dac_quantize" \
        "$PYTHON" - 9>&- <<'PY'
import os
from pathlib import Path

path = Path(os.environ["HIGGS_ENGINE"])
text = path.read_text(encoding="utf-8")
old = "        self.model = HiggsAudioModel.from_pretrained(model_name_or_path, torch_dtype=torch_dtype).to(device)\n"
new = """        load_kwargs = {\"torch_dtype\": torch_dtype}
        if device != \"cpu\":
            load_kwargs[\"low_cpu_mem_usage\"] = True
            load_kwargs[\"device_map\"] = {\"\": device}
        self.model = HiggsAudioModel.from_pretrained(model_name_or_path, **load_kwargs)
        if device == \"cpu\":
            self.model = self.model.to(device)
"""
if old in text:
    path.write_text(text.replace(old, new), encoding="utf-8")
    print("Applied Higgs direct CUDA load patch")
elif "device_map" in text and "low_cpu_mem_usage" in text:
    print("Higgs direct CUDA load patch already present")
else:
    raise RuntimeError("Could not patch direct CUDA loading in pinned Higgs source")

tokenizer_path = Path(os.environ["HIGGS_AUDIO_TOKENIZER"])
if not tokenizer_path.is_file():
    print(f"WARNING: Higgs audio tokenizer source not found: {tokenizer_path}")
else:
    tokenizer_text = tokenizer_path.read_text(encoding="utf-8")
    remote = 'AutoModel.from_pretrained("bosonai/hubert_base", trust_remote_code=True)'
    local = '''AutoModel.from_pretrained(
                os.path.join(os.environ["TTS_SERVER_MODELS_DIR"], "hubert_base"),
                trust_remote_code=True,
                local_files_only=True,
            )'''
    if remote in tokenizer_text:
        tokenizer_path.write_text(tokenizer_text.replace(remote, local), encoding="utf-8")
        print("Applied Higgs local-only HuBERT patch")
    elif "TTS_SERVER_MODELS_DIR" in tokenizer_text and "local_files_only=True" in tokenizer_text:
        print("Higgs local-only HuBERT patch already present")
    else:
        raise RuntimeError("Could not apply Higgs local-only HuBERT patch")

dac_path = Path(os.environ["HIGGS_DAC_MODULE"])
if not dac_path.is_file():
    raise RuntimeError(f"Higgs vendored DAC source not found: {dac_path}")
dac_text = dac_path.read_text(encoding="utf-8")
audio_imports = "from audiotools import AudioSignal\nfrom audiotools.ml import BaseModel\n"
codec_import = "from .base import CodecMixin\n"
marker = "# TTS Server: lightweight Higgs encoder/decoder import"
if audio_imports in dac_text and codec_import in dac_text:
    dac_text = dac_text.replace(audio_imports, "")
    dac_text = dac_text.replace(codec_import, "")
    dac_text = dac_text.replace(
        "from torch import nn\n",
        "from torch import nn\n\n"
        f"{marker}\n"
        "BaseModel = nn.Module\n"
        "class CodecMixin:\n"
        "    pass\n",
    )
    dac_text = dac_text.replace("from dac.nn.", "from ..nn.")
    dac_path.write_text(dac_text, encoding="utf-8")
    print("Applied Higgs lightweight vendored-DAC import patch")
elif marker in dac_text:
    print("Higgs lightweight vendored-DAC import patch already present")
else:
    raise RuntimeError("Could not patch the pinned Higgs vendored DAC module")

quantize_path = Path(os.environ["HIGGS_DAC_QUANTIZE"])
quantize_text = quantize_path.read_text(encoding="utf-8")
if "from dac.nn.layers import WNConv1d" in quantize_text:
    quantize_path.write_text(
        quantize_text.replace("from dac.nn.layers import WNConv1d", "from .layers import WNConv1d"),
        encoding="utf-8",
    )
    print("Applied Higgs relative vendored-DAC quantizer import patch")
elif "from .layers import WNConv1d" in quantize_text:
    print("Higgs relative vendored-DAC quantizer import patch already present")
else:
    raise RuntimeError("Could not patch the pinned Higgs vendored DAC quantizer")
PY
}

patch_outetts_minimal_runtime() {
    local source_root="$REPOS_DIR/outetts"
    local dac_root="$OVERRIDES_DIR/outetts/dac"
    if [ ! -d "$source_root/outetts" ] || [ ! -d "$dac_root" ]; then
        echo "ERROR: OuteTTS or DAC source is missing from the minimal override" >&2
        return 1
    fi
    OUTETTS_SOURCE_ROOT="$source_root" OUTETTS_DAC_ROOT="$dac_root" \
        "$PYTHON" - 9>&- <<'PY'
import os
from pathlib import Path

source_root = Path(os.environ["OUTETTS_SOURCE_ROOT"])
dac_root = Path(os.environ["OUTETTS_DAC_ROOT"])

# OuteTTS imports every optional backend at module import time.  The server
# only uses the official HF backend, so defer optional GGUF/EXL2/vLLM imports
# rather than storing their large, mutually incompatible runtimes.
interface_path = source_root / "outetts/version/interface.py"
text = interface_path.read_text(encoding="utf-8")
for line in (
    "import polars as pl\n",
    "from ..models.gguf_model import GGUFModel\n",
    "from ..models.exl2_model import EXL2Model, EXL2ModelAsync\n",
    "from ..models.vllm_model import VLLMModelBatch\n",
    "from ..models.llamacpp_server import LlamaCPPServerModel, LlamaCPPServerAsyncModel\n",
):
    text = text.replace(line, "")
interface_path.write_text(text, encoding="utf-8")

# Default-speaker synthesis does not use OuteTTS's bundled OpenAI-Whisper
# helper.  Make that import lazy so the API can use its separately managed,
# pinned Whisper worker for subtitles without pulling a second ASR runtime.
audio_path = source_root / "outetts/version/v3/audio_processor.py"
text = audio_path.read_text(encoding="utf-8")
import_line = "from ...whisper.transcribe import transcribe_once_word_level"
method_line = '    def create_speaker_from_whisper(self, audio, whisper_model: str = "turbo", device = None):'
lines = [line for line in text.splitlines() if line.strip() != import_line]
try:
    method_index = lines.index(method_line)
except ValueError as exc:
    raise RuntimeError("Could not locate OuteTTS speaker transcription hook") from exc
lines.insert(method_index + 1, "        " + import_line)
for index, line in enumerate(lines):
    if line.lstrip().startswith("seconds = self.audio_codec.load_audio(audio)"):
        lines[index] = "        " + line.lstrip()
audio_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# uroman is imported but unused in the supported path.  MeCab remains present
# for the official Japanese tokenizer path.
preprocess_path = source_root / "outetts/utils/preprocessing.py"
text = preprocess_path.read_text(encoding="utf-8")
text = text.replace("import uroman as ur\n", "")
preprocess_path.write_text(text, encoding="utf-8")

# Let callers bound newly generated audio tokens independently of the large
# bundled speaker prompt.  Transformers otherwise receives max_length even
# when max_new_tokens is present and emits misleading/fragile precedence.
hf_model_path = source_root / "outetts/models/hf_model.py"
text = hf_model_path.read_text(encoding="utf-8")
old = '''        return self.model.generate(
            input_ids,
            max_length=config.max_length,
            temperature=config.sampler_config.temperature,
            repetition_penalty=config.sampler_config.repetition_penalty,
            top_k=config.sampler_config.top_k,
            top_p=config.sampler_config.top_p,
            min_p=config.sampler_config.min_p,
            **config.additional_gen_config,
        )[0].tolist()
'''
new = '''        generate_kwargs = dict(config.additional_gen_config)
        if "max_new_tokens" not in generate_kwargs:
            generate_kwargs["max_length"] = config.max_length
        return self.model.generate(
            input_ids,
            temperature=config.sampler_config.temperature,
            repetition_penalty=config.sampler_config.repetition_penalty,
            top_k=config.sampler_config.top_k,
            top_p=config.sampler_config.top_p,
            min_p=config.sampler_config.min_p,
            **generate_kwargs,
        )[0].tolist()
'''
if old in text:
    text = text.replace(old, new)
elif 'generate_kwargs = dict(config.additional_gen_config)' not in text:
    raise RuntimeError("Could not patch OuteTTS max_new_tokens generation path")
hf_model_path.write_text(text, encoding="utf-8")

# descript-audio-codec's inference model only needs torch and its local
# CodecMixin, but the published package imports the full audiotools training
# suite at module import time.  Keep its load/device behavior locally while
# removing that training-only dependency tree.
init_path = dac_root / "__init__.py"
init_path.write_text(
    '__version__ = "1.0.0"\n'
    '__model_version__ = "latest"\n'
    'from .model.dac import DAC\n'
    'from .model.base import DACFile\n',
    encoding="utf-8",
)

(dac_root / "nn/__init__.py").write_text(
    "# Inference submodules are imported explicitly; omit training losses.\n",
    encoding="utf-8",
)

model_init = dac_root / "model/__init__.py"
model_init.write_text(
    'from .base import CodecMixin, DACFile\n'
    'from .dac import DAC\n',
    encoding="utf-8",
)

base_path = dac_root / "model/base.py"
text = base_path.read_text(encoding="utf-8")
text = text.replace("from audiotools import AudioSignal\n", "AudioSignal = object\n")
base_path.write_text(text, encoding="utf-8")

dac_path = dac_root / "model/dac.py"
text = dac_path.read_text(encoding="utf-8")
text = text.replace("from audiotools import AudioSignal\n", "")
text = text.replace("from audiotools.ml import BaseModel\n", "")
anchor = "from .base import CodecMixin\n"
loader = '''from .base import CodecMixin

class BaseModel(nn.Module):
    @property
    def device(self):
        return next(self.parameters()).device

    @classmethod
    def load(cls, location, *args, strict=False, **kwargs):
        import inspect
        try:
            importer = torch.package.PackageImporter(location)
            model = importer.load_pickle(cls.__name__, f"{cls.__name__}.pth", "cpu")
            model.importer = importer
            return model
        except Exception:
            payload = torch.load(location, map_location="cpu", weights_only=False)
            metadata = dict(payload.get("metadata") or {})
            init_kwargs = dict(metadata.get("kwargs") or {})
            init_kwargs.update(kwargs)
            allowed = set(inspect.signature(cls).parameters)
            init_kwargs = {key: value for key, value in init_kwargs.items() if key in allowed}
            model = cls(*args, **init_kwargs)
            model.load_state_dict(payload["state_dict"], strict=strict)
            model.metadata = metadata
            return model
'''
if anchor not in text:
    raise RuntimeError("Could not locate descript-audio-codec base import")
text = text.replace(anchor, loader)
dac_path.write_text(text, encoding="utf-8")

print("Applied OuteTTS HF-only/local-codec runtime patches")
PY
}

patch_voxtral_vllm_omni_main() {
    local target="$OVERRIDES_DIR/voxtral"
    if [ ! -d "$target" ]; then
        echo "WARNING: Voxtral override directory not found for vllm-omni overlay: $target"
        return 0
    fi
    echo "Overlaying Voxtral-capable vllm-omni build..."
    # Build the pinned overlay into a temp target FIRST, then swap it in only on
    # success — so a mid-install pip failure can't leave the override with
    # vllm_omni removed or half-overwritten. 9>&- closes the inherited flock fd
    # in the child.
    local staging attempt
    staging="$(mktemp -d "$target/.vllm_omni_stage.XXXXXX")"
    attempt=1
    while [ "$attempt" -le 3 ]; do
        if "$PIP" install --no-deps --target="$staging" \
            "git+https://github.com/vllm-project/vllm-omni.git@c0e132d973276e5c1213bd03d930718ff056fd57" 9>&-; then
            break
        fi
        if [ "$attempt" -ge 3 ]; then
            rm -rf "$staging"
            echo "ERROR: vllm-omni overlay install failed after $attempt attempts — left existing override intact"
            return 1
        fi
        echo "Pinned vllm-omni overlay attempt $attempt failed; retrying transient network lookup..."
        find "$staging" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
        if [ "$attempt" -eq 1 ]; then sleep 5; else sleep 15; fi
        attempt=$((attempt + 1))
    done

    if [ -d "$staging/vllm_omni" ]; then
        rm -rf "$target/vllm_omni" "$target"/vllm_omni-*.dist-info
        # Move the freshly-built artifacts over the (now removed) old ones.
        find "$staging" -mindepth 1 -maxdepth 1 -exec mv -f {} "$target/" \;
        rm -rf "$staging"
    else
        rm -rf "$staging"
        echo "ERROR: vllm-omni overlay produced no importable package — left existing override intact"
        return 1
    fi
}

patch_voxtral_mistral_common() {
    local target="$OVERRIDES_DIR/voxtral"
    local wanted="1.11.5"
    local installed staging
    installed="$(PYTHONPATH="$target" "$PYTHON" -c \
        'import importlib.metadata as m; print(m.version("mistral-common"))' \
        2>/dev/null || true)"
    if [ "$installed" = "$wanted" ]; then
        echo "Reusing Transformers-compatible mistral-common $wanted overlay."
        return 0
    fi

    # Transformers 5.15 guards its Mistral tokenizer imports behind
    # mistral-common >=1.11.5. vLLM's older 1.11.2 pin leaves ValidationMode
    # undefined at stage startup, so overlay the smallest compatible release.
    # Stage first and swap atomically so a network failure cannot corrupt the
    # otherwise completed 8+ GB Voxtral runtime.
    echo "Overlaying Transformers-compatible mistral-common $wanted..."
    staging="$(mktemp -d "$target/.mistral_common_stage.XXXXXX")"
    if "$PIP" install --no-deps --target="$staging" \
        "mistral-common==$wanted" 9>&-; then
        rm -rf "$target/mistral_common" "$target"/mistral_common-*.dist-info
        find "$staging" -mindepth 1 -maxdepth 1 -exec mv -f {} "$target/" \;
        rm -rf "$staging"
    else
        rm -rf "$staging"
        echo "ERROR: mistral-common compatibility overlay failed — left existing override intact"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Helper: download model weights from HuggingFace
# ---------------------------------------------------------------------------
download_weights() {
    local repo="$1"
    local local_name="$2"
    local revision="${3:-}"   # optional commit/tag/branch; defaults to main
    local allow_patterns="${4:-}"  # optional | separated snapshot filters
    echo ""
    if [ -n "$revision" ]; then
        echo "Downloading model weights: $repo @ $revision"
    else
        echo "Downloading model weights: $repo"
    fi

    # Read HF token if saved
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        echo "Using saved HuggingFace token"
    elif [ -f "$LEGACY_HF_TOKEN_FILE" ]; then
        export HF_TOKEN="$(cat "$LEGACY_HF_TOKEN_FILE")"
        echo "Using legacy HuggingFace token"
    fi

    # Disk space pre-check
    avail_gb=$(df -BG "$MODELS_DIR" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
    if [ -n "$avail_gb" ] && [ "$avail_gb" -lt 5 ]; then
        echo "WARNING: Less than 5GB free in $MODELS_DIR"
    fi

    # Pass values via environment variables to avoid shell injection in Python heredoc
    export HF_REPO="$repo"
    export HF_LOCAL="$local_name"
    export HF_MODELS_DIR="$MODELS_DIR"
    export HF_REVISION="$revision"
    export HF_ALLOW_PATTERNS="$allow_patterns"
    export TTS_REVISION_LOCK="$SCRIPT_DIR/model_revisions.json"

    "$PYTHON" -c "
import os, gc, json
from pathlib import Path
models_dir = os.environ['HF_MODELS_DIR']
repo = os.environ['HF_REPO']
local_name = os.environ['HF_LOCAL']
local_dir = os.path.join(models_dir, local_name)
revision = os.environ.get('HF_REVISION') or json.loads(Path(os.environ['TTS_REVISION_LOCK']).read_text())[repo]
allow_raw = os.environ.get('HF_ALLOW_PATTERNS') or ''
allow_patterns = [value for value in allow_raw.split('|') if value] or None
os.environ['HF_HOME'] = models_dir
os.environ['HUGGINGFACE_HUB_CACHE'] = os.path.join(models_dir, 'hub')
from huggingface_hub import snapshot_download
token = os.environ.get('HF_TOKEN') or None
download_cache = os.path.join(local_dir, '.cache', 'huggingface', 'download')
if os.path.isdir(download_cache):
    for base, _dirs, files in os.walk(download_cache):
        for name in files:
            if name.endswith('.lock'):
                try:
                    os.remove(os.path.join(base, name))
                except OSError:
                    pass
snapshot_download(
    repo_id=repo,
    cache_dir=os.path.join(models_dir, 'hub'),
    local_dir=local_dir,
    token=token,
    revision=revision,
    allow_patterns=allow_patterns,
    max_workers=max(1, int(os.environ.get('TTS_DOWNLOAD_WORKERS', '2'))),
)
inventory = {str(p.relative_to(local_dir)): p.stat().st_size
             for p in Path(local_dir).rglob('*')
             if p.is_file() and '.cache' not in p.parts and p.name != '.download_complete.json'}
Path(local_dir, '.download_complete.json').write_text(json.dumps({'revision': revision, 'files': inventory}))
gc.collect()
print(f'Download complete: {repo}' + (f' @ {revision}' if revision else ''))
" 9>&-

    unset HF_REPO HF_LOCAL HF_MODELS_DIR HF_REVISION HF_ALLOW_PATTERNS

}

download_url_file() {
    local url="$1"
    local destination="$2"
    if [ -s "$destination" ]; then
        echo "Reusing downloaded runtime asset: $destination"
        return 0
    fi
    mkdir -p "$(dirname "$destination")"
    export TTS_ASSET_URL="$url"
    export TTS_ASSET_DEST="$destination"
    "$PYTHON" -c "
import os, urllib.request
url = os.environ['TTS_ASSET_URL']
dest = os.environ['TTS_ASSET_DEST']
part = dest + '.incomplete'
request = urllib.request.Request(url, headers={'User-Agent': 'TTS-Server/1.0'})
with urllib.request.urlopen(request, timeout=60) as response, open(part, 'wb') as out:
    while True:
        block = response.read(1024 * 1024)
        if not block:
            break
        out.write(block)
        out.flush()
    os.fsync(out.fileno())
os.replace(part, dest)
print(f'Downloaded runtime asset: {dest}')
" 9>&-
    unset TTS_ASSET_URL TTS_ASSET_DEST
}

# ---------------------------------------------------------------------------
# Helper: clone git repo
# ---------------------------------------------------------------------------
clone_repo() {
    local url="$1"
    local dest="$2"
    local revision="${3:-}"   # optional immutable commit SHA / tag
    # Current model installers pass verified immutable commits. Pinned mode
    # fetches and checks out the exact revision and never advances it on rerun.
    # 9>&- prevents a child process from retaining the install lock.
    if [ -n "$revision" ]; then
        # Pinned mode: reuse an exact local checkout without requiring network.
        # This keeps repeatable installs usable when the portable distro is
        # offline and avoids fetching a commit that is already present.
        if [ -d "$dest/.git" ] \
            && [ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" = "$revision" ]; then
            echo "Reusing pinned repo at $dest @ $revision"
            return
        fi
        # Otherwise fetch and check out the exact revision (no silent advance).
        if [ ! -d "$dest/.git" ]; then
            [ -d "$dest" ] && rm -rf "$dest"
            echo "Cloning $url @ $revision..."
            git clone "$url" "$dest" 9>&- 2>&1
        fi
        (cd "$dest" && git fetch --depth 1 origin "$revision" 9>&- 2>&1 \
            && git checkout --quiet "$revision" 9>&- 2>&1)
        return
    fi
    if [ -d "$dest/.git" ]; then
        echo "Repo already cloned at $dest — pulling latest..."
        (cd "$dest" && git pull 9>&- 2>&1) || true
    elif [ -d "$dest" ]; then
        echo "WARNING: $dest exists but is not a git repo — removing and re-cloning..."
        rm -rf "$dest"
        git clone --depth 1 "$url" "$dest" 9>&- 2>&1
    else
        echo "Cloning $url..."
        git clone --depth 1 "$url" "$dest" 9>&- 2>&1
    fi
}

patch_vibevoice_local_tokenizer() {
    local processor_config="$MODELS_DIR/vibevoice/preprocessor_config.json"
    # The pinned VibeVoice processor selects its tokenizer subclass from this
    # path string, so retain "qwen" in the local directory name.
    local tokenizer_dir="$MODELS_DIR/vibevoice-qwen-tokenizer"
    if [ ! -f "$processor_config" ] || [ ! -f "$tokenizer_dir/tokenizer.json" ]; then
        echo "ERROR: VibeVoice processor config or local tokenizer is missing"
        return 1
    fi
    export VIBEVOICE_PROCESSOR_CONFIG="$processor_config"
    export VIBEVOICE_TOKENIZER_DIR="$tokenizer_dir"
    "$PYTHON" - 9>&- <<'PY'
import json
import os
from pathlib import Path

config_path = Path(os.environ["VIBEVOICE_PROCESSOR_CONFIG"])
tokenizer_dir = str(Path(os.environ["VIBEVOICE_TOKENIZER_DIR"]).resolve())
data = json.loads(config_path.read_text(encoding="utf-8"))
data["language_model_pretrained_name"] = tokenizer_dir
config_path.write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(f"Pinned VibeVoice processor to local tokenizer: {tokenizer_dir}")
PY
    unset VIBEVOICE_PROCESSOR_CONFIG VIBEVOICE_TOKENIZER_DIR
}

# ---------------------------------------------------------------------------
# Model-specific installation
# ---------------------------------------------------------------------------
case "$MODEL" in

    kokoro)
        echo "Installing Kokoro packages into base venv..."
        "$PIP" install -q "kokoro==0.9.4"
        download_weights "hexgrad/Kokoro-82M" "kokoro"
        ;;

    dia)
        echo "Installing Dia packages into override..."
        # Keep the runtime source and checkpoint on immutable, tested commits.
        install_override_runtime_only "dia" \
            "git+https://github.com/nari-labs/dia.git@876125e461a03b157ec905b0fe8b57a0f8b9e7a0" \
            "torch==2.6.0" "torchaudio==2.6.0" "triton==3.2.0" \
            "descript-audio-codec>=1.0.0" \
            "huggingface-hub>=0.30.2" "numpy>=2.2.4" \
            "pydantic>=2.11.3" "safetensors>=0.5.3" \
            "soundfile>=0.13.1" "transformers>=4.44,<5.0"
        download_weights "nari-labs/Dia-1.6B-0626" "dia" \
            "ef2795fcc29c5abe6ffc91fd33808588b49bbc66" \
            "config.json|dia-v1.pth|generation_config.json|audio_tokenizer_config.json|tokenizer_config.json|special_tokens_map.json|README.md"
        # Dia otherwise downloads this decoder into a generic HOME cache on
        # first load, where readiness/removal cannot see it.
        download_url_file \
            "https://github.com/descriptinc/descript-audio-codec/releases/download/0.0.1/weights.pth" \
            "$MODELS_DIR/dia-dac/weights_44khz_8kbps_0.0.1.pth"
        ;;

    fish)
        echo "Installing Fish Speech into override..."
        clone_repo "https://github.com/fishaudio/fish-speech" \
            "$REPOS_DIR/fish-speech" \
            "781bf1cd7afef831fc58928a6444a2161449dae5"
        # The upstream package declares its Web UI, training telemetry, dataset,
        # server, and microphone stacks as mandatory dependencies. The API
        # worker imports none of those. Install only its exercised inference
        # runtime, then add the pinned source itself without declared extras.
        install_override_runtime_only "fish" "$REPOS_DIR/fish-speech" \
            "torch==2.8.0" "torchaudio==2.8.0" \
            "transformers==4.57.3" \
            "lightning>=2.1.0" "hydra-core>=1.3.2" \
            "einops>=0.7.0" "loguru>=0.6.0" "loralib>=0.1.2" \
            "natsort==8.4.0" "tiktoken>=0.8.0" \
            "pyrootutils>=1.0.4" "descript-audio-codec>=1.0.0" \
            "pydantic==2.9.2" "soundfile" "tqdm" "click"
        download_weights "fishaudio/openaudio-s1-mini" "fish-speech" \
            "f4b445029346701e082b60bb63fcc2d1bb17a0e2"
        # Remove files produced by the superseded Qwen AutoTokenizer conversion.
        # They are not part of this pinned S1 snapshot and can silently select
        # the incompatible S2 token IDs if a newer source is used accidentally.
        rm -f "$MODELS_DIR/fish-speech/tokenizer.json" \
              "$MODELS_DIR/fish-speech/tokenizer_config.json" \
              "$MODELS_DIR/fish-speech/chat_template.jinja"
        legacy_tokenizer_dir="$(realpath -m "$MODELS_DIR/fish-tokenizer-source")"
        case "$legacy_tokenizer_dir" in
            "$MODELS_DIR"/*) [ -d "$legacy_tokenizer_dir" ] && rm -rf "$legacy_tokenizer_dir" ;;
            *) echo "WARNING: Refusing to remove unexpected Fish tokenizer path: $legacy_tokenizer_dir" ;;
        esac
        unset legacy_tokenizer_dir
        # OpenAudio S1-mini uses Fish's own tiktoken vocabulary.  The later S2
        # source changed to AutoTokenizer, which reassigns these special-token
        # IDs and produces unintelligible audio with the S1 checkpoint.  Keep the
        # last tested S1 source revision above and validate its exact ID mapping.
        export FISH_MODEL_DIR="$MODELS_DIR/fish-speech"
        PYTHONPATH="$OVERRIDES_DIR/fish" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
            "$PYTHON" - <<'PY'
import json
import os
from pathlib import Path

from fish_speech.tokenizer import FishTokenizer

model_dir = Path(os.environ["FISH_MODEL_DIR"])
expected = json.loads((model_dir / "special_tokens.json").read_text(encoding="utf-8"))
tokenizer = FishTokenizer.from_pretrained(model_dir)
actual = tokenizer.all_special_tokens_with_ids
if actual != expected:
    raise RuntimeError("Fish tokenizer IDs do not match special_tokens.json")
if len(tokenizer.semantic_id_to_token_id) != 4096:
    raise RuntimeError("Fish tokenizer does not contain all 4096 semantic tokens")
print(
    "Validated Fish S1 tokenizer "
    f"({tokenizer.vocab_size} base; {tokenizer.num_special_tokens} special tokens)"
)
PY
        unset FISH_MODEL_DIR
        ;;

    f5)
        echo "Installing F5-TTS into override..."
        install_override "f5" "torch==2.8.0" "torchaudio==2.8.0" "torchvision==0.23.0" "f5-tts" "transformers>=4.44,<5.0"
        # F5TTS() defaults to F5TTS_v1_Base. Avoid downloading the legacy
        # F5TTS_Base, BigVGAN, and no-zero-init training variants.
        download_weights "SWivid/F5-TTS" "f5-tts" "" \
            "F5TTS_v1_Base/model_1250000.safetensors|F5TTS_v1_Base/vocab.txt"
        ;;

    bark)
        echo "Installing Coqui TTS (for Bark) into override..."
        install_override "coqui" "torch==2.8.0" "torchaudio==2.8.0" \
            "coqui-tts[all]" "transformers>=4.47,<5.0"
        # Coqui's Bark implementation is wired to this five-file bundle
        # (coarse_2.pt, fine_2.pt, text_2.pt, config.json, tokenizer.pth).
        # Download it directly into TTS_HOME's model directory so first load
        # does not duplicate the weights.  suno/bark has hundreds of files and
        # is not the directory consumed by TTS(".../bark").
        download_weights "erogol/bark" "coqui/tts/tts_models--multilingual--multi-dataset--bark"
        # The runtime weights do not contain speaker histories. Fetch only the
        # official v2 prompt tensors, not suno/bark's large checkpoints or its
        # v1 prompt set.
        download_weights "suno/bark" "bark-voices" "" "speaker_embeddings/v2/*"
        ;;

    xtts)
        echo "Installing Coqui TTS (for XTTS) into override..."
        install_override "coqui" "torch==2.8.0" "torchaudio==2.8.0" \
            "coqui-tts[all]" "transformers>=4.47,<5.0"
        download_weights "coqui/XTTS-v2" "xtts-v2"
        ;;

    chatterbox)
        echo "Installing Chatterbox into override..."
        # resemble-perth still imports pkg_resources, removed in setuptools 81.
        install_override "chatterbox" "chatterbox-tts" "setuptools<81"
        # ChatterboxTTS (English) loads exactly these five assets. Exclude the
        # multilingual/VC checkpoints and duplicate legacy .pt weights.
        download_weights "ResembleAI/chatterbox" "chatterbox" "" \
            "ve.safetensors|t3_cfg.safetensors|s3gen.safetensors|tokenizer.json|conds.pt"
        ;;

    qwen)
        echo "Installing Qwen's quantization/processor runtime into a minimal override..."
        # transformers constructs Qwen's video processor even for text-only
        # TTS. Match the shared torch 2.6.0 runtime exactly and keep both
        # packages isolated from the base venv.
        "$PYTHON" -c "import torch; assert torch.__version__.split('+')[0] == '2.6.0', 'Run setup.sh to repair the pinned base PyTorch before installing Qwen'"
        install_override_minimal "qwen" \
            "bitsandbytes==0.50.0" \
            "torchvision==0.21.0"
        download_weights "Qwen/Qwen2.5-Omni-7B" "qwen-omni" \
            "ae9e1690543ffd5c0221dc27f79834d0294cba00" \
            "added_tokens.json|chat_template.json|config.json|generation_config.json|merges.txt|model-*.safetensors|model.safetensors.index.json|preprocessor_config.json|special_tokens_map.json|spk_dict.pt|tokenizer.json|tokenizer_config.json|vocab.json"
        ;;

    vibevoice)
        echo "Installing VibeVoice's compatibility runtime into a minimal override..."
        # Reuse the app-owned Torch 2.5/CUDA stack. Only the model's pinned
        # Transformers-era compatibility layer and missing pure-Python runtime
        # packages live here; UI/WebRTC packages are not used by API workers.
        install_override_minimal "vibevoice" \
            "transformers==4.51.3" "tokenizers==0.21.4" \
            "huggingface-hub==0.30.2" "accelerate==1.6.0" \
            "diffusers==0.33.1" "ml-collections==1.1.0" \
            "absl-py==2.5.0"
        clone_repo "https://github.com/vibevoice-community/VibeVoice" \
            "$OVERRIDES_DIR/vibevoice/VibeVoice" \
            "631804b9c1f042e381207fe87c54603fe6accbc1"
        download_weights "microsoft/VibeVoice-1.5B" "vibevoice" \
            "c00898d257e6b46004e3e2866a47534085fb685a" \
            "config.json|preprocessor_config.json|model-*.safetensors|model.safetensors.index.json"
        download_weights "Qwen/Qwen2.5-1.5B" "vibevoice-qwen-tokenizer" \
            "8faed761d45a263340a0528343f099c05c9a4323" \
            "tokenizer.json|tokenizer_config.json|merges.txt|vocab.json"
        patch_vibevoice_local_tokenizer
        ;;

    higgs)
        echo "Installing Higgs Audio into override..."
        # Reuse the app-owned base torch/torchaudio/CUDA stack. The explicit
        # minimal set below isolates only conflicting and missing imports.
        # Deps from higgs-audio/requirements.txt — listed explicitly so the
        # --no-deps prevents pip from duplicating the CUDA runtime.
        install_override_minimal "higgs" \
            "transformers==4.46.3" "tokenizers==0.20.3" \
            "huggingface-hub==0.26.5" \
            "dacite==1.9.2" "loguru==0.7.3" "pandas==2.3.3" \
            "pytz==2025.2" "tzdata==2025.3" \
            "omegaconf==2.3.0" "antlr4-python3-runtime==4.9.3" \
            "vector-quantize-pytorch==1.31.1" \
            "torch-einops-utils==0.1.16" "einops==0.8.1" \
            "einx==0.4.3" "frozendict==2.4.6"
        clone_repo "https://github.com/boson-ai/higgs-audio" \
            "$OVERRIDES_DIR/higgs/higgs-audio" \
            "05a145bb490501b534563bf51bf2f7aa2326b271"
        patch_higgs_cuda_load
        # Pin to pre-trfms-5 revision (2025-07-28). The 2026-04-04 'trfms-support'
        # commit on HF flattened the config (model_type=higgs_audio_v2, no nested
        # text_config), but boson_multimodal still expects nested text_config.
        download_weights "bosonai/higgs-audio-v2-generation-3B-base" "higgs-audio" \
            "10840182ca4ad5d9d9113b60b9bb3c1ef1ba3f84" \
            "config.json|generation_config.json|model-*.safetensors|model.safetensors.index.json|special_tokens_map.json|tokenizer.json|tokenizer_config.json"
        download_weights "bosonai/higgs-audio-v2-tokenizer" "higgs-audio-tokenizer" \
            "9d4988fbd4ad07b4cac3a5fa462741a41810dbec" \
            "config.json|model.pth"
        # Hubert base model used by the audio tokenizer's semantic_techer.
        # Pre-download so worker doesn't hang on first use trying to fetch it.
        download_weights "bosonai/hubert_base" "hubert_base" \
            "d7a7039bd0d93005f6532853db1934722d9a5e02" \
            "config.json|model.safetensors"
        ;;

    whisper)
        echo "Whisper is pre-installed in the base venv."
        echo "Ensuring the default base model is stored in the portable model directory..."
        TTS_MODELS_DIR="$MODELS_DIR" "$PYTHON" -c "
import gc, os
import whisper
model = whisper.load_model('base', device='cpu', download_root=os.path.join(os.environ['TTS_MODELS_DIR'], 'whisper'))
del model
gc.collect()
print('Whisper base model is ready')
" 9>&-
        ;;

    speecht5)
        echo "Installing SpeechT5 into base venv..."
        "$PIP" install -q sentencepiece pyarrow
        download_weights "microsoft/speecht5_tts" "speecht5"
        download_weights "microsoft/speecht5_hifigan" "speecht5-hifigan"
        export HF_MODELS_DIR="$MODELS_DIR"
        # Download the official auto-converted parquet instead of executing the
        # legacy dataset script (unsupported by modern `datasets`). Pin the
        # conversion commit so row indices used by the named voices stay stable.
        "$PYTHON" -c "
import os
from huggingface_hub import hf_hub_download
models_dir = os.environ['HF_MODELS_DIR']
path = hf_hub_download(
    repo_id='Matthijs/cmu-arctic-xvectors',
    repo_type='dataset',
    revision='36e87b347a6a70f0420445b02ec40c55556f9ed7',
    filename='default/validation/0000.parquet',
    local_dir=os.path.join(models_dir, 'speecht5-xvectors'),
)
print(f'CMU Arctic xvectors downloaded: {path}')
" 2>&1
        unset HF_MODELS_DIR
        echo "SpeechT5 installed (with speaker embeddings)."
        ;;

    parler)
        echo "Installing Parler-TTS into override..."
        # Pin the official source so portable re-installs cannot drift with main.
        install_override "parler" "torch==2.8.0" "torchaudio==2.8.0" "torchvision==0.23.0" "git+https://github.com/huggingface/parler-tts.git@d108732cd57788ec86bc857d99a6cabd66663d68"
        download_weights "parler-tts/parler-tts-mini-v1.1" "parler-tts" \
            "fbb2dd281092c5b414ef29cf9d8895f386f1feef"
        # The Parler checkpoint embeds FLAN-T5 weights, but its description
        # tokenizer is referenced by repo id. Keep just that tokenizer local.
        download_weights "google/flan-t5-large" "parler-desc-tokenizer" \
            "0613663d0d48ea86ba8cb3d7a44f0f65dc596a2a" \
            "config.json|tokenizer*|spiece.model|special_tokens_map.json"
        ;;

    outetts)
        echo "Installing pinned OuteTTS 1.0 inference runtime..."
        # Reuse the app-owned torch 2.5.1+cu121/torchaudio stack.  The upstream
        # package's declared dependencies include a second Torch/CUDA stack,
        # llama.cpp, UI playback, and OpenAI Whisper even though this worker
        # exclusively uses the HF backend and the server's own Whisper worker.
        install_override_minimal "outetts" \
            "transformers==4.52.3" "tokenizers==0.21.4" \
            "huggingface-hub==0.30.2" "loguru==0.7.3" \
            "ftfy==6.3.1" "wcwidth==0.8.2" \
            "pyloudnorm==0.2.0" "einops==0.8.2" \
            "mecab-python3==1.0.10" "unidic-lite==1.0.8" \
            "descript-audio-codec==1.0.0"
        clone_repo "https://github.com/edwko/OuteTTS.git" \
            "$REPOS_DIR/outetts" \
            "f5eac6e70d792844c6a6959d900a47af2c061a5b"
        patch_outetts_minimal_runtime
        download_weights "OuteAI/OuteTTS-1.0-0.6B" "outetts-1.0-0.6b" \
            "e7bcd87b0ca47fd8c46317c8f745a5e4e19c7b5c" \
            "*.json|*.safetensors|*.txt"
        download_weights "ibm-research/DAC.speech.v1.0" "outetts-dac" \
            "1ea7f64cd0678415e2d8c32d67b190722cb9b149" \
            "weights_24khz_1.5kbps_v1.0.pth"
        ;;

    vits)
        echo "VITS uses the Coqui TTS package (shared with Bark/XTTS)."
        echo "Model weights download automatically on first use."
        install_override "coqui" "torch==2.8.0" "torchaudio==2.8.0" \
            "coqui-tts[all]" "transformers>=4.47,<5.0"
        ;;

    edge)
        echo "Installing Edge-TTS package into base venv..."
        "$PIP" install -q edge-tts
        echo "Edge-TTS is cloud-based — no model weights to download."
        ;;

    voxtral)
        echo "Installing pinned Voxtral (vLLM + vllm-omni) runtime..."
        # vLLM is large (~2GB) and pulls its own torch/xformers/flash-attn.
        # Install into the override so it cannot contaminate other models.
        # mistral_common >= 1.10 required for SpeechRequest/MistralTokenizer.
        # PyPI vllm-omni 0.20.0 cannot inspect VoxtralTTSForConditionalGeneration,
        # so overlay the tested upstream build after dependency resolution.
        # vLLM-Omni's pinned May 17 source was tested on vLLM 0.21.0; keep its
        # matching May package and mistral-common floor exact.  Newer releases
        # currently advance to a different Torch/CUDA ABI and are not portable
        # drop-in upgrades for this worker.
        install_override "voxtral" \
            "vllm==0.21.0" \
            "vllm-omni==0.20.0" \
            "mistral_common==1.11.2"
        patch_voxtral_mistral_common
        patch_voxtral_vllm_omni_main
        download_weights "mistralai/Voxtral-4B-TTS-2603" "voxtral-4b-tts" \
            "b81be46c3777f88621676791b512bb01dc1cb970" \
            "consolidated.safetensors|params.json|tekken.json|voice_embedding/*"
        ;;

    voxcpm2)
        echo "Installing pinned, inference-only VoxCPM2 runtime..."
        # VoxCPM's declared dependency set includes Gradio, ModelScope,
        # datasets/FunASR, normalization, and denoiser tooling. The local
        # denoiser-free inference path needs only the source package and
        # einops beyond the app's existing Torch/Transformers/audio stack.
        install_override_minimal "voxcpm2" \
            "voxcpm==2.0.3" \
            "einops==0.8.1"
        download_weights "openbmb/VoxCPM2" "voxcpm2" \
            "bffb3df5a29440629464e5e839f4d214c8714c3d" \
            "audiovae.pth|config.json|model.safetensors|special_tokens_map.json|tokenization_voxcpm2.py|tokenizer.json|tokenizer_config.json"
        ;;

    csm)
        echo "Using the app's native Transformers CSM runtime..."
        # CsmForConditionalGeneration is already present in the base runtime;
        # do not store a second Torch/CUDA/Transformers stack for one model.
        "$PYTHON" -c "from transformers import CsmForConditionalGeneration, AutoProcessor; print('CSM runtime ready')"
        download_weights "sesame/csm-1b" "csm-1b" \
            "c92a71e1c419772e25be7dc14d952c2521a740ab" \
            "chat_template.jinja|config.json|generation_config.json|preprocessor_config.json|special_tokens_map.json|tokenizer.json|tokenizer_config.json|transformers-00001-of-00002.safetensors|transformers-00002-of-00002.safetensors|transformers.safetensors.index.json"
        ;;

    orpheus)
        echo "Installing the minimal Orpheus 3B inference runtime..."
        # The worker already has the app-owned Torch/CUDA, Transformers, NumPy,
        # and Hugging Face runtime. Keep only the tiny codec package and its one
        # missing tensor helper in the override instead of duplicating CUDA.
        install_override_minimal "orpheus" "snac==1.2.1" "einops==0.8.1"
        PYTHONPATH="$OVERRIDES_DIR/orpheus" "$PYTHON" -c \
            "from snac import SNAC; import einops; print('Orpheus codec runtime ready')"

        # Canopy's original repository also contains ~41.5 GB of optimizer/FSDP
        # training state and stores inference weights in FP32. This pinned
        # Apache-2.0 Transformers repack contains the same finetune in FP16 and
        # is the portable inference representation used by this app.
        download_weights "unsloth/orpheus-3b-0.1-ft" "orpheus-3b" \
            "eae2b6e5e429c81b95ac42a883ac64f126583d43" \
            "chat_template.jinja|config.json|generation_config.json|model-00001-of-00002.safetensors|model-00002-of-00002.safetensors|model.safetensors.index.json|special_tokens_map.json|tokenizer.json|tokenizer_config.json"
        download_weights "hubertsiuzdak/snac_24khz" "snac-24khz" \
            "d73ad176a12188fcf4f360ba3bf2c2fbbe8f58ec" \
            "config.json|pytorch_model.bin"
        ;;

    all)
        echo "Installing ALL models..."
        # Release parent lock so each child can acquire its own sequentially
        exec 9>&-
        failed=""
        for m in kokoro dia fish f5 bark xtts chatterbox qwen vibevoice higgs whisper speecht5 parler outetts vits edge voxtral voxcpm2 csm orpheus; do
            echo ""
            echo "--- $m ---"
            bash "$0" "$m" || failed="$failed $m"
        done
        if [ -n "$failed" ]; then
            echo ""
            echo "FAILED models:$failed"
            exit 1
        fi
        exit 0
        ;;

    *)
        echo "ERROR: Unknown model '$MODEL'"
        echo "Models: kokoro, dia, fish, f5, bark, xtts, chatterbox, qwen, vibevoice, higgs, whisper, speecht5, parler, outetts, vits, edge, voxtral, voxcpm2, csm, orpheus, all"
        exit 1
        ;;
esac

# Package archives and build scratch are reclaimable once this model's
# dependencies are installed.  Keep model weights and the HuggingFace runtime
# metadata, but do not let pip/temp caches silently grow the portable VHDX.
echo "Clearing completed installer download caches..."
"$PIP" cache purge >/dev/null 2>&1 || true
# Invocation scratch is removed by the EXIT trap.

echo ""
echo "============================================"
echo "  $MODEL installation complete!"
echo "============================================"
