"""
TTS Worker - A single-model inference server launched as a subprocess by the gateway.

Usage: python tts_worker.py --model kokoro --port 8102 --device cuda:0

Each worker loads exactly ONE model and exposes:
  GET  /health  - Status, device, VRAM info
  POST /infer   - Generate audio from text (returns base64 numpy array + sample rate)
  POST /load    - (Re)load the model
  POST /unload  - Unload model from GPU, process stays alive
"""

import argparse
import base64
import gc
import hashlib
import io
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import torch

# ---------------------------------------------------------------------------
# Bootstrap: inject config before anything else
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()

# Ensure our project root is on sys.path so config, audio_profiles, etc. import
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    VENV_DIR, OVERRIDES_DIR, REPOS_DIR, MODELS_DIR, VOICE_DIR, OUTPUT_DIR,
    PROJECTS_OUTPUT, MODEL_OVERRIDE_MAP, setup_environment,
)

setup_environment()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _inject_venv(model: str) -> None:
    """Inject shared base venv + optional model-specific override into sys.path.

    The base venv contains PyTorch, transformers (latest), and common packages.
    Override directories contain only conflicting packages (e.g. pinned
    transformers versions) and are injected BEFORE the base so they win.
    """
    env_prefix: list[str] = []

    def _prepend(path: Path, label: str) -> None:
        path_str = str(path)
        if path.exists() and path_str not in sys.path:
            sys.path.insert(0, path_str)
            logger.info("Injected %s: %s", label, path)
        if path.exists() and path_str not in env_prefix:
            env_prefix.insert(0, path_str)

    # 1. Inject base venv site-packages
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    base_sp = VENV_DIR / "lib" / py_ver / "site-packages"
    _prepend(base_sp, "base venv")

    # 2. Inject override BEFORE base (so conflicting packages win)
    override_name = MODEL_OVERRIDE_MAP.get(model)
    if override_name:
        override_dir = OVERRIDES_DIR / override_name
        _prepend(override_dir, "override")

    # 3. Extra source dirs for models with cloned repos
    extra_dirs = {
        "fish": REPOS_DIR / "fish-speech",
        "vibevoice": OVERRIDES_DIR / "vibevoice" / "VibeVoice",
        "higgs": OVERRIDES_DIR / "higgs" / "higgs-audio",
        "outetts": REPOS_DIR / "outetts",
    }
    extra = extra_dirs.get(model)
    if extra:
        _prepend(extra, "extra source dir")

    # Some runtimes (notably vLLM) spawn Python subprocesses for architecture
    # inspection. Those subprocesses do not inherit this process' sys.path, so
    # keep PYTHONPATH in sync with the same model-specific override ordering.
    if env_prefix:
        existing = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
        combined: list[str] = []
        for path_str in env_prefix + existing:
            if path_str not in combined:
                combined.append(path_str)
        os.environ["PYTHONPATH"] = os.pathsep.join(combined)


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------
_model_name: str = ""
_device: str = "cuda:0"
_precision: str | None = None  # "fp32", "fp16", "bf16", or None (auto)
_model_obj = None  # the loaded model object (varies per model)
_loaded: bool = False
_load_lock = threading.Lock()
_whisper_access_times: dict = {}  # {size: last_access_time} for LRU eviction


# ============================================================
# Model loading
# ============================================================
def _resolve_precision(precision: str | None, device: str) -> "torch.dtype":
    """Resolve precision string to a torch dtype.

    Args:
        precision: "fp32", "fp16", "bf16", or None (auto).
        device: Target device string — auto uses fp16 on CUDA, fp32 on CPU.
    """
    import torch
    if precision == "fp16":
        return torch.float16
    if precision == "bf16":
        return torch.bfloat16
    if precision == "fp32":
        return torch.float32
    # Auto: fp16 on CUDA (saves VRAM), fp32 on CPU
    return torch.float16 if "cuda" in device else torch.float32


def _resolve_device(device_str: str) -> str:
    """Validate and return the device string."""
    import torch
    if device_str == "cpu":
        return "cpu"
    if not torch.cuda.is_available():
        logger.warning("CUDA not available in this venv's torch (%s), using CPU",
                       torch.__version__)
        return "cpu"
    if device_str.startswith("cuda:"):
        # Guard the parse: "cuda:" / "cuda:abc" would otherwise raise a bare
        # ValueError/IndexError and crash load_model with an opaque traceback.
        try:
            idx = int(device_str.split(":")[1])
        except (ValueError, IndexError):
            raise ValueError(f"Invalid CUDA device string {device_str!r}")
        if idx < torch.cuda.device_count():
            return device_str
        raise ValueError(
            f"Device {device_str} not found (have {torch.cuda.device_count()} GPUs)"
        )
    # Note: managed workers always receive cuda:0 (the physical GPU is selected
    # via CUDA_VISIBLE_DEVICES in worker_manager.py), so the index-bounds branch
    # above is effectively standalone-only.
    return "cuda:0"


def load_model() -> None:
    """Load the model onto the configured device."""
    global _model_obj, _loaded, _device

    if _loaded:
        logger.info("Model %s already loaded", _model_name)
        return

    device = _resolve_device(_device)
    _device = device  # write back so health checks and inference use the actual device
    logger.info("Loading model %s on %s...", _model_name, device)
    start = time.time()

    if _model_name == "xtts":
        from TTS.api import TTS
        xtts_dir = MODELS_DIR / "xtts-v2"
        _model_obj = TTS(
            model_path=str(xtts_dir / "model.pth"),
            config_path=str(xtts_dir / "config.json"),
            speakers_file_path=str(xtts_dir / "speakers_xtts.pth"),
        ).to(device)

    elif _model_name == "bark":
        from TTS.api import TTS
        import torch as _torch

        # Bark loads 3 checkpoints (text=5GB, coarse=3.7GB, fine=3.5GB).
        # Loading all to CPU simultaneously peaks at 20+ GB RAM, which can
        # OOM-kill the WSL2 process (default 50% system RAM ≈ 32 GB).
        # Fix: override load_bark_models to load each sub-model directly to
        # GPU and free the CPU copy before loading the next.
        target = _torch.device(device)
        dtype = _resolve_precision(_precision, device)
        def _streaming_load_bark_models(bark_self):
            from TTS.tts.layers.bark.model import GPT
            from TTS.tts.layers.bark.model_fine import FineGPT
            from TTS.tts.layers.bark.load_model import GPTConfig, FineGPTConfig

            for model_type in ("text", "coarse", "fine"):
                ckpt = bark_self.config.LOCAL_MODEL_PATHS[model_type]
                logger.info("Bark loading '%s' from %s", model_type, ckpt)

                # Memory-efficient loading: extract only the state dict and
                # model args from the checkpoint, then discard the rest
                # (optimizer state, training metadata).  Coqui's default
                # torch.load(weights_only=False) keeps everything in RAM.
                # SECURITY: weights_only=False uses pickle and executes
                # arbitrary code embedded in the checkpoint. weights_only=True is
                # NOT a drop-in here because we read checkpoint["model_args"]
                # (non-tensor metadata the restricted unpickler rejects).
                # Mitigation requires converting Bark weights to safetensors at
                # install time and/or pinning + hash-verifying the HF revision
                # (handled install-side). Do not change the load mechanism here.
                checkpoint = _torch.load(ckpt, map_location="cpu", weights_only=False)
                model_args = checkpoint["model_args"]
                if "input_vocab_size" not in model_args:
                    model_args["input_vocab_size"] = model_args.pop("vocab_size")
                    model_args["output_vocab_size"] = model_args["input_vocab_size"]

                if model_type == "fine":
                    conf = FineGPTConfig(**model_args)
                    bark_self.config.fine_config = conf
                    model = FineGPT(conf)
                else:
                    conf = GPTConfig(**model_args)
                    if model_type == "text":
                        bark_self.config.semantic_config = conf
                    else:
                        bark_self.config.coarse_config = conf
                    model = GPT(conf)

                state_dict = checkpoint["model"]
                del checkpoint  # free the bulk of the checkpoint RAM NOW
                gc.collect()

                # Strip prefix if present
                for k in list(state_dict.keys()):
                    if k.startswith("_orig_mod."):
                        state_dict[k[len("_orig_mod."):]] = state_dict.pop(k)

                model.load_state_dict(state_dict, strict=False)
                del state_dict
                gc.collect()

                model.eval()
                model = model.to(dtype=dtype, device=target)

                attr = "semantic_model" if model_type == "text" else f"{model_type}_model"
                setattr(bark_self, attr, model)
                del model
                gc.collect()
                if _torch.cuda.is_available():
                    _torch.cuda.empty_cache()
                logger.info("Bark '%s' -> %s (%s)", model_type, device, dtype)

        # Patch before constructing — TTS() calls load_bark_models internally
        from TTS.tts.models.bark import Bark
        Bark.load_bark_models = _streaming_load_bark_models

        _model_obj = TTS("tts_models/multilingual/multi-dataset/bark")
        # Sub-models are already on GPU via our patched loader.
        # Also move EnCodec to GPU (loaded to CPU during __init__).
        bark_model = _model_obj.synthesizer.tts_model
        if hasattr(bark_model, 'encodec') and bark_model.encodec is not None:
            bark_model.encodec = bark_model.encodec.to(target)
            logger.info("Bark EnCodec -> %s", device)
        _model_obj.synthesizer.device = str(target)

    elif _model_name == "fish":
        import torch as _torch
        from fish_speech.models.text2semantic.inference import launch_thread_safe_queue
        from fish_speech.models.dac.inference import load_model as _load_dac
        from fish_speech.inference_engine import TTSInferenceEngine

        fish_dir = MODELS_DIR / "fish-speech"
        fish_dtype = _resolve_precision(_precision, device)
        # Fish Speech prefers bfloat16 on CUDA when no explicit precision set
        if _precision is None and "cuda" in device:
            fish_dtype = _torch.bfloat16

        llama_queue = launch_thread_safe_queue(
            checkpoint_path=str(fish_dir),
            device=device,
            precision=fish_dtype,
            compile=False,
        )
        # DAC codec (current fish-speech API)
        codec_path = fish_dir / "codec.pth"
        if not codec_path.exists():
            # Fallback for older weight downloads
            legacy = fish_dir / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
            if legacy.exists():
                raise FileNotFoundError(
                    "Found legacy VQGAN checkpoint but not codec.pth. "
                    "Re-download weights or use openaudio-s1-mini model."
                )
            raise FileNotFoundError(f"Codec checkpoint not found at {codec_path}")
        decoder_model = _load_dac(
            config_name="modded_dac_vq",
            checkpoint_path=str(codec_path),
            device=device,
        )
        _model_obj = TTSInferenceEngine(
            llama_queue=llama_queue,
            decoder_model=decoder_model,
            precision=_precision,
            compile=False,
        )

    elif _model_name == "kokoro":
        from kokoro import KModel, KPipeline
        root = MODELS_DIR / "kokoro"
        model = KModel(repo_id="hexgrad/Kokoro-82M", config=str(root / "config.json"),
                       model=str(root / "kokoro-v1_0.pth")).to(device).eval()
        _model_obj = {"model": model, "pipelines": {
            "a": KPipeline(lang_code="a", model=model, repo_id="hexgrad/Kokoro-82M", device=device)
        }}

    elif _model_name == "chatterbox":
        from chatterbox import ChatterboxTTS
        chatterbox_dir = MODELS_DIR / "chatterbox"
        required = (
            "ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors",
            "tokenizer.json", "conds.pt",
        )
        missing = [name for name in required if not (chatterbox_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(
                "Chatterbox English assets are missing: " + ", ".join(missing)
            )
        _model_obj = ChatterboxTTS.from_local(chatterbox_dir, device=device)

    elif _model_name == "f5":
        # Block torchcodec - it tries to load FFmpeg shared DLLs that
        # aren't available in our static ffmpeg build. Make importlib
        # unable to find it so transformers falls back to soundfile.
        import importlib.util
        _orig_find_spec = importlib.util.find_spec
        def _patched_find_spec(name, *a, **kw):
            if name == "torchcodec":
                return None
            return _orig_find_spec(name, *a, **kw)
        importlib.util.find_spec = _patched_find_spec
        try:
            from f5_tts.api import F5TTS
            f5_dir = MODELS_DIR / "f5-tts" / "F5TTS_v1_Base"
            checkpoint = f5_dir / "model_1250000.safetensors"
            vocab = f5_dir / "vocab.txt"
            if not checkpoint.is_file() or not vocab.is_file():
                raise FileNotFoundError(
                    "F5-TTS selected checkpoint is missing. "
                    "Run POST /api/setup/install/f5."
                )
            _model_obj = F5TTS(
                model="F5TTS_v1_Base",
                ckpt_file=str(checkpoint),
                vocab_file=str(vocab),
                device=device,
                hf_cache_dir=str(MODELS_DIR / "hub"),
            )
        finally:
            importlib.util.find_spec = _orig_find_spec

    elif _model_name == "dia":
        from dia.model import Dia
        import torch as _torch
        dia_dir = MODELS_DIR / "dia"
        if not (dia_dir / "config.json").is_file():
            raise FileNotFoundError(
                "Dia's local checkpoint is missing. Run POST /api/setup/install/dia."
            )
        _dia_dtype = "bfloat16" if "cuda" in device else "float32"
        checkpoint = dia_dir / "dia-v1.pth"
        if not checkpoint.is_file():
            raise FileNotFoundError(
                "Dia's selected local checkpoint is missing. "
                "Run POST /api/setup/install/dia."
            )
        _model_obj = Dia.from_local(
            str(dia_dir / "config.json"),
            str(checkpoint),
            compute_dtype=_dia_dtype,
            device=_torch.device(device),
            load_dac=False,
        )
        import dac as _dac
        dac_path = MODELS_DIR / "dia-dac" / "weights_44khz_8kbps_0.0.1.pth"
        if not dac_path.is_file():
            raise FileNotFoundError(
                "Dia's local DAC decoder is missing. Run POST /api/setup/install/dia."
            )
        _model_obj.dac_model = _dac.DAC.load(str(dac_path)).to(device).eval()
        _model_obj.load_dac = True

    elif _model_name == "qwen":
        from transformers import (
            Qwen2_5OmniForConditionalGeneration,
            Qwen2_5OmniProcessor,
        )
        import torch
        # Prefer local snapshot — passing the HF repo ID triggers a hub re-fetch
        # at "Fetching N files" that hangs for 10+ min even when all weights
        # are local (HF Hub safety re-validation against latest revision).
        local_qwen = MODELS_DIR / "qwen-omni"
        if not (local_qwen / "config.json").is_file():
            raise FileNotFoundError(
                "Qwen Omni's local checkpoint is missing. "
                "Run POST /api/setup/install/qwen."
            )
        model_id = str(local_qwen)
        # Qwen ships its two built-in speaker embeddings in a tiny PyTorch
        # archive while the actual model weights are safetensors.  Transformers
        # 5.x rejects every torch.load call on Torch < 2.6 because of
        # CVE-2025-32434, even with weights_only=True.  Do not weaken that guard
        # globally: replace only this model class' speaker loader, require the
        # exact file from our pinned HF revision, and verify its digest before
        # allowing the restricted load.
        spk_path = (local_qwen / "spk_dict.pt").resolve()
        expected_spk_sha256 = (
            "6a05609b28f5d42b7b748f0f07592545"
            "c8f1f6885b9ae8fff64baf56e86b2a18"
        )
        if not spk_path.is_file():
            raise FileNotFoundError(
                "Qwen Omni's pinned speaker map is missing. "
                "Run POST /api/setup/install/qwen."
            )

        def _load_pinned_qwen_speakers(self, path):
            resolved = Path(path).resolve()
            if resolved != spk_path:
                raise ValueError(f"Refusing unpinned Qwen speaker map: {resolved}")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if digest != expected_spk_sha256:
                raise ValueError(
                    "Qwen speaker-map integrity check failed; reinstall qwen"
                )
            speaker_data = torch.load(
                resolved, map_location="cpu", weights_only=True
            )
            if not isinstance(speaker_data, dict):
                raise ValueError("Qwen speaker map is not a dictionary")
            self.speaker_map.update(speaker_data)
            logger.info(
                "Loaded pinned Qwen speakers: %s", sorted(self.speaker_map)
            )

        Qwen2_5OmniForConditionalGeneration.load_speakers = (
            _load_pinned_qwen_speakers
        )
        # Use local snapshot to avoid HF Hub re-fetch hang ("Fetching N files").
        # Honor checkpoint's native BF16 (FP16 cast overflows some weights).
        # KNOWN ISSUE under WSL2: bulk .to(device) on qwen-omni's deep
        # multimodal nn.Module tree triggers "CUDA driver error: unknown error"
        # at _load_state_dict_into_meta_model. device_map={"": device} loads
        # weights directly on GPU as they're read from disk, avoiding the
        # CPU→GPU bulk move at the end.
        free_mb = 0
        if device != "cpu" and torch.cuda.is_available():
            try:
                free_bytes, _total_bytes = torch.cuda.mem_get_info()
                free_mb = int(free_bytes // (1024 * 1024))
            except Exception:
                free_mb = 0
        dtype = torch.bfloat16 if device != "cpu" else torch.float32
        is_prequant = any(tok in str(model_id).lower() for tok in ("awq", "gptq", "int4", "int8"))
        load_kwargs = {
            "torch_dtype": dtype,
            "device_map": {"": device} if device != "cpu" else None,
            "low_cpu_mem_usage": True,
            "attn_implementation": "eager",
        }
        if not is_prequant and device != "cpu" and free_mb > 0 and free_mb < 28000:
            logger.info("Qwen Omni TTS bf16 headroom is low (%dMB free); using int4 quantization", free_mb)
            from transformers import BitsAndBytesConfig
            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=dtype,
                bnb_4bit_quant_type="nf4",
            )
            load_kwargs["device_map"] = "auto"

        qwen_model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
            model_id,
            local_files_only=True,
            **load_kwargs,
        )
        processor = Qwen2_5OmniProcessor.from_pretrained(
            model_id, local_files_only=True
        )
        _model_obj = (qwen_model, processor)

    elif _model_name == "vibevoice":
        import torch
        from vibevoice.modular.modeling_vibevoice_inference import (
            VibeVoiceForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
        weights_dir = MODELS_DIR / "vibevoice"
        if not (weights_dir / "config.json").is_file():
            raise FileNotFoundError(
                "VibeVoice's local checkpoint is missing. "
                "Run POST /api/setup/install/vibevoice."
            )
        model_id = str(weights_dir)
        vv_processor = VibeVoiceProcessor.from_pretrained(
            model_id, local_files_only=True
        )
        vv_model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            model_id,
            torch_dtype=torch.bfloat16 if device != "cpu" else torch.float32,
            device_map=device,
            attn_implementation="sdpa",
            local_files_only=True,
        )
        vv_model.eval()
        vv_model.set_ddpm_inference_steps(num_steps=10)
        _model_obj = (vv_model, vv_processor)

    elif _model_name == "higgs":
        from boson_multimodal.serve.serve_engine import HiggsAudioServeEngine
        # Use local snapshot — install pins to a pre-trfms-5 revision because the
        # latest HF config (model_type=higgs_audio_v2, flat) breaks the boson
        # repo's HiggsAudioConfig (expects nested text_config). Passing the repo
        # ID directly would resolve to main and re-trigger the incompatibility.
        local_higgs = MODELS_DIR / "higgs-audio"
        local_tok   = MODELS_DIR / "higgs-audio-tokenizer"
        if not (local_higgs / "config.json").is_file() or not local_tok.is_dir():
            raise FileNotFoundError(
                "Higgs Audio's local checkpoint or tokenizer is missing. "
                "Run POST /api/setup/install/higgs."
            )
        model_path = str(local_higgs)
        tokenizer_path = str(local_tok)
        _model_obj = HiggsAudioServeEngine(
            model_path, tokenizer_path, device=device,
        )

    elif _model_name == "speecht5":
        from transformers import SpeechT5Processor, SpeechT5ForTextToSpeech, SpeechT5HifiGan
        import pyarrow.parquet as _pq
        import torch as _torch
        local_model = MODELS_DIR / "speecht5"
        local_vocoder = MODELS_DIR / "speecht5-hifigan"
        xvector_file = (
            MODELS_DIR / "speecht5-xvectors" /
            "default" / "validation" / "0000.parquet"
        )
        if not xvector_file.is_file():
            raise FileNotFoundError(
                f"SpeechT5 speaker embeddings are missing: {xvector_file}. "
                "Re-run POST /api/setup/install/speecht5."
            )
        processor = SpeechT5Processor.from_pretrained(str(local_model))
        model = SpeechT5ForTextToSpeech.from_pretrained(str(local_model)).to(device)
        vocoder = SpeechT5HifiGan.from_pretrained(str(local_vocoder)).to(device)
        # Modern `datasets` rejects this repository's legacy Python dataset
        # script. Read its official auto-converted parquet directly and keep it
        # memory-mapped so 7,931 xvectors do not become Python-float objects.
        xvectors = _pq.read_table(
            xvector_file, columns=["xvector"], memory_map=True
        ).column("xvector")
        _model_obj = (model, processor, vocoder, xvectors)

    elif _model_name == "parler":
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
        import torch as _torch
        parler_dir = MODELS_DIR / "parler-tts"
        desc_tokenizer_dir = MODELS_DIR / "parler-desc-tokenizer"
        load_kwargs = {"local_files_only": True}
        if "cuda" in device:
            load_kwargs.update({
                "torch_dtype": _resolve_precision(_precision, device),
                "low_cpu_mem_usage": True,
            })
        model = ParlerTTSForConditionalGeneration.from_pretrained(
            str(parler_dir), **load_kwargs
        ).to(device)
        tokenizer = AutoTokenizer.from_pretrained(
            str(parler_dir), local_files_only=True
        )
        desc_tokenizer = AutoTokenizer.from_pretrained(
            str(desc_tokenizer_dir), local_files_only=True
        )
        _model_obj = (model, tokenizer, desc_tokenizer)

    elif _model_name == "outetts":
        import torch
        import outetts
        model_dir = MODELS_DIR / "outetts-1.0-0.6b"
        dac_path = MODELS_DIR / "outetts-dac" / "weights_24khz_1.5kbps_v1.0.pth"
        if not (model_dir / "model.safetensors").is_file() or not dac_path.is_file():
            raise FileNotFoundError(
                "OuteTTS's pinned local model or DAC is missing. "
                "Run POST /api/setup/install/outetts."
            )
        dtype = torch.bfloat16 if device != "cpu" and torch.cuda.is_bf16_supported() else (
            torch.float16 if device != "cpu" else torch.float32
        )
        interface = outetts.Interface(
            config=outetts.ModelConfig(
                model_path=str(model_dir),
                tokenizer_path=str(model_dir),
                interface_version=outetts.InterfaceVersion.V3,
                backend=outetts.Backend.HF,
                device=device,
                dtype=dtype,
                audio_codec_path=str(dac_path),
                max_seq_length=4096,
                additional_model_config={
                    "local_files_only": True,
                    "low_cpu_mem_usage": True,
                    "attn_implementation": "sdpa",
                },
            )
        )
        _model_obj = interface

    elif _model_name == "vits":
        from TTS.api import TTS
        _model_obj = TTS("tts_models/en/ljspeech/vits").to(device)

    elif _model_name == "edge":
        # Edge-TTS is cloud-based — no local model to load
        __import__("edge_tts")  # verify the package is installed
        _model_obj = True  # placeholder — inference calls the cloud API

    elif _model_name == "voxcpm2":
        from voxcpm import VoxCPM
        weights_dir = MODELS_DIR / "voxcpm2"
        if not (weights_dir / "config.json").is_file():
            raise FileNotFoundError(
                "VoxCPM2's local checkpoint is missing. "
                "Run POST /api/setup/install/voxcpm2."
            )
        _model_obj = VoxCPM.from_pretrained(
            str(weights_dir),
            load_denoiser=False,
            local_files_only=True,
            # torch.compile plus the package's dummy warm-up creates a large
            # one-off JIT cache and CPU spike. Eager inference is portable and
            # keeps cold loads bounded for this shared workstation.
            optimize=False,
            device=device,
        )

    elif _model_name == "csm":
        from transformers import CsmForConditionalGeneration, AutoProcessor
        weights_dir = MODELS_DIR / "csm-1b"
        if not (weights_dir / "config.json").is_file():
            raise FileNotFoundError(
                "CSM's local checkpoint is missing. "
                "Run POST /api/setup/install/csm."
            )
        model_id = str(weights_dir)
        dtype = _resolve_precision(_precision, device)
        processor = AutoProcessor.from_pretrained(
            model_id, local_files_only=True
        )
        model = CsmForConditionalGeneration.from_pretrained(
            model_id, dtype=dtype, device_map=device,
            low_cpu_mem_usage=True, local_files_only=True,
        )
        _model_obj = (model.eval(), processor)

    elif _model_name == "orpheus":
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from snac import SNAC
        weights_dir = MODELS_DIR / "orpheus-3b"
        snac_dir = MODELS_DIR / "snac-24khz"
        if not (weights_dir / "config.json").is_file() or not snac_dir.is_dir():
            raise FileNotFoundError(
                "Orpheus's local checkpoint or SNAC decoder is missing. "
                "Run POST /api/setup/install/orpheus."
            )
        model_id = str(weights_dir)
        snac_id = str(snac_dir)
        dtype = _resolve_precision(_precision, device)
        tokenizer = AutoTokenizer.from_pretrained(
            model_id, local_files_only=True
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=dtype, device_map=device,
            low_cpu_mem_usage=True, local_files_only=True,
        )
        snac_model = SNAC.from_pretrained(snac_id).to(device).eval()
        _model_obj = (model.eval(), tokenizer, snac_model)

    elif _model_name == "voxtral":
        # FlashInfer's sampler path JIT-builds CUDA extensions with nvcc.
        # This WSL runtime ships CUDA runtime libraries but not /usr/local/cuda,
        # so use vLLM's PyTorch-native sampler path for Voxtral.
        os.environ.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
        from mistral_common.tokens.tokenizers.mistral import MistralTokenizer
        from vllm_omni.entrypoints.omni import Omni

        weights_dir = MODELS_DIR / "voxtral-4b-tts"
        model_path = str(weights_dir) if weights_dir.exists() else \
            "mistralai/Voxtral-4B-TTS-2603"
        if (weights_dir / "tekken.json").exists():
            tokenizer = MistralTokenizer.from_file(str(weights_dir / "tekken.json"))
        else:
            tokenizer = MistralTokenizer.from_hf_hub("mistralai/Voxtral-4B-TTS-2603")
        # Map our precision strings to vLLM dtype names; "auto" reads from safetensors.
        _vllm_dtype = {"fp32": "float32", "fp16": "float16", "bf16": "bfloat16"}.get(
            _precision, "auto"
        )
        omni = Omni(
            model=model_path,
            dtype=_vllm_dtype,
            enforce_eager=True,
        )
        _model_obj = (omni, tokenizer)

    elif _model_name == "whisper":
        __import__("whisper")  # verify the package is installed
        # For whisper worker, model_obj is a dict of loaded sizes
        _model_obj = {}

    else:
        raise ValueError(f"Unknown model: {_model_name}")

    _loaded = True
    elapsed = time.time() - start
    logger.info("Model %s loaded in %.1fs", _model_name, elapsed)

    # Dia needs warmup inferences to produce coherent speech.
    # First 1-2 inferences after cold load produce garbage (hum/drone).
    if _model_name == "dia":
        _warmup_dia()


def _warmup_dia():
    """Run a warmup inference on Dia to prime CUDA kernels."""
    import numpy as np
    warmup_text = "[S1] Warmup inference, please discard this audio."
    try:
        t0 = time.time()
        data, sr = _infer_dia(warmup_text, {"max_new_tokens": 512})
        rms = float(np.sqrt(np.mean(data**2)))
        rms_db = 20 * np.log10(rms) if rms > 1e-10 else -100
        logger.info("Dia warmup: %.1fs, rms=%.1fdB", time.time() - t0, rms_db)
    except Exception as e:
        logger.warning("Dia warmup failed (expected): %s", e)


def _teardown_model_obj() -> None:
    """Release the (possibly partially-constructed) model and free VRAM.

    Caller MUST already hold _load_lock. Used both by unload_model() and by the
    load-failure paths so a partial/failed load cannot strand GPU tensors in a
    surviving worker (they would otherwise be referenced only by half-built
    locals/_model_obj and never freed, so a later /load stacks a second copy on
    top of the leaked first and accelerates OOM).
    """
    global _model_obj, _loaded

    # Model-specific cleanup before releasing reference
    if _model_name == "fish" and _model_obj is not None:
        try:
            if getattr(_model_obj, 'llama_queue', None) is not None:
                # launch_thread_safe_queue runs the GPU model in a daemon worker
                # thread that only exits when a None sentinel is enqueued. Without
                # this, clearing the attribute drops one reference but the thread
                # keeps the weights resident on GPU, so the gc.collect()/
                # empty_cache() below cannot reclaim VRAM.
                _model_obj.llama_queue.put(None)
                _model_obj.llama_queue = None
        except Exception:
            pass
    elif _model_name == "voxtral" and _model_obj is not None:
        # vLLM holds large GPU allocations; explicit shutdown releases them
        try:
            omni, _ = _model_obj
            if hasattr(omni, "shutdown"):
                omni.shutdown()
        except Exception:
            pass
    _model_obj = None
    _whisper_access_times.clear()
    _loaded = False

    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass


def unload_model() -> None:
    """Unload the model from GPU and free memory."""
    global _model_obj, _loaded

    with _load_lock:
        # Gate on _model_obj (not _loaded) so an explicit /unload after a
        # partial/failed load can still reclaim stranded GPU tensors.
        if not _loaded and _model_obj is None:
            return

        logger.info("Unloading model %s...", _model_name)
        _teardown_model_obj()

    logger.info("Model %s unloaded", _model_name)


# ============================================================
# Inference functions (moved from tts_api_server.py)
# ============================================================
def infer(text: str, params: dict) -> tuple:
    """Run model-specific inference. Returns (numpy_array, sample_rate).

    For Bark, params may contain 'history_prompt' and the response will
    include updated history in a separate field.
    """
    if not _loaded or _model_obj is None:
        raise RuntimeError(f"Model {_model_name} is not loaded")

    handler = _infer_dispatch().get(_model_name)
    if handler is None:
        raise ValueError(f"Inference not implemented for: {_model_name}")
    return handler(text, params)


_INFER_DISPATCH: dict | None = None


def _infer_dispatch() -> dict:
    """Lazily build (once) the model-name -> infer-handler dispatch table.

    Built lazily because the _infer_<model> functions are defined later in the
    module than infer(). Behavior is identical to the previous if/elif chain:
    an unknown model maps to None and infer() raises the same ValueError.
    """
    global _INFER_DISPATCH
    if _INFER_DISPATCH is None:
        _INFER_DISPATCH = {
            "xtts": _infer_xtts,
            "fish": _infer_fish,
            "kokoro": _infer_kokoro,
            "bark": _infer_bark,
            "chatterbox": _infer_chatterbox,
            "f5": _infer_f5,
            "dia": _infer_dia,
            "qwen": _infer_qwen,
            "vibevoice": _infer_vibevoice,
            "higgs": _infer_higgs,
            "speecht5": _infer_speecht5,
            "parler": _infer_parler,
            "outetts": _infer_outetts,
            "vits": _infer_vits,
            "edge": _infer_edge,
            "voxtral": _infer_voxtral,
            "voxcpm2": _infer_voxcpm2,
            "csm": _infer_csm,
            "orpheus": _infer_orpheus,
        }
    return _INFER_DISPATCH


_REFERENCE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


# Shared path-containment primitive.
from pathsafe import is_within as _path_is_relative_to


def _resolve_reference_audio_path(value: str) -> str | None:
    """Resolve saved/temp reference audio while rejecting arbitrary paths."""
    if not value or "\x00" in value:
        return None

    candidates = []
    try:
        candidates.append(VOICE_DIR / value)
    except (OSError, ValueError):
        pass
    try:
        candidates.append(Path(value).expanduser())
    except (OSError, ValueError):
        pass

    allowed_roots = (VOICE_DIR.resolve(), OUTPUT_DIR.resolve(), PROJECTS_OUTPUT.resolve())
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if not resolved.exists() or resolved.suffix.lower() not in _REFERENCE_AUDIO_EXTS:
            continue
        if any(resolved == root or _path_is_relative_to(resolved, root)
               for root in allowed_roots):
            return str(resolved)
    return None


def _infer_xtts(text: str, params: dict) -> tuple:
    import numpy as np
    voice = params.get("voice") or ""
    mode = params.get("mode") or "cloned"

    speaker_param = {}
    _tmp_ref = None  # temp file path to clean up (set when ref audio is trimmed)
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if mode == "cloned" and ref_path:
            ref, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            speaker_param["speaker_wav"] = ref
            if was_trimmed:
                _tmp_ref = ref
        else:
            speaker_param["speaker"] = voice
    else:
        builtin_speakers = getattr(_model_obj, "speakers", None) or []
        if builtin_speakers:
            speaker_param["speaker"] = builtin_speakers[0]

    try:
        tts_kwargs = {
            "text": text,
            "language": params.get("language") or "en",
            "temperature": float(_param(params, "temperature", 0.65)),
            "repetition_penalty": float(_param(params, "repetition_penalty", 2.0)),
            "split_sentences": False,
            **speaker_param,
        }
        top_k = params.get("top_k")
        top_p = params.get("top_p")
        if top_k is not None:
            tts_kwargs["top_k"] = int(top_k)
        if top_p is not None:
            tts_kwargs["top_p"] = float(top_p)
        wav = _model_obj.tts(**tts_kwargs)
        data = np.array(wav, dtype=np.float32)
        sr = _model_obj.synthesizer.output_sample_rate
        return data, sr
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _param(params, name, default):
    value = params.get(name)
    return default if value is None else value


def _read_and_trim_ref_audio(path: str, max_seconds: float = 30.0) -> bytes:
    """Read a reference audio file, trimming to max_seconds if too long.

    Fish Speech recommends 10-30s of reference audio.  Longer clips can
    degrade quality or cause OOM.  Returns WAV bytes.
    """
    import soundfile as sf

    from audio_io import read_audio
    data, sr = read_audio(path, max_seconds=max_seconds)
    duration = len(data) / sr
    if duration > max_seconds:
        logger.info("Trimming reference audio from %.1fs to %.1fs", duration, max_seconds)
        data = data[: int(sr * max_seconds)]
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="WAV")
    return buf.getvalue()


def _trim_ref_audio_path(path: str, max_seconds: float = 30.0) -> tuple[str, bool]:
    """Trim reference audio file if too long. Returns (path, was_trimmed).

    If trimming is needed, the returned path is a temp file the caller must delete.
    If no trimming needed, returns the original path unchanged.
    """
    import soundfile as sf
    import tempfile

    from audio_io import open_audio
    with open_audio(path, max_seconds=max_seconds) as stream:
        sr = stream.samplerate
        if len(stream) <= int(sr * max_seconds) and Path(path).suffix.lower() != ".m4a":
            return path, False
        data = stream.read(frames=int(sr * max_seconds), dtype="float32")
    # Pin to TMPDIR (set by config.app_environment to /opt/tts_server/cache/tmp)
    # so trimmed refs never leak into /tmp.
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=tempfile.gettempdir())
    sf.write(tmp.name, data, sr, subtype="PCM_16")
    tmp.close()
    return tmp.name, True


def _infer_fish(text: str, params: dict) -> tuple:
    import numpy as np
    from fish_speech.utils.schema import ServeTTSRequest, ServeReferenceAudio

    engine = _model_obj

    top_p = float(_param(params, "top_p", 0.8))
    temperature = float(_param(params, "temperature", 0.8))
    repetition_penalty = float(_param(params, "repetition_penalty", 1.1))
    # Fish Speech requires 0 < repetition_penalty < 2 (strict);
    # gateway default is 2.0 (for XTTS), so clamp to Fish's safe range
    if repetition_penalty >= 2.0:
        repetition_penalty = 1.1
    # chunk_length and max_new_tokens are NOT user-controllable. The text
    # splitter (chunk_text_for_model) sizes each chunk for fish (≤250 chars),
    # so chunk_length=250 here just disables fish-speech's internal re-chunking.
    chunk_length = 250
    # Fish-speech has poor EOS calibration on short text — without bounding,
    # it generates the full 1024 tokens (~80s of audio for 3s of text) because
    # each VQ token decodes to ~0.08s. Cap proportional to chunk char count
    # (~2.5 tokens/char + 60 token tail) so the model has prosody headroom
    # but can't run away.
    max_new_tokens = max(120, min(1024, int(len(text) * 2.5) + 60))
    seed = int(_param(params, "seed", 0)) or None  # 0 = random

    # Handle reference audio (optional voice cloning)
    references = []
    voice = params.get("voice") or params.get("reference_audio") or ""
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if ref_path:
            audio_bytes = _read_and_trim_ref_audio(ref_path, max_seconds=30.0)
            ref_text = params.get("reference_text") or ""
            references.append(ServeReferenceAudio(audio=audio_bytes, text=ref_text))

    req = ServeTTSRequest(
        text=text,
        chunk_length=chunk_length,
        format="wav",
        references=references,
        top_p=top_p,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        max_new_tokens=max_new_tokens,
        seed=seed,
    )

    audio_segments = []
    for result in engine.inference(req):
        if result.code == "final" and result.audio:
            _, audio_data = result.audio
            audio_segments.append(audio_data)
        elif result.code == "error" and result.error:
            raise result.error

    if not audio_segments:
        raise RuntimeError("Fish Speech returned no audio")

    data = np.concatenate(audio_segments, axis=0).astype(np.float32)
    return data, 24000


def _infer_kokoro(text: str, params: dict) -> tuple:
    import numpy as np
    from kokoro import KPipeline
    voice = params.get("voice") or "af_heart"
    if not re.fullmatch(r"[a-z]{2}_[a-z0-9_]+", voice):
        raise ValueError("Invalid Kokoro voice")
    lang_code = voice[0]
    pipelines = _model_obj["pipelines"]
    if lang_code not in pipelines:
        pipelines[lang_code] = KPipeline(lang_code=lang_code, model=_model_obj["model"],
                                        repo_id="hexgrad/Kokoro-82M", device=_device)
    voice_path = MODELS_DIR / "kokoro" / "voices" / f"{voice}.pt"
    if not voice_path.is_file():
        raise FileNotFoundError(f"Kokoro voice not installed: {voice}")
    gen = pipelines[lang_code](text, voice=str(voice_path))
    audio_segments = [audio for _, _, audio in gen]

    if not audio_segments:
        raise RuntimeError("Kokoro returned no audio segments")

    raw_audio = np.concatenate(audio_segments, axis=0).astype(np.float32)
    return raw_audio, 24000


def _infer_bark(text: str, params: dict) -> tuple:
    """Bark inference with history prompt support.

    The gateway passes history_prompt through params and expects
    updated history back via the 'bark_history' field in the response.

    history_prompt from gateway is either:
      - None: first chunk without a preset
      - v2/<language>_speaker_<0..9>: bundled Bark prompt tensors
      - three base64-encoded numpy arrays: continued chunk history
    """
    import numpy as np
    import torch

    bark_model = _model_obj.synthesizer.tts_model

    # Decode history prompt from gateway
    # Gateway sends either: None, string, or {"_bark_b64": [b64_1, b64_2, b64_3]}
    raw_history = params.get("history_prompt")
    if raw_history is None:
        history = (None, None, None)
    elif isinstance(raw_history, str):
        if not re.fullmatch(r"v2/[a-z]{2}_speaker_[0-9]", raw_history):
            raise ValueError(
                "Invalid Bark preset. Use v2/<language>_speaker_<0..9> "
                "from GET /api/tts/bark/voices."
            )
        prompt_root = MODELS_DIR / "bark-voices" / "speaker_embeddings"
        stem = prompt_root / raw_history
        prompt_paths = (
            Path(f"{stem}_semantic_prompt.npy"),
            Path(f"{stem}_coarse_prompt.npy"),
            Path(f"{stem}_fine_prompt.npy"),
        )
        if any(not path.is_file() for path in prompt_paths):
            raise FileNotFoundError(
                f"Bark preset '{raw_history}' is not installed. "
                "Run POST /api/setup/install/bark."
            )
        history = tuple(
            torch.from_numpy(np.load(path, allow_pickle=False)).to(bark_model.device)
            for path in prompt_paths
        )
        logger.info("Loaded Bark voice preset %s", raw_history)
    elif isinstance(raw_history, dict) and "_bark_b64" in raw_history:
        # Base64-encoded numpy arrays from gateway
        parts = []
        for b64_str in raw_history["_bark_b64"]:
            buf = io.BytesIO(base64.b64decode(b64_str))
            arr = np.load(buf, allow_pickle=False)
            parts.append(torch.from_numpy(arr).to(bark_model.device))
        history = tuple(parts)
    else:
        history = (None, None, None)

    text_temp = float(_param(params, "temperature", 0.7))
    waveform_temp = float(_param(params, "waveform_temperature", 0.7))

    audio_arr, x_semantic, coarse, fine = bark_model.generate_audio(
        text,
        history_prompt=history,
        text_temp=text_temp,
        waveform_temp=waveform_temp,
    )

    # Free input history GPU tensors now that generation is complete
    del history

    if hasattr(audio_arr, 'numpy'):
        audio_arr = audio_arr.numpy()
    data = np.array(audio_arr, dtype=np.float32)
    del audio_arr

    # Move history to CPU numpy immediately, then release GPU tensors
    bark_history = (
        x_semantic.cpu().numpy() if hasattr(x_semantic, 'cpu') else np.array(x_semantic),
        coarse.cpu().numpy() if hasattr(coarse, 'cpu') else np.array(coarse),
        fine.cpu().numpy() if hasattr(fine, 'cpu') else np.array(fine),
    )
    del x_semantic, coarse, fine

    return data, 24000, bark_history


def _infer_chatterbox(text: str, params: dict) -> tuple:
    import numpy as np

    exaggeration = float(_param(params, "exaggeration", 0.5))
    cfg_weight = float(_param(params, "cfg_weight", 0.5))
    temperature = float(_param(params, "temperature", 0.8))
    repetition_penalty = float(_param(params, "repetition_penalty", 1.2))

    voice = params.get("voice") or params.get("reference_audio") or ""
    audio_prompt_path = None
    _tmp_ref = None
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if ref_path:
            audio_prompt_path, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = audio_prompt_path

    try:
        wav = _model_obj.generate(
            text,
            audio_prompt_path=audio_prompt_path,
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
            temperature=temperature,
            repetition_penalty=repetition_penalty,
        )

        if hasattr(wav, 'cpu'):
            wav = wav.cpu()
        if hasattr(wav, 'numpy'):
            wav = wav.numpy()

        data = np.array(wav, dtype=np.float32).squeeze()
        del wav
        return data, _model_obj.sr
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_f5(text: str, params: dict) -> tuple:
    import numpy as np

    voice = params.get("voice") or params.get("reference_audio") or ""
    ref_text = params.get("reference_text") or ""
    # Sanitize: JSON null can become string "null" in some serialization paths
    if ref_text.lower() in ("null", "none"):
        ref_text = ""
    if not ref_text:
        logger.warning("F5-TTS: no reference_text provided — model will auto-transcribe "
                       "(slower, uses extra VRAM). Pre-transcribe via Voices tab for best results.")

    ref_file = None
    _tmp_ref = None
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if ref_path:
            ref_file, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=15.0)
            if was_trimmed:
                _tmp_ref = ref_file

    if not ref_file:
        raise ValueError("F5-TTS requires reference audio (set 'voice' to a wav file)")

    seed = params.get("seed")
    if seed is not None:
        seed = int(seed)
        if seed == 0:
            seed = -1  # F5-TTS uses -1 for random, our UI uses 0
    nfe_step = int(_param(params, "nfe_step", 32))
    cfg_strength = float(_param(params, "cfg_scale", 2.0))

    logger.info("F5 infer: ref_file=%s, ref_text=%r, gen_text=%r, nfe=%d, cfg=%.1f",
                ref_file, ref_text[:50] if ref_text else "(empty - will auto-transcribe)",
                text[:50], nfe_step, cfg_strength)

    try:
        wav, sr, _ = _model_obj.infer(
            ref_file=ref_file, ref_text=ref_text, gen_text=text,
            seed=seed, remove_silence=False,
            nfe_step=nfe_step, cfg_strength=cfg_strength,
        )

        data = np.array(wav, dtype=np.float32).squeeze()
        del wav
        return data, sr
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_dia(text: str, params: dict) -> tuple:
    import re
    import numpy as np

    # Auto-fix lowercase dialogue tags: [s1] -> [S1], [s2] -> [S2]
    text = re.sub(r'\[s(\d)\]', lambda m: f'[S{m.group(1)}]', text)

    if params.get("max_new_tokens") is not None:
        max_new_tokens = int(params["max_new_tokens"])
    else:
        # Dia emits roughly 86 acoustic tokens per second. A plain character
        # estimate is too small for dialogue because speaker transitions and
        # non-verbal cues consume time without adding many input characters.
        # Give each extra turn/effect bounded headroom while retaining a hard
        # ceiling against runaway generations.
        speaker_turns = len(re.findall(r"\[S\d\]", text))
        nonverbal_cues = len(re.findall(r"\([^)]{1,40}\)", text))
        dialogue_headroom = max(0, speaker_turns - 1) * 256
        effect_headroom = nonverbal_cues * 128
        max_new_tokens = max(
            384,
            min(
                2048,
                int(len(text) * 8) + 160 + dialogue_headroom + effect_headroom,
            ),
        )
    cfg_scale = float(_param(params, "cfg_scale", 3.0))
    top_p = float(_param(params, "top_p", 0.90))
    top_k = int(_param(params, "top_k", 50))
    temperature = float(_param(params, "temperature", 1.8))

    voice = params.get("voice") or params.get("reference_audio") or ""
    audio_prompt = None
    _tmp_ref = None
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if ref_path:
            audio_prompt, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = audio_prompt

    try:
        generate_kwargs = {
            "text": text,
            "max_tokens": max_new_tokens,
            "cfg_scale": cfg_scale,
            "temperature": temperature,
            "top_p": top_p,
            "cfg_filter_top_k": top_k,
        }
        if audio_prompt:
            generate_kwargs["audio_prompt_path"] = audio_prompt

        output = _model_obj.generate(**generate_kwargs, verbose=True)
        data = np.array(output, dtype=np.float32).squeeze()
        del output
        duration = len(data) / 44100 if data.ndim > 0 else 0
        rms = float(np.sqrt(np.mean(data**2))) if len(data) > 0 else 0
        rms_db = 20 * np.log10(rms) if rms > 1e-10 else -100
        logger.info("Dia generate output: shape=%s duration=%.3fs rms=%.1fdB", data.shape, duration, rms_db)
        if duration < 0.5:
            raise RuntimeError(f"Dia generated degenerate audio ({duration:.3f}s) - will retry")
        if rms_db < -40:
            raise RuntimeError(f"Dia generated near-silent audio (rms={rms_db:.1f}dB) - will retry")
        return data, 44100
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_qwen(text: str, params: dict) -> tuple:
    import numpy as np
    import torch

    model_obj, processor = _model_obj

    speaker = params.get("voice") or "Chelsie"
    temperature = float(_param(params, "temperature", 0.9))
    top_p = float(_param(params, "top_p", 0.8))
    top_k = int(_param(params, "top_k", 40))
    seed = int(_param(params, "seed", 0))
    if seed:
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

    thinker_max_new_tokens = max(32, min(128, int(len(text) * 1.0) + 24))
    # Qwen's speech codec is roughly 12.5 tokens/second. This leaves generous
    # prosody headroom without the upstream 4,096-token multi-minute ceiling.
    talker_max_new_tokens = max(64, min(768, int(len(text) * 1.8) + 40))

    conversation = [
        {
            "role": "system",
            "content": [{
                "type": "text",
                "text": (
                    "You are Qwen, a virtual human developed by the Qwen Team, "
                    "Alibaba Group, capable of perceiving auditory and visual inputs, "
                    "as well as generating text and speech."
                ),
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"Please read the following text aloud exactly as written: {text}",
            }],
        },
    ]

    inputs = processor.apply_chat_template(
        conversation, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt", padding=True,
    ).to(model_obj.device)

    _, audio = model_obj.generate(
        **inputs, speaker=speaker, return_audio=True,
        thinker_max_new_tokens=thinker_max_new_tokens,
        talker_max_new_tokens=talker_max_new_tokens,
        talker_do_sample=True, talker_temperature=temperature,
        talker_top_p=top_p, talker_top_k=top_k,
    )
    del inputs  # free GPU input tensors

    if audio is None:
        raise RuntimeError("Qwen returned no audio output")

    data = audio.reshape(-1).detach().cpu().numpy().astype(np.float32)
    del audio
    return data, 24000


def _infer_vibevoice(text: str, params: dict) -> tuple:
    import numpy as np
    import torch

    model_obj, processor = _model_obj

    if not text.strip().startswith("Speaker"):
        text = f"Speaker 1: {text}"

    references = params.get("reference_audios") or []
    if not isinstance(references, list):
        raise ValueError("reference_audios must be a JSON list")
    voice = params.get("voice") or params.get("reference_audio") or ""
    reference_candidates = [str(value) for value in references if str(value or "").strip()]
    if voice and voice not in reference_candidates:
        reference_candidates.insert(0, str(voice))
    reference_candidates = reference_candidates[:4]
    voice_samples = []
    _tmp_refs: list[str] = []
    try:
        for candidate in reference_candidates:
            ref_path = _resolve_reference_audio_path(candidate)
            if not ref_path:
                raise FileNotFoundError(f"VibeVoice reference audio not found: {candidate}")
            ref, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=15.0)
            voice_samples.append(ref)
            if was_trimmed:
                _tmp_refs.append(ref)
        if not voice_samples:
            raise ValueError("VibeVoice requires at least one reference voice")

        cfg_scale = float(1.3 if params.get("cfg_scale") is None else params["cfg_scale"])
        seed = int(_param(params, "seed", 0))
        if seed:
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        # The upstream inference class otherwise permits the full 64K context.
        # VibeVoice emits speech latents at roughly 7.5 Hz; this bound still leaves
        # generous long-form headroom for each app-owned 800-character chunk.
        max_new_tokens = max(64, min(768, int(len(text) * 1.6) + 80))

        inputs = processor(
            text=[text],
            voice_samples=[voice_samples] if voice_samples else None,
            padding=True, return_tensors="pt", return_attention_mask=True,
        )
        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(model_obj.device)

        outputs = model_obj.generate(
            **inputs, cfg_scale=cfg_scale, tokenizer=processor.tokenizer,
            generation_config={"do_sample": False},
            is_prefill=bool(voice_samples),
            max_new_tokens=max_new_tokens,
            max_length_times=2,
            show_progress_bar=False,
        )
        del inputs  # free GPU input tensors

        audio_tensor = outputs.speech_outputs[0]
        del outputs  # free remaining GPU generation state
        if hasattr(audio_tensor, "cpu"):
            audio_tensor = audio_tensor.cpu()
        if hasattr(audio_tensor, "float"):
            audio_tensor = audio_tensor.float()
        if hasattr(audio_tensor, "numpy"):
            audio_tensor = audio_tensor.numpy()

        data = np.array(audio_tensor, dtype=np.float32).squeeze()
        return data, 24000
    finally:
        for _tmp_ref in _tmp_refs:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_higgs(text: str, params: dict) -> tuple:
    import numpy as np
    from boson_multimodal.data_types import ChatMLSample, Message, AudioContent

    temperature = float(_param(params, "temperature", 0.3))
    top_p = float(_param(params, "top_p", 0.95))
    top_k = int(_param(params, "top_k", 50))
    seed = int(_param(params, "seed", 0)) or None

    scene_description = str(params.get("voice_description") or "").strip()
    for control_token in ("<|scene_desc_start|>", "<|scene_desc_end|>"):
        scene_description = scene_description.replace(control_token, " ")
    scene_description = " ".join(scene_description.split())[:1000]
    if not scene_description:
        scene_description = "Audio is recorded from a quiet room."

    system_prompt = (
        "Generate audio following instruction.\n\n"
        "<|scene_desc_start|>\n"
        f"{scene_description}\n"
        "<|scene_desc_end|>"
    )

    messages = [Message(role="system", content=system_prompt)]

    voice = params.get("voice") or params.get("reference_audio") or ""
    ref_text = params.get("reference_text") or ""
    _tmp_ref = None
    if voice:
        ref_path = _resolve_reference_audio_path(voice)
        if ref_path:
            ref_path, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = ref_path
            if ref_text:
                messages.append(Message(role="user", content=ref_text))
            messages.append(
                Message(role="assistant", content=AudioContent(audio_url=ref_path))
            )

    messages.append(Message(role="user", content=text))

    try:
        # Higgs emits about 50 audio timesteps/second. Keep proportional
        # headroom for prosody without allowing a short API request to consume
        # the old 2,048-token (~41 second) ceiling.
        max_new_tokens = max(160, min(1024, int(len(text) * 3.0) + 80))
        output = _model_obj.generate(
            chat_ml_sample=ChatMLSample(messages=messages),
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            stop_strings=["<|end_of_text|>", "<|eot_id|>"],
        )

        if output.audio is None:
            raise RuntimeError("Higgs returned no audio output")

        data = np.array(output.audio, dtype=np.float32).squeeze()
        sr = output.sampling_rate or 24000
        return data, sr
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_speecht5(text: str, params: dict) -> tuple:
    """SpeechT5 — Microsoft's HF-native multi-speaker TTS."""
    import numpy as np
    import torch

    model, processor, vocoder, xvectors = _model_obj

    # Speaker selection — index into CMU-Arctic xvectors (default 7306 = neutral female)
    speaker_idx = int(_param(params, "speaker_idx", 7306))
    if speaker_idx < 0 or speaker_idx >= len(xvectors):
        speaker_idx = 7306  # fall back to default on out-of-range
    voice = params.get("voice") or ""
    # Map voice names to xvector indices
    _speaker_map = {
        "female_1": 7306, "male_1": 2271, "female_2": 1055, "male_2": 4446,
        "female_3": 6671, "male_3": 3043, "neutral": 7306,
    }
    if voice in _speaker_map:
        speaker_idx = _speaker_map[voice]

    speaker_embeddings = torch.tensor(
        xvectors[speaker_idx].as_py()
    ).unsqueeze(0).to(_device)

    inputs = processor(text=text, return_tensors="pt").to(_device)
    speech = model.generate_speech(inputs["input_ids"], speaker_embeddings, vocoder=vocoder)
    del inputs, speaker_embeddings

    data = speech.cpu().numpy().astype(np.float32)
    del speech
    return data, 16000


def _infer_parler(text: str, params: dict) -> tuple:
    """Parler-TTS — describe the voice in natural language."""
    import numpy as np

    model, tokenizer, desc_tokenizer = _model_obj

    # Voice description — the unique Parler feature
    description = params.get("voice_description") or params.get("voice") or (
        "A female speaker delivers a clear and expressive speech with moderate speed. "
        "The recording is of very high quality with the speaker's voice sounding clear and close up."
    )
    temperature = float(_param(params, "temperature", 1.0))

    desc_inputs = desc_tokenizer(description, return_tensors="pt").to(_device)
    prompt_inputs = tokenizer(text, return_tensors="pt").to(_device)

    # Bound audio-code generation so a short API request cannot monopolize the
    # distro for several minutes when EOS sampling is unlucky. Parler produces
    # roughly 75 audio frames/sec; this allows up to ~3-16 seconds depending on
    # text length while preserving normal early-EOS behavior.  The gateway
    # chunks longer narration, so a single unlucky sample cannot monopolize a
    # GPU (and make the whole WSL app unresponsive) for several minutes.
    max_new_tokens = max(256, min(1200, len(text) * 12))

    generation = model.generate(
        input_ids=desc_inputs.input_ids,
        attention_mask=desc_inputs.attention_mask,
        prompt_input_ids=prompt_inputs.input_ids,
        prompt_attention_mask=prompt_inputs.attention_mask,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
    )
    del desc_inputs, prompt_inputs  # free GPU input tensors
    data = generation.cpu().numpy().squeeze().astype(np.float32)
    del generation
    sr = model.config.sampling_rate  # typically 24000
    return data, sr


def _infer_outetts(text: str, params: dict) -> tuple:
    """OuteTTS 1.0 — local multilingual TTS and transcript-guided cloning."""
    import numpy as np
    import soundfile as sf
    import torch

    interface = _model_obj
    temperature = float(_param(params, "temperature", 0.4))
    repetition_penalty = float(_param(params, "repetition_penalty", 1.1))
    top_k = int(_param(params, "top_k", 40))
    top_p = float(_param(params, "top_p", 0.9))
    seed = int(_param(params, "seed", 0))
    if seed:
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

    # Upstream cloning loads a second, unpinned Whisper-turbo model inside this
    # worker.  Use the caller's exact transcript to build the same V3 speaker
    # structure locally and deterministically instead.
    voice = params.get("voice") or params.get("reference_audio") or ""
    default_voice = "EN-FEMALE-1-NEUTRAL"
    if voice and str(voice).upper() != default_voice:
        ref_path = _resolve_reference_audio_path(voice)
        if not ref_path:
            raise FileNotFoundError(f"OuteTTS reference audio not found: {voice}")
        reference_text = str(params.get("reference_text") or "").strip()
        if not reference_text:
            raise ValueError("OuteTTS cloning requires reference_text for the reference audio")
        info = sf.info(ref_path)
        duration = float(info.frames) / float(info.samplerate)
        if duration <= 0 or duration > 15.0:
            raise ValueError("OuteTTS reference audio must be between 0 and 15 seconds")
        words = re.findall(r"\S+", reference_text)
        if not words:
            raise ValueError("OuteTTS reference_text must contain spoken words")
        weights = [max(1, len(re.sub(r"\W+", "", word))) for word in words]
        total_weight = float(sum(weights))
        cursor = 0.0
        word_timings = []
        for index, (word, weight) in enumerate(zip(words, weights)):
            end = duration if index == len(words) - 1 else (
                cursor + duration * (float(weight) / total_weight)
            )
            word_timings.append({"word": word, "start": cursor, "end": end})
            cursor = end
        with open(ref_path, "rb") as ref_file:
            audio_bytes = ref_file.read()
        speaker = interface.audio_processor.create_speaker_from_dict({
            "audio": {"bytes": audio_bytes},
            "text": reference_text,
            "words": word_timings,
        })
    else:
        speaker = interface.load_default_speaker(default_voice)

    import outetts
    # Bound new audio tokens separately from the roughly 1,840-token bundled
    # speaker prompt.  Upstream's total max_length default is 8192.
    max_new_tokens = max(512, min(1024, int(len(text) * 12) + 384))
    output = interface.generate(
        config=outetts.GenerationConfig(
            text=text,
            speaker=speaker,
            max_length=4096,
            additional_gen_config={"max_new_tokens": max_new_tokens},
            sampler_config=outetts.SamplerConfig(
                temperature=temperature,
                repetition_penalty=repetition_penalty,
                top_k=top_k,
                top_p=top_p,
            ),
        )
    )

    audio = output.audio
    if audio is None:
        raise RuntimeError("OuteTTS returned no audio")
    if hasattr(audio, "cpu"):
        audio = audio.cpu()
    data = audio.numpy().squeeze().astype(np.float32)
    return data, int(output.sr)


def _infer_vits(text: str, params: dict) -> tuple:
    """VITS inference via Coqui TTS — fast, lightweight, single-speaker."""
    import numpy as np
    wav = _model_obj.tts(text=text, split_sentences=False)
    data = np.array(wav, dtype=np.float32)
    sr = _model_obj.synthesizer.output_sample_rate
    return data, sr


def _infer_edge(text: str, params: dict) -> tuple:
    """Edge-TTS inference — cloud-based Microsoft neural voices."""
    import numpy as np
    import asyncio
    import tempfile
    import os
    import socket

    # Fast connectivity check — fail immediately instead of hanging for minutes
    try:
        sock = socket.create_connection(("speech.platform.bing.com", 443), timeout=3)
        sock.close()
    except (OSError, socket.timeout):
        raise RuntimeError("Edge-TTS requires internet — cannot reach Microsoft speech service")

    voice = params.get("voice") or "en-US-JennyNeural"
    # Speed is handled by the gateway's post-processing (pyrubberband),
    # so we don't pass rate to the cloud API — avoids double-application.
    pitch_pct = int(_param(params, "pitch", 0))
    pitch_str = f"{pitch_pct:+d}Hz"
    volume_pct = int(_param(params, "volume", 0))
    volume_str = f"{volume_pct:+d}%"

    # Pin to TMPDIR (config.app_environment) so Edge-TTS scratch files don't
    # leak into /tmp inside the WSL distro.
    tmp_fh = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=tempfile.gettempdir())
    tmp = tmp_fh.name
    tmp_fh.close()
    try:
        async def _gen():
            import edge_tts
            comm = edge_tts.Communicate(
                text, voice, pitch=pitch_str, volume=volume_str,
            )
            await comm.save(tmp)

        # Run async edge_tts in a fresh event loop (we're in a sync thread)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_gen())
        finally:
            loop.close()

        # Edge-TTS outputs MP3; decode via pydub (already installed)
        from pydub import AudioSegment
        audio = AudioSegment.from_mp3(tmp)
        samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
        samples /= 2 ** (audio.sample_width * 8 - 1)  # normalize based on actual bit depth
        if audio.channels > 1:
            samples = samples.reshape((-1, audio.channels)).mean(axis=1)
        return samples, audio.frame_rate
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _infer_csm(text: str, params: dict) -> tuple:
    """Sesame CSM-1B — bounded speech with optional audio context."""
    import numpy as np
    import torch

    model, processor = _model_obj

    speaker_idx = int(_param(params, "speaker_idx", 0))
    voice = params.get("voice") or ""
    # These are conversation role IDs, not guaranteed fixed voice identities.
    _speaker_map = {"speaker_0": 0, "speaker_1": 1, "speaker_2": 2, "speaker_3": 3}
    if voice in _speaker_map:
        speaker_idx = _speaker_map[voice]

    ref_path = _resolve_reference_audio_path(voice) if voice else None
    ref_text = str(params.get("reference_text") or "").strip()
    _tmp_ref = None
    try:
        if ref_path:
            if not ref_text:
                raise ValueError(
                    "CSM reference conditioning requires the exact reference_text transcript"
                )
            ref_path, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = ref_path
            import librosa
            ref_audio, _ = librosa.load(ref_path, sr=24000, mono=True)
            conversation = [
                {
                    "role": str(speaker_idx),
                    "content": [
                        {"type": "text", "text": ref_text},
                        {"type": "audio", "audio": ref_audio},
                    ],
                },
                {
                    "role": str(speaker_idx),
                    "content": [{"type": "text", "text": text}],
                },
            ]
            inputs = processor.apply_chat_template(
                conversation, tokenize=True, return_dict=True
            ).to(_device)
            if inputs.get("input_values") is not None:
                codec_dtype = next(model.codec_model.parameters()).dtype
                inputs["input_values"] = inputs["input_values"].to(
                    device=_device, dtype=codec_dtype
                )
        else:
            prompt = f"[{speaker_idx}]{text}"
            inputs = processor(prompt, add_special_tokens=True).to(_device)

        temperature = float(_param(params, "temperature", 0.9))
        top_p = float(_param(params, "top_p", 0.95))
        top_k = int(_param(params, "top_k", 50))
        seed = int(_param(params, "seed", 42))
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        # Keep malformed EOS behavior from running for thousands of frames.
        max_new_tokens = max(125, min(500, int(len(text) * 2.0) + 70))
        audio = model.generate(
            **inputs,
            output_audio=True,
            max_new_tokens=max_new_tokens,
            do_sample=True,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            depth_decoder_do_sample=True,
            depth_decoder_temperature=temperature,
            depth_decoder_top_k=top_k,
        )
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass
    if isinstance(audio, (list, tuple)):
        audio = audio[0]
    if hasattr(audio, "cpu"):
        audio = audio.cpu()
    if hasattr(audio, "numpy"):
        audio = audio.numpy()
    data = np.array(audio, dtype=np.float32).squeeze()
    sr = int(getattr(getattr(processor, "feature_extractor", None), "sampling_rate", 24000))
    return data, sr


def _infer_orpheus(text: str, params: dict) -> tuple:
    """Orpheus 3B — llama-based TTS, audio tokens decoded via SNAC.

    Voice tags and emotion tags (`<laugh>`, `<sigh>`, etc.) are inline
    in the text. The model emits audio tokens which we strip, redistribute
    across SNAC's 3 codebooks, and decode to 24kHz waveform.
    """
    import numpy as np
    import torch

    model, tokenizer, snac_model = _model_obj

    voice = params.get("voice") or "tara"
    supported_voices = {"tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"}
    if voice not in supported_voices:
        raise ValueError(f"Unknown Orpheus voice '{voice}'")
    temperature = float(
        params.get("temperature") if params.get("temperature") is not None else 0.6
    )
    top_p = float(params.get("top_p") if params.get("top_p") is not None else 0.95)
    repetition_penalty = float(
        params.get("repetition_penalty")
        if params.get("repetition_penalty") is not None else 1.1
    )
    seed = int(params.get("seed") if params.get("seed") is not None else 42)
    if seed == 0:
        seed = int.from_bytes(os.urandom(8), "big") % (2**63 - 1)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    # Seven generated tokens represent one 12 Hz SNAC frame (~84 tokens/sec).
    # Derive a conservative per-chunk ceiling instead of allowing the old
    # unconditional 2,000-token / ~24-second runaway on a short sentence.
    max_new_tokens = max(280, min(1260, len(text.strip()) * 9 + 196))

    # Official finetune prompt contract:
    # [SOH] voice: text [text-EOT] [EOH] [SOA] [audio-start]
    prompt = f"{voice}: {text.strip()}"
    base_ids = tokenizer(prompt, return_tensors="pt").input_ids
    start_token = torch.tensor([[128259]], dtype=torch.int64)
    end_tokens = torch.tensor([[128009, 128260, 128261, 128257]], dtype=torch.int64)
    input_ids = torch.cat([start_token, base_ids, end_tokens], dim=1).to(_device)
    attention_mask = torch.ones_like(input_ids)

    generation_kwargs = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "max_new_tokens": max_new_tokens,
        "do_sample": temperature > 0,
        "repetition_penalty": repetition_penalty,
        "eos_token_id": 128258,
        "pad_token_id": 128263,
    }
    if temperature > 0:
        generation_kwargs.update(
            temperature=temperature,
            top_p=max(1e-6, top_p),
        )
    with torch.inference_mode():
        output = model.generate(
            **generation_kwargs,
        )
    del input_ids, attention_mask

    # Strip everything before the last audio-start marker (128257),
    # remove EOT markers (128258), trim to multiple of 7 codes, shift base.
    indices = (output == 128257).nonzero(as_tuple=True)
    if len(indices[1]) > 0:
        cropped = output[:, indices[1][-1].item() + 1:]
    else:
        raise RuntimeError("Orpheus output is missing the audio-start marker")
    row = cropped[0]
    # Ignore the terminating special token and reject text/control noise by
    # retaining only the documented flattened SNAC token range.
    row = row[(row >= 128266) & (row <= 156937)]
    new_len = (row.size(0) // 7) * 7
    if new_len == 0:
        raise RuntimeError("Orpheus generated no audio tokens")
    code_list = (row[:new_len] - 128266).tolist()

    # Redistribute the flat code stream into SNAC's 3 hierarchical codebooks.
    layer_1, layer_2, layer_3 = [], [], []
    for i in range(len(code_list) // 7):
        layer_1.append(code_list[7 * i])
        layer_2.append(code_list[7 * i + 1] - 4096)
        layer_3.append(code_list[7 * i + 2] - 2 * 4096)
        layer_3.append(code_list[7 * i + 3] - 3 * 4096)
        layer_2.append(code_list[7 * i + 4] - 4 * 4096)
        layer_3.append(code_list[7 * i + 5] - 5 * 4096)
        layer_3.append(code_list[7 * i + 6] - 6 * 4096)
    codes = [
        torch.tensor(layer_1, device=_device, dtype=torch.long).unsqueeze(0),
        torch.tensor(layer_2, device=_device, dtype=torch.long).unsqueeze(0),
        torch.tensor(layer_3, device=_device, dtype=torch.long).unsqueeze(0),
    ]
    if any(torch.any(code < 0) or torch.any(code >= 4096) for code in codes):
        raise RuntimeError("Orpheus generated an invalid SNAC code")

    with torch.inference_mode():
        audio_hat = snac_model.decode(codes)
    data = audio_hat.squeeze().detach().cpu().numpy().astype(np.float32)
    return data, 24000


def _infer_voxcpm2(text: str, params: dict) -> tuple:
    """VoxCPM2 — diffusion-AR TTS with reference cloning + voice design.

    Voice design: prepend a parenthesized description to text (e.g. "(young
    woman, gentle)Hello"). Voice cloning: set 'voice' to a saved reference
    WAV; pass 'reference_text' for ultimate cloning fidelity.
    """
    import numpy as np

    cfg_value = float(_param(params, "cfg_scale", 2.0))
    inference_timesteps = int(_param(params, "nfe_step", 10))

    voice = params.get("voice") or params.get("reference_audio") or ""
    ref_text = params.get("reference_text") or ""
    ref_path = _resolve_reference_audio_path(voice) if voice else None
    _tmp_ref = None

    gen_kwargs = {
        "text": text,
        "cfg_value": cfg_value,
        "inference_timesteps": inference_timesteps,
        # The upstream default may silently run three complete regenerations
        # when its heuristic dislikes a sample. API callers already have an
        # explicit, observable retry policy; keep each worker call single-pass.
        "retry_badcase": False,
    }

    try:
        if ref_path:
            ref_path, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = ref_path
            gen_kwargs["reference_wav_path"] = ref_path
            # Ultimate cloning mode kicks in when prompt_wav_path + prompt_text
            # are also provided. We mirror reference_wav_path into prompt_wav_path
            # only when the user supplies a transcript.
            if ref_text:
                gen_kwargs["prompt_wav_path"] = ref_path
                gen_kwargs["prompt_text"] = ref_text

        wav = _model_obj.generate(**gen_kwargs)
        if hasattr(wav, "cpu"):
            wav = wav.cpu()
        if hasattr(wav, "numpy"):
            wav = wav.numpy()
        data = np.array(wav, dtype=np.float32).squeeze()
        sr = getattr(_model_obj.tts_model, "sample_rate", 48000)
        return data, sr
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


def _infer_voxtral(text: str, params: dict) -> tuple:
    """Voxtral 4B TTS via vllm-omni offline Python API.

    Voice resolves the same way as other models: if it points to a saved
    reference clip, clone from that audio; otherwise treat the value as a
    Mistral preset voice name (e.g. "casual_male"). cfg_alpha controls
    flow-matching guidance strength.
    """
    import numpy as np
    import torch
    from mistral_common.protocol.instruct.chunk import TextChunk
    from mistral_common.protocol.speech.request import SpeechRequest
    from vllm import SamplingParams

    omni, tokenizer = _model_obj
    instruct_tokenizer = tokenizer.instruct_tokenizer

    voice = params.get("voice") or params.get("reference_audio") or "casual_male"
    cfg_alpha = float(_param(params, "cfg_alpha", 1.2))
    seed = int(_param(params, "seed", 42))
    max_tokens = max(128, min(800, int(len(text) * 12) + 192))

    text_chunk = TextChunk(text=text)
    inputs: dict = {}
    ref_path = _resolve_reference_audio_path(voice) if voice else None
    _tmp_ref = None

    try:
        if ref_path:
            ref_path, was_trimmed = _trim_ref_audio_path(ref_path, max_seconds=30.0)
            if was_trimmed:
                _tmp_ref = ref_path
            with open(ref_path, "rb") as f:
                ref_audio_bytes = f.read()
            tokenized = instruct_tokenizer.encode_speech_request(
                SpeechRequest(input=text_chunk.text, ref_audio=ref_audio_bytes)
            )
            if not tokenized.audios:
                raise RuntimeError("Voxtral tokenizer returned no audio for ref clip")
            audio = tokenized.audios[0]
            inputs["multi_modal_data"] = {
                "audio": [(audio.audio_array, audio.sampling_rate)]
            }
        else:
            tokenized = instruct_tokenizer.encode_speech_request(
                SpeechRequest(input=text_chunk.text, voice=voice)
            )
            inputs["additional_information"] = {"voice": [voice]}
        inputs["prompt_token_ids"] = tokenized.tokens

        sampling_params = SamplingParams(
            max_tokens=max_tokens,
            seed=seed,
            extra_args={"cfg_alpha": cfg_alpha},
        )
        # vllm-omni expects a 2-element list (one per pipeline stage)
        sampling_params_list = [sampling_params, sampling_params]

        outputs = omni.generate(inputs, sampling_params_list)
        if not outputs:
            raise RuntimeError("Voxtral returned no outputs")
        multimodal_output = getattr(outputs[0], "multimodal_output", None) or {}
        audio_chunks = multimodal_output.get("audio")

        def _to_audio_tensor(value):
            if value is None:
                return None
            if torch.is_tensor(value):
                return value.detach().reshape(-1)
            if isinstance(value, np.ndarray):
                if value.size == 0:
                    return None
                return torch.from_numpy(value).reshape(-1)
            if isinstance(value, dict):
                for key in ("audio", "audio_array", "array", "waveform", "data"):
                    if key in value:
                        return _to_audio_tensor(value[key])
                return None
            if isinstance(value, (list, tuple)):
                chunks = []
                for chunk in value:
                    chunk_tensor = _to_audio_tensor(chunk)
                    if chunk_tensor is not None and chunk_tensor.numel() > 0:
                        chunks.append(chunk_tensor)
                if not chunks:
                    return None
                return torch.cat(chunks)
            try:
                array = np.asarray(value, dtype=np.float32)
            except (TypeError, ValueError):
                return None
            if array.size == 0:
                return None
            return torch.from_numpy(array).reshape(-1)

        audio_tensor = _to_audio_tensor(audio_chunks)
        if audio_tensor is None or audio_tensor.numel() == 0:
            raise RuntimeError("Voxtral returned no audio output")
        data = audio_tensor.float().detach().cpu().numpy().astype(np.float32, copy=False)
        return data, 24000
    finally:
        if _tmp_ref:
            try:
                os.unlink(_tmp_ref)
            except OSError:
                pass


# ============================================================
# Whisper inference (for whisper worker mode)
# ============================================================
def _get_whisper_model(size):
    import whisper
    if size not in whisper.available_models():
        raise ValueError(f"Unknown Whisper size: {size}")
    if not _loaded:
        load_model()
    with _load_lock:
        if size not in _model_obj:
            # Transactional eviction: load the new model FIRST, then mutate the
            # caches only after a successful load. If load_model raises
            # (download/OOM), the previously-cached LRU model is preserved
            # instead of being needlessly discarded.
            logger.info("Loading whisper model size '%s'...", size)
            new_model = whisper.load_model(
                size,
                device=_device,
                download_root=str(MODELS_DIR / "whisper"),
            )
            logger.info("Whisper '%s' loaded", size)
            if len(_model_obj) >= 2:
                lru_size = min(_model_obj, key=lambda key: _whisper_access_times.get(key, 0))
                logger.info("Evicting whisper model '%s' (LRU) to load '%s'", lru_size, size)
                del _model_obj[lru_size]
                _whisper_access_times.pop(lru_size, None)
                gc.collect()
                try:
                    import torch as _t
                    if _t.cuda.is_available():
                        _t.cuda.empty_cache()
                except (ImportError, RuntimeError):
                    pass
            _model_obj[size] = new_model
        _whisper_access_times[size] = time.time()
        return _model_obj[size]



def transcribe(audio_path: str, size: str = "base",
               word_timestamps: bool = False,
               language: str | None = None,
               task: str = "transcribe",
               initial_prompt: str | None = None) -> dict:
    """Transcribe audio using whisper. Only available in whisper worker mode.

    Args:
        audio_path: path to audio file
        size: whisper model size (tiny/base/small/medium/large)
        word_timestamps: when True, response contains a flat ``words`` list
            ([{word, start, end}, ...]) for SRT/alignment use.
        language: optional ISO 639-1 code (e.g. "en", "ja") to force a language.
            None lets Whisper auto-detect.
        task: "transcribe" (default) keeps audio in its source language;
            "translate" translates to English.
        initial_prompt: optional bounded source-script hint. This improves
            proper nouns and near-homophones while Whisper still owns timing.
    """
    if _model_name != "whisper":
        raise RuntimeError("This worker is not a whisper worker")

    model = _get_whisper_model(size)

    transcribe_kwargs: dict = {"word_timestamps": word_timestamps, "task": task}
    if language:
        transcribe_kwargs["language"] = language
    if initial_prompt:
        # Whisper has a small text-context window; reject accidental project-
        # sized payloads at the worker boundary instead of wasting tokenizer
        # and decoder memory. The gateway already applies this same bound.
        transcribe_kwargs["initial_prompt"] = initial_prompt[:2000]
    result = model.transcribe(audio_path, **transcribe_kwargs)
    out = {"text": result.get("text", ""), "language": result.get("language", "")}
    if word_timestamps:
        words: list[dict] = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append({
                    "word": (w.get("word") or "").strip(),
                    "start": w.get("start"),
                    "end": w.get("end"),
                })
        out["words"] = words
    return out


# ============================================================
# FastAPI app
# ============================================================
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional

worker_app = FastAPI(title="TTS Worker", version="1.0.0")


class InferRequest(BaseModel):
    text: str
    params: dict = {}

    @field_validator("text")
    @classmethod
    def text_not_empty_or_too_long(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Text must not be empty")
        if len(v) > 50000:
            raise ValueError(f"Text too long ({len(v)} chars, max 50000)")
        return v


class TranscribeRequest(BaseModel):
    audio_path: str
    size: str = "base"
    word_timestamps: bool = False
    language: Optional[str] = None
    task: str = "transcribe"  # "transcribe" or "translate"
    initial_prompt: Optional[str] = None


@worker_app.get("/health")
async def health():
    vram_used = 0
    vram_total = 0
    try:
        import torch
        if torch.cuda.is_available():
            # Get VRAM for the device this worker uses
            dev_idx = 0
            if _device.startswith("cuda:"):
                try:
                    dev_idx = int(_device.split(":")[1])
                except (ValueError, IndexError):
                    dev_idx = 0
            # NOTE: memory_reserved is THIS process's PyTorch reserved cache only
            # (excludes other processes / non-PyTorch allocations). For true
            # device-wide free VRAM the manager uses nvidia-smi (detect_devices).
            vram_used = torch.cuda.memory_reserved(dev_idx) // (1024 * 1024)
            vram_total = torch.cuda.get_device_properties(dev_idx).total_memory // (1024 * 1024)
    except (ImportError, RuntimeError, AttributeError):
        pass

    return {
        "status": "ready" if _loaded else "idle",
        "model": _model_name,
        "device": _device,
        "pid": os.getpid(),
        "loaded_sizes": sorted(_model_obj) if _model_name == "whisper" and isinstance(_model_obj, dict) else [],
        "vram_used_mb": vram_used,
        "vram_total_mb": vram_total,
    }


@worker_app.post("/load")
def api_load():
    with _load_lock:
        try:
            load_model()
            return {"status": "loaded", "model": _model_name}
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error("Model load failed:\n%s", tb)
            # Release any GPU tensors a partial load already allocated so a
            # surviving worker doesn't strand VRAM before re-raising.
            _teardown_model_obj()
            raise HTTPException(status_code=500, detail=f"{e}\n{tb[-1000:]}")


@worker_app.post("/unload")
def api_unload():
    unload_model()
    return {"status": "unloaded", "model": _model_name}


@worker_app.post("/infer")
def api_infer(req: InferRequest):
    """Synchronous endpoint — FastAPI runs it in a thread pool,
    keeping the event loop free for /health checks during long inferences."""
    import numpy as np

    text_preview = req.text[:80] + "..." if len(req.text) > 80 else req.text
    logger.info("Received /infer request: text=%r", text_preview)

    with _load_lock:
        if not _loaded:
            # Auto-load on first inference
            try:
                load_model()
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error("Model load failed (auto-load on /infer):\n%s", tb)
                # Release any GPU tensors a partial load already allocated.
                _teardown_model_obj()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to load model: {e}\n{tb[-1000:]}",
                )

    try:
        logger.info("Starting inference for model=%s ...", _model_name)
        infer_start = time.time()
        result = infer(req.text, req.params)

        # Bark returns (audio, sr, history); others return (audio, sr)
        if _model_name == "bark" and len(result) == 3:
            audio_data, sample_rate, bark_history = result
        else:
            audio_data, sample_rate = result
            bark_history = None

        logger.info("Inference complete in %.1fs (sr=%d, samples=%d)",
                    time.time() - infer_start, sample_rate, len(audio_data))

        # Encode numpy array as base64
        buf = io.BytesIO()
        np.save(buf, audio_data)
        audio_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        response = {
            "audio_b64": audio_b64,
            "sample_rate": sample_rate,
            "dtype": str(audio_data.dtype),
        }

        # For bark, include updated history prompt
        if bark_history is not None:
            history_parts = []
            for arr in bark_history:
                hbuf = io.BytesIO()
                np.save(hbuf, np.array(arr) if not isinstance(arr, np.ndarray) else arr)
                history_parts.append(base64.b64encode(hbuf.getvalue()).decode("utf-8"))
            response["bark_history"] = history_parts
            del bark_history  # release GPU tensor references before responding

        # Free intermediate data so the finally block's empty_cache can
        # reclaim them. Drop only on the success path (they exist here).
        del audio_data, buf

        return JSONResponse(response)

    except Exception as e:
        logger.error("Inference error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Release cached CUDA memory on BOTH the success and failure paths so a
        # failed generate() (OOM, bad params) does not leave fragmented/reserved
        # VRAM behind for the gateway's retries against this long-lived worker.
        # Guarded so it never masks the original exception.
        try:
            gc.collect()
            import torch as _t
            if _t.cuda.is_available():
                _t.cuda.empty_cache()
        except (ImportError, RuntimeError):
            pass


@worker_app.post("/whisper/{size}/load")
def api_whisper_load(size: str):
    if _model_name != "whisper":
        raise HTTPException(status_code=400, detail="Not a Whisper worker")
    _get_whisper_model(size)
    return {"status": "loaded", "size": size}


@worker_app.post("/whisper/{size}/unload")
def api_whisper_unload(size: str):
    if _model_name != "whisper":
        raise HTTPException(status_code=400, detail="Not a Whisper worker")
    with _load_lock:
        if isinstance(_model_obj, dict):
            _model_obj.pop(size, None)
        _whisper_access_times.pop(size, None)
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return {"status": "unloaded", "size": size}


@worker_app.post("/transcribe")
def api_transcribe(req: TranscribeRequest):
    """Whisper transcription endpoint (only for whisper workers)."""
    if _model_name != "whisper":
        raise HTTPException(status_code=400, detail="Not a whisper worker")

    # Validate audio path is within allowed directories (output, voices, projects)
    audio_path = Path(req.audio_path).resolve()
    _allowed = (OUTPUT_DIR.resolve(), VOICE_DIR.resolve(), PROJECTS_OUTPUT.resolve())
    if not any(audio_path.is_relative_to(base) for base in _allowed):
        raise HTTPException(status_code=400, detail="Audio path must be in output, voices, or projects directory")
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    if not _loaded:
        try:
            load_model()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load whisper: {e}")

    try:
        result = transcribe(
            str(audio_path), req.size, req.word_timestamps,
            req.language, req.task, req.initial_prompt,
        )
        return result
    except Exception as e:
        logger.error("Transcribe error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Main entry point
# ============================================================
def main():
    global _model_name, _device, _precision

    parser = argparse.ArgumentParser(description="TTS Worker Server")
    parser.add_argument("--model", required=True, help="Model to load (kokoro, xtts, etc.)")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--device", default="cuda:0", help="CUDA device (cuda:0, cuda:1, cpu)")
    parser.add_argument("--precision", default=None, choices=["fp32", "fp16", "bf16"],
                        help="Weight precision (default: auto per model)")
    parser.add_argument("--preload", action="store_true", help="Load model immediately on startup")
    args = parser.parse_args()

    _model_name = args.model
    _device = args.device
    _precision = args.precision

    # Inject the model's venv into sys.path
    _inject_venv(_model_name)

    logger.info("Starting worker: model=%s port=%d device=%s", _model_name, args.port, _device)

    # Log the actual physical GPU so the user can verify the right card was selected.
    # After CUDA_VISIBLE_DEVICES filtering, cuda:0 could be any physical GPU.
    if _device.startswith("cuda"):
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                vram_mb = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
                vis = os.environ.get("CUDA_VISIBLE_DEVICES", "all")
                logger.info("Physical GPU: %s (%d MB) [CUDA_VISIBLE_DEVICES=%s]",
                            gpu_name, vram_mb, vis)
        except Exception as e:
            logger.warning("Could not identify physical GPU: %s", e)

    if args.preload:
        load_model()

    import uvicorn
    uvicorn.run(worker_app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
