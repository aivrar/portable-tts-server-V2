"""
TTS API Server - Gateway + Worker Architecture

The gateway runs on port 8300 and orchestrates:
- Text chunking and normalization
- Audio post-processing (de-reverb, high-pass, de-ess, trim, normalize, peak limit)
- Whisper verification (via whisper worker subprocess)
- Job tracking with recovery and cancellation
- Format conversion (wav/mp3/ogg/flac/m4a)
- Worker lifecycle management (spawn/kill/scale/health)
- Load balancing across multiple worker instances
- Multi-GPU support

Workers are separate subprocesses, each running one TTS model instance.
The gateway delegates inference to workers via HTTP.

Run with: uvicorn tts_api_server:app --host 0.0.0.0 --port 8300
"""

import asyncio
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import traceback
from pathlib import Path

# Bootstrap: ensure project root is on sys.path for sibling imports
_BASE_DIR = Path(__file__).parent.resolve()
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

import io
import queue as queue_mod
import shutil
import uuid
import gc
import time
import base64
import logging

import soundfile as sf

from typing import Any, Optional
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, ValidationError
import uvicorn

from config import (
    APP_DIR, BASE_DIR, MODELS_DIR, OUTPUT_DIR, REPOS_DIR,
    DEFAULT_API_HOST, DEFAULT_API_PORT, DEFAULT_BRIDGE_PORT, JOBS_DIR, PROJECTS_OUTPUT,
    VOICE_DIR, MAX_RETRIES, WHISPER_MODEL_SIZE, WHISPER_ENABLED,
    WHISPER_DEFAULT_TOLERANCE, WHISPER_AVAILABLE_MODELS, MAX_INFERENCE_WORKERS,
    WORKER_AUTO_SPAWN, WORKER_DEFAULT_DEVICE,
    RUN_DIR, WORKER_PORT_MIN, WORKER_PORT_MAX, WORKER_LOG_DIR,
    MODEL_OVERRIDE_MAP, MODEL_SETUP, MODEL_VRAM_ESTIMATE_GB, MODEL_DEFAULTS, MODEL_FIELDS, PARAM_CONFIG, PARAM_OVERRIDES, TOOLTIPS,
    MODEL_INFER_TIMEOUT, DEFAULT_INFER_TIMEOUT,
    VENV_DIR, OVERRIDES_DIR, HF_TOKEN_FILE, LEGACY_HF_TOKEN_FILE,
    SECRETS_DIR, TMP_DIR, CACHE_DIR,
    setup_environment,
)

from text_utils import chunk_text_for_model
from audio_profiles import PROFILES
from audio_processing import post_process, verify_with_whisper
from audio_assembler import assemble_chunks, convert_format
import audio_editor
from job_manager import JobManager
from worker_registry import WorkerRegistry
from worker_manager import WorkerManager
import discovery

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
setup_environment()
OUTPUT_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_OUTPUT.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)
DELIVERED_JOBS_DIR = OUTPUT_DIR / "delivered_jobs"
DELIVERED_JOBS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RUN_DIR.mkdir(parents=True, exist_ok=True)
_API_TOKEN_FILE = RUN_DIR / "api_token"
# Persist a local token across restarts. Only the process holding the gateway
# lock may publish credentials or recover jobs (see lifespan).
_API_TOKEN = os.environ.get("TTS_API_TOKEN") or (
    _API_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if _API_TOKEN_FILE.exists() else secrets.token_urlsafe(32)
)
_gateway_lock_file = None

# Actual listening port — set by start_server() at launch. Defaults to config.
_GATEWAY_PORT: int = DEFAULT_API_PORT
_BRIDGE_PORT: int = int(os.environ.get("BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
_REGISTRY_NAME = "tts_server"

def _read_app_version() -> str:
    """Pull version from app.json if present; default to 1.0.0."""
    app_json = BASE_DIR.parent / "app.json"
    if not app_json.exists():
        return "1.0.0"
    try:
        return json.loads(app_json.read_text(encoding="utf-8")).get("version", "1.0.0")
    except (json.JSONDecodeError, OSError):
        return "1.0.0"

APP_VERSION = _read_app_version()

# ---------------------------------------------------------------------------
# Worker infrastructure
# ---------------------------------------------------------------------------
registry = WorkerRegistry(WORKER_PORT_MIN, WORKER_PORT_MAX)
worker_manager = WorkerManager(registry)
_model_load_tasks: dict[str, asyncio.Task] = {}
_model_load_states: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
# Shared reentrancy guard so the graceful worker teardown runs exactly once even
# if both the signal handler and the lifespan shutdown reach it (uvicorn restores
# our handlers and re-raises the signal after serve() returns).
_workers_killed_once = threading.Event()


def _kill_workers_sync(graceful_timeout: float = 3.0, term_timeout: float = 2.0):
    """Synchronously stop all worker subprocesses (for signal handlers).

    Three-phase shutdown so model-specific cleanup (e.g. vLLM `omni.shutdown()`
    in tts_worker.unload_model) gets a chance to run before the process dies:
      1. POST /unload on each worker (best-effort, short timeout).
      2. SIGTERM the worker's process group; wait up to `term_timeout`.
      3. SIGKILL the process group if anything is still alive.

    Workers are spawned with start_new_session=True so killpg catches children.
    Reentrancy-guarded: only the first caller performs the teardown.
    """
    if _workers_killed_once.is_set():
        return
    _workers_killed_once.set()
    import urllib.request
    import urllib.error

    workers = list(registry.all_workers())
    if not workers:
        return

    # Phase 1: best-effort graceful model unload via HTTP (parallel via threads).
    def _request_unload(w):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{w.port}/unload",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=graceful_timeout).close()
        except (urllib.error.URLError, OSError, ValueError):
            pass

    unload_threads = []
    for w in workers:
        try:
            if w.process and w.process.poll() is None:
                t = threading.Thread(target=_request_unload, args=(w,), daemon=True)
                t.start()
                unload_threads.append(t)
        except Exception:
            pass
    deadline = time.monotonic() + graceful_timeout + 0.5
    for t in unload_threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)

    # Phase 2: SIGTERM the process group.
    for w in workers:
        try:
            if w.process and w.process.poll() is None:
                try:
                    pgid = os.getpgid(w.process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    try:
                        w.process.terminate()
                    except Exception:
                        pass
        except Exception:
            pass

    # Wait for workers to exit after SIGTERM.
    term_deadline = time.monotonic() + term_timeout
    for w in workers:
        if not w.process:
            continue
        remaining = max(0.0, term_deadline - time.monotonic())
        try:
            w.process.wait(timeout=remaining if remaining > 0 else 0.1)
        except Exception:
            pass

    # Phase 3: SIGKILL anything still alive.
    for w in workers:
        try:
            if w.process and w.process.poll() is None:
                try:
                    pgid = os.getpgid(w.process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        w.process.kill()
                    except Exception:
                        pass
                try:
                    w.process.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if w.log_fh:
                w.log_fh.close()
        except Exception:
            pass


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT — unload models, kill workers, unpublish before we die."""
    logger.info("Signal %d received — shutting down workers gracefully...", signum)
    _kill_workers_sync()
    try:
        discovery.unpublish(
            _REGISTRY_NAME,
            fallback_dir=str(RUN_DIR),
            expected_pid=os.getpid(),
            expected_token=_API_TOKEN,
        )
    except Exception:
        pass
    try:
        worker_manager.kill_orphan_workers()
    except Exception:
        pass
    logger.info("Shutdown complete — exiting")
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the gateway lock throughout startup, serving and shutdown."""
    global _gateway_lock_file
    import fcntl
    lock_file = (RUN_DIR / "gateway.lock").open("a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError("A TTS gateway is already running")
    _gateway_lock_file = lock_file
    try:
        async with _gateway_lifecycle(app):
            yield
    finally:
        lock_file.close()
        if _gateway_lock_file is lock_file:
            _gateway_lock_file = None


@asynccontextmanager
async def _gateway_lifecycle(app: FastAPI):
    """Start health checks and stop workers when the gateway shuts down."""
    global _API_TOKEN
    if not os.environ.get("TTS_API_TOKEN") and _API_TOKEN_FILE.exists():
        _API_TOKEN = _API_TOKEN_FILE.read_text(encoding="utf-8").strip() or _API_TOKEN
    token_tmp = _API_TOKEN_FILE.with_suffix(".part")
    token_tmp.write_text(_API_TOKEN, encoding="utf-8")
    token_tmp.chmod(0o600)
    token_tmp.replace(_API_TOKEN_FILE)
    (RUN_DIR / "shutdown.requested").unlink(missing_ok=True)
    # Clean up stale state from previous run
    from config import MAX_JOB_AGE_HOURS
    job_manager.recover_stale_running_jobs()
    job_manager.cleanup_old_jobs(MAX_JOB_AGE_HOURS)
    await asyncio.to_thread(job_manager.list_jobs, limit=None)

    # Kill any orphan worker processes from a previous crash
    worker_manager.kill_orphan_workers()

    # Clean up orphaned temp files from previous crashes
    for _pattern in ("raw_*_*.wav", "assembled_*.wav", "ref_*.*"):
        for _tmp in OUTPUT_DIR.glob(_pattern):
            try:
                _tmp.unlink()
                logger.info("Cleaned up orphaned temp file: %s", _tmp.name)
            except OSError:
                pass

    # Clean up old worker logs (keep last 50, delete the rest)
    from config import WORKER_LOG_DIR
    if WORKER_LOG_DIR.exists():
        logs = sorted(WORKER_LOG_DIR.glob("worker_*.log"), key=lambda f: f.stat().st_mtime)
        if len(logs) > 50:
            for old_log in logs[:-50]:
                try:
                    old_log.unlink()
                except OSError:
                    pass
            logger.info("Cleaned up %d old worker log files", len(logs) - 50)

    worker_manager.start_health_checks()

    # Publish discovery registry so peer apps can find us
    def _local_network_host() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect(("8.8.8.8", 80))
                host = s.getsockname()[0]
        except OSError:
            return ""
        if not host or host.startswith("127."):
            return ""
        return host

    def _local_http_ok(port: int, path: str = "/health", timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{int(port)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                s.sendall(req)
                head = s.recv(128)
        except OSError:
            return False
        return head.startswith(b"HTTP/1.1 2") or head.startswith(b"HTTP/1.0 2")

    try:
        bridge_alive = _local_http_ok(_BRIDGE_PORT, path="/bridge-health")
        primary_port = _BRIDGE_PORT if bridge_alive else _GATEWAY_PORT
        endpoints = {
            "api": f"http://127.0.0.1:{primary_port}",
            "gateway": f"http://127.0.0.1:{_GATEWAY_PORT}",
            "bridge": f"http://127.0.0.1:{_BRIDGE_PORT}",
            "openapi": f"http://127.0.0.1:{primary_port}/openapi.json",
            "docs": f"http://127.0.0.1:{primary_port}/docs",
        }
        wsl_host = _local_network_host()
        if wsl_host:
            endpoints["wsl_api"] = f"http://{wsl_host}:{primary_port}"
            endpoints["wsl_gateway"] = f"http://{wsl_host}:{_GATEWAY_PORT}"
            endpoints["wsl_bridge"] = f"http://{wsl_host}:{_BRIDGE_PORT}"
            endpoints["wsl_openapi"] = f"http://{wsl_host}:{primary_port}/openapi.json"
            endpoints["wsl_docs"] = f"http://{wsl_host}:{primary_port}/docs"
        published = discovery.publish(
            _REGISTRY_NAME,
            endpoints=endpoints,
            auth={"header": "X-TTS-API-Token", "token": _API_TOKEN},
            app_dir=str(APP_DIR.resolve()),
            version=APP_VERSION,
            extra={
                "models": list(MODEL_SETUP.keys()),
                "default_device": str(WORKER_DEFAULT_DEVICE),
                # Surface the WSL distro name so Windows-side callers can
                # build \\wsl$\<distro>\opt\... UNC paths to open Linux-side
                # directories (cache, models, venv) in Explorer.
                "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
                "bridge_alive": bridge_alive,
            },
            fallback_dir=str(RUN_DIR),
        )
        logger.info("Discovery registry published at %s", published)
    except Exception as e:
        logger.warning("Could not publish discovery registry: %s", e)

    logger.info("Gateway started - worker health checks active")
    yield
    logger.info("Gateway shutting down - killing all workers...")
    worker_manager.stop_health_checks()
    # Run the graceful 3-phase teardown (HTTP /unload first) so model-specific
    # cleanup (e.g. vLLM omni.shutdown()) runs on the normal uvicorn shutdown
    # path, not just on a signal that reaches _signal_handler. Off-loop because
    # it is synchronous and does blocking HTTP + waits. Reentrancy-guarded so it
    # does not double-run if _signal_handler also fires.
    try:
        await asyncio.to_thread(_kill_workers_sync)
    except Exception as e:
        logger.warning("Graceful worker unload failed: %s", e)
    killed = await worker_manager.kill_all_workers()
    worker_manager.kill_orphan_workers()
    # Signal any in-flight pipeline threads to abort so their finally blocks run
    # (cleaning the in-memory maps + writing a consistent on-disk status) before
    # we abandon the executor.
    try:
        with _running_jobs_lock:
            _active_ids = {jid for ids in _running_jobs.values() for jid in ids}
        for _jid in _active_ids:
            try:
                job_manager.request_cancel(_jid)
            except Exception:
                pass
    except Exception:
        pass
    executor.shutdown(wait=False)
    try:
        discovery.unpublish(
            _REGISTRY_NAME,
            fallback_dir=str(RUN_DIR),
            expected_pid=os.getpid(),
            expected_token=_API_TOKEN,
        )
    except Exception as e:
        logger.warning("Could not unpublish discovery registry: %s", e)
    logger.info("All workers stopped (%d killed), memory reclaimed", killed)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="TTS API Gateway",
    description="Gateway server orchestrating TTS workers with full pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:9300", "http://127.0.0.1:9300",
        "http://localhost:8300", "http://127.0.0.1:8300",
        "http://localhost:9091", "http://127.0.0.1:9091",
        "http://localhost:8100", "http://127.0.0.1:8100",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _requires_api_token(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    return request.url.path.startswith("/api/")


def _extract_api_token(request: Request) -> str:
    token = request.headers.get("x-tts-api-token", "")
    if token:
        return token
    token = request.query_params.get("token", "")
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


_maintenance_busy = False
_active_api_requests = 0
_active_exports = 0


@app.middleware("http")
async def track_api_activity(request: Request, call_next):
    global _active_api_requests
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if _maintenance_busy:
        return JSONResponse({"detail": "Cache maintenance is active"}, status_code=503)
    _active_api_requests += 1
    try:
        return await call_next(request)
    finally:
        _active_api_requests -= 1


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if _requires_api_token(request):
        supplied = _extract_api_token(request)
        # Compare encoded bytes so a non-ASCII supplied token yields a clean 401
        # instead of an unhandled TypeError (compare_digest rejects non-ASCII
        # str). Encoding both sides preserves the constant-time guarantee.
        if not secrets.compare_digest(supplied.encode("utf-8"), _API_TOKEN.encode("utf-8")):
            return JSONResponse(
                {"detail": "Missing or invalid API token"},
                status_code=401,
            )
    return await call_next(request)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
job_manager = JobManager(JOBS_DIR)


def _delivered_manifest_path(job_id: str) -> Path:
    safe = str(job_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", safe):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    return DELIVERED_JOBS_DIR / f"{safe}.json"


def _read_delivered_job(job_id: str) -> dict | None:
    path = _delivered_manifest_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read delivered job manifest %s: %s", path, e)
        return None


def _write_delivered_job(job: dict) -> None:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    path = _delivered_manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _archive_delivered_job_manifest(job_id: str, delivered_info: dict | None) -> None:
    job = job_manager.get_job(job_id)
    if not job:
        return
    archived = dict(job)
    archived["status"] = archived.get("status") or "completed"
    archived["delivered_to"] = delivered_info or {}
    if delivered_info:
        srt_info = archived.get("srt")
        if isinstance(srt_info, dict):
            srt_info = dict(srt_info)
            if delivered_info.get("srt"):
                srt_info["srt_path"] = delivered_info["srt"]
            if delivered_info.get("timing"):
                srt_info["timing_path"] = delivered_info["timing"]
            archived["srt"] = srt_info
        if delivered_info.get("srt"):
            archived["srt_path"] = delivered_info["srt"]
        if delivered_info.get("timing"):
            archived["srt_timing_path"] = delivered_info["timing"]
        if delivered_info.get("audio"):
            archived["saved_to"] = delivered_info["audio"]
    archived["purged_work_dir"] = True
    archived["job_dir"] = ""
    archived["delivered_manifest"] = str(_delivered_manifest_path(job_id))
    _write_delivered_job(archived)


def _get_job_or_delivered(job_id: str) -> dict | None:
    return job_manager.get_job(job_id) or _read_delivered_job(job_id)


def _delivered_artifact_path(job: dict, key: str) -> Path | None:
    delivered = job.get("delivered_to") if isinstance(job, dict) else None
    if not isinstance(delivered, dict):
        return None
    raw = delivered.get(key)
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.exists() else None
executor = ThreadPoolExecutor(max_workers=MAX_INFERENCE_WORKERS)

# Track running job_id per model for cancellation
_running_jobs: dict[str, set[str]] = {}  # model -> set of active job_ids
_running_jobs_lock = threading.Lock()

# Bark history prompt state: { job_id: history_data }
# Gateway manages this, passes to worker per-chunk, receives updated history back
_bark_history: dict[str, object] = {}
_bark_history_lock = threading.Lock()

# Prevent concurrent auto-spawns for the same model (pre-populated, no runtime lock needed)
_spawn_locks: dict[str, asyncio.Lock] = {m: asyncio.Lock() for m in MODEL_SETUP}
_pipeline_spawn_locks: dict[str, threading.Lock] = {m: threading.Lock() for m in MODEL_SETUP}

# Track active model installations (thread-safe)
_active_installs: dict[str, bool] = {}
_active_install_meta: dict[str, dict] = {}
_active_install_processes: dict[str, Any] = {}
_install_cancel_all = threading.Event()
_install_cancel_models: set[str] = set()
_active_installs_lock = threading.Lock()

_DEFAULT_SETUP_INSTALL_TIMEOUT_SEC = int(os.environ.get("TTS_SETUP_INSTALL_TIMEOUT_SEC", "7200"))
_SETUP_INSTALL_TIMEOUTS_SEC = {
    "dia": 10800,
    "qwen": 14400,
    "higgs": 14400,
    "voxtral": 14400,
    "voxcpm2": 14400,
    "orpheus": 14400,
}
_SETUP_INSTALL_ATTEMPTS = max(1, int(os.environ.get("TTS_SETUP_INSTALL_ATTEMPTS", "2")))


def _setup_install_timeout_sec(model: str) -> int:
    return max(300, _SETUP_INSTALL_TIMEOUTS_SEC.get(model, _DEFAULT_SETUP_INSTALL_TIMEOUT_SEC))

# SSE log subscriber list lock
_log_subscribers_lock = threading.Lock()

# Cached whisper model for local fallback verification (avoids reloading per-call)
# Uses OrderedDict for LRU eviction (max 2 models cached)
from collections import OrderedDict
_whisper_cache: OrderedDict[str, object] = OrderedDict()
_whisper_cache_lock = threading.Lock()
_WHISPER_CACHE_MAX = 2

_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
_OUTPUT_FORMATS = {"wav", "mp3", "ogg", "flac", "m4a"}
_MAX_REFERENCE_AUDIO_BYTES = 50 * 1024 * 1024
_MAX_PIPELINE_TEXT_CHARS = int(os.environ.get("TTS_SERVER_MAX_TEXT_CHARS", "100000"))
_MAX_PIPELINE_CHUNKS = int(os.environ.get("TTS_SERVER_MAX_CHUNKS", "500"))
_MAX_INLINE_AUDIO_BYTES = int(os.environ.get("TTS_SERVER_MAX_INLINE_AUDIO_BYTES", str(25 * 1024 * 1024)))
_PIPELINE_PARAM_DEFAULTS = {
    "speed": 1.0,
    "temperature": 0.65,
    "repetition_penalty": 2.0,
    # Destructive cleanup is opt-in. Clean TTS output should not be passed
    # through dereverberation merely because a caller omitted the field.
    "de_reverb": 0.0,
    "de_ess": 0.0,
    "tolerance": WHISPER_DEFAULT_TOLERANCE,
}


def _ts():
    return time.strftime("%H:%M:%S")


# ============================================================
# Request models
# ============================================================
class PipelineTTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=_MAX_PIPELINE_TEXT_CHARS)
    voice: Optional[str] = None
    reference_audio: Optional[str] = None      # file path or base64 audio data
    # VibeVoice JSON API: one reference per Speaker N, in speaker order.
    reference_audios: Optional[list[str]] = Field(default=None, max_length=4)
    reference_audio_name: Optional[str] = None  # original filename (when base64)
    reference_text: Optional[str] = None
    language: Optional[str] = "en"
    # None means "use this engine's tuned default". Keeping these dynamic is
    # important for API callers that do not first read /api/config.
    speed: Optional[float] = Field(default=None, gt=0, le=4, allow_inf_nan=False)
    temperature: Optional[float] = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    repetition_penalty: Optional[float] = Field(default=None, gt=0, le=20, allow_inf_nan=False)
    de_reverb: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    de_ess: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    tolerance: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    verify_whisper: Optional[bool] = False
    whisper_model: Optional[str] = None
    output_format: Optional[str] = "wav"
    save_path: Optional[str] = None
    skip_post_process: Optional[bool] = False
    auto_retry: Optional[int] = Field(default=None, ge=0, le=10)
    device: Optional[str] = None
    worker_id: Optional[str] = None
    mode: Optional[str] = "cloned"
    # External delivery: write only the final audio (+ SRT/timing if generated)
    # to an absolute path outside the TTS server, then purge the work dir.
    # Mutually exclusive with save_path.
    deliver_to: Optional[str] = None
    generate_srt: Optional[bool] = False
    srt_words_per_line: Optional[int] = Field(default=3, ge=1, le=20)
    srt_size: Optional[str] = "base"
    # Model-native inference parameters. These mirror MODEL_DEFAULTS/PARAM_CONFIG
    # and must be preserved when the UI submits JSON or multipart requests.
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=200)
    cfg_scale: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    exaggeration: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    cfg_weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    cfg_alpha: Optional[float] = Field(default=None, ge=0.5, le=3.0)
    waveform_temperature: Optional[float] = Field(default=None, ge=0.0, le=3.0)
    seed: Optional[int] = Field(default=None, ge=0, le=99999)
    nfe_step: Optional[int] = Field(default=None, ge=4, le=128)
    pitch: Optional[int] = Field(default=None, ge=-50, le=50)
    volume: Optional[int] = Field(default=None, ge=-50, le=50)
    speaker_idx: Optional[int] = Field(default=None, ge=0)
    voice_description: Optional[str] = None
    # NOTE: max_new_tokens / chunk_length are NOT user-controllable. The text
    # splitter (chunk_text_for_model) owns chunk sizing per model, and the
    # worker derives max_new_tokens from the chunk length internally.
    # Audio profile overrides (None = use model default)
    inter_pause_sec: Optional[float] = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    front_pad_sec: Optional[float] = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    padding_sec: Optional[float] = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    trim_db: Optional[float] = Field(default=None, ge=-120, le=0, allow_inf_nan=False)
    min_silence_ms: Optional[int] = Field(default=None, ge=0, le=60000)
    front_protect_ms: Optional[int] = Field(default=None, ge=0, le=60000)
    end_protect_ms: Optional[int] = Field(default=None, ge=0, le=60000)
    clipping: Optional[float] = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    lufs: Optional[float] = Field(default=None, ge=-70, le=0, allow_inf_nan=False)

    @field_validator("device")
    @classmethod
    def valid_device(cls, value):
        return WorkerManager.normalize_device(value) if value else None

    @field_validator("output_format")
    @classmethod
    def output_format_supported(cls, value: str | None) -> str:
        fmt = (value or "wav").lower().strip()
        if fmt not in _OUTPUT_FORMATS:
            raise ValueError(f"Unsupported output_format '{value}'. Choose from: {sorted(_OUTPUT_FORMATS)}")
        return fmt

    @field_validator("srt_size")
    @classmethod
    def srt_size_supported(cls, value: str | None) -> str:
        v = (value or "base").lower().strip()
        if v not in WHISPER_AVAILABLE_MODELS:
            raise ValueError(f"Unsupported srt_size '{value}'. Choose from: {list(WHISPER_AVAILABLE_MODELS.keys())}")
        return v


def _effective_pipeline_params(model: str, req: PipelineTTSRequest) -> dict:
    """Resolve common and per-model defaults without overriding user values."""
    params = req.model_dump()
    explicit = req.model_fields_set
    for name, value in _PIPELINE_PARAM_DEFAULTS.items():
        if name not in explicit or params.get(name) is None:
            params[name] = value
    for name, value in MODEL_DEFAULTS.get(model, {}).items():
        if name not in explicit or params.get(name) is None:
            params[name] = value
    return params


class CancelRequest(BaseModel):
    job_id: Optional[str] = None


class SpawnRequest(BaseModel):
    model: str
    device: Optional[str] = None
    precision: Optional[str] = None  # "fp32", "fp16", "bf16", or None (auto)

    @field_validator("model")
    @classmethod
    def model_known(cls, value: str) -> str:
        if value not in MODEL_SETUP:
            raise ValueError(f"Unknown model: {value}")
        return value


class ScaleRequest(BaseModel):
    count: int = Field(..., ge=0, le=16)
    device: Optional[str] = None


# ============================================================
# Health / discovery
# ============================================================
@app.get("/health")
async def health():
    workers = registry.all_workers()
    loaded_models = list(set(w.model for w in workers if w.status in ("ready", "busy")))
    return {
        "status": "ok",
        "message": "TTS API Gateway running",
        "loaded_models": loaded_models,
        "worker_count": len(workers),
    }


@app.get("/api/live")
async def live():
    return {"ok": True, "status": "ok", "app": "tts_server"}


@app.get("/api/ready")
async def ready(model: Optional[str] = None, timeout: float = 0.0):
    """Poll-style readiness probe.

    With no model: returns immediately with server status.
    With ?model=X: waits up to ``timeout`` seconds for at least one ready
    worker for that model. Returns 200 when ready, 503 on timeout.
    """
    if model is not None and model not in MODEL_SETUP:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model}")

    deadline = time.monotonic() + max(0.0, min(float(timeout), 600.0))
    while True:
        if model is None:
            workers = registry.all_workers()
            return {
                "status": "ok",
                "ready": True,
                "worker_count": len(workers),
                "loaded_models": sorted(
                    {w.model for w in workers if w.status in ("ready", "busy")}
                ),
            }
        ready_workers = registry.get_ready_workers(model)
        if ready_workers:
            return {
                "status": "ok",
                "ready": True,
                "model": model,
                "workers": [w.worker_id for w in ready_workers],
            }
        if time.monotonic() >= deadline:
            return JSONResponse(
                {"status": "timeout", "ready": False, "model": model},
                status_code=503,
            )
        await asyncio.sleep(0.5)


@app.get("/api/peers")
async def list_peers():
    """Return all live peer apps from the discovery registry."""
    try:
        # File I/O + per-PID liveness checks — keep off the event loop.
        peers = await asyncio.to_thread(discovery.list_peers, fallback_dir=str(RUN_DIR))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read registry: {e}")
    # Strip auth tokens and internal "_file" annotations from peer responses.
    # Callers who need the token can read the registry file themselves.
    sanitized = []
    for p in peers:
        copy = {k: v for k, v in p.items() if k not in ("auth", "_file")}
        if p.get("auth"):
            copy["auth_required"] = True
            copy["auth_header"] = p["auth"].get("header")
        sanitized.append(copy)
    return {"peers": sanitized}


@app.get("/api/models")
async def list_models():
    models = []
    for model_id, info in MODEL_SETUP.items():
        models.append({
            "id": model_id,
            "name": info["display"],
            "desc": info["desc"],
            "override": MODEL_OVERRIDE_MAP.get(model_id),
            "weights_repo": info.get("weights_repo"),
            "weights_dir": info.get("weights_dir"),
            "extra_weights_dirs": info.get("extra_weights_dirs", []),
            "weights_size": info.get("weights_size"),
            "estimated_vram_gb": MODEL_VRAM_ESTIMATE_GB.get(model_id),
            "vram_estimate_policy": "conservative_planning_estimate",
        })
    return {"models": models}


@app.get("/api/models/status")
async def models_status():
    workers = registry.all_workers()
    live_worker_ids = {w.worker_id for w in workers}
    status = {}
    for w in workers:
        if w.model not in status:
            status[w.model] = {"workers": [], "loaded": False}
        status[w.model]["workers"].append({
            "worker_id": w.worker_id,
            "device": w.device,
            "status": w.status,
        })
        if w.status in ("ready", "busy"):
            status[w.model]["loaded"] = True
    # A successful background load record is only current while its worker is
    # still registered.  Prune records left behind by unloads, direct worker
    # deletion, or a worker that exited unexpectedly so clients never mistake
    # historical state for a resident model.
    stale_load_keys = []
    for key, state in _model_load_states.items():
        if (state.get("status") == "loaded"
                and state.get("worker_id") not in live_worker_ids):
            stale_load_keys.append(key)
            continue
        model = state["model"]
        if model not in status:
            status[model] = {"workers": [], "loaded": False}
        status[model].setdefault("loads", []).append(dict(state))
    for key in stale_load_keys:
        _model_load_states.pop(key, None)
    return {"models": status}


# ============================================================
# GPU / Device discovery
# ============================================================
@app.get("/api/devices")
async def list_devices():
    devices = await worker_manager.detect_devices_async()
    return {"devices": devices}


# ============================================================
# Worker management endpoints
# ============================================================
@app.get("/api/workers")
async def list_workers():
    return {"workers": registry.to_dict_list()}


def _ensure_known_model(model: str) -> None:
    if model not in MODEL_SETUP:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model}")


@app.post("/api/workers/spawn")
async def spawn_worker(req: SpawnRequest):
    _ensure_known_model(req.model)
    try:
        worker = await worker_manager.spawn_worker(req.model, req.device, req.precision)
        return {
            "status": "spawned",
            "worker_id": worker.worker_id,
            "model": worker.model,
            "port": worker.port,
            "device": worker.device,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/workers/{worker_id}")
async def delete_worker(worker_id: str):
    killed = await worker_manager.kill_worker(worker_id)
    if not killed:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    return {"status": "killed", "worker_id": worker_id}


@app.post("/api/workers/{worker_id}/unload")
async def unload_worker(worker_id: str):
    """Tell a worker to unload its model (frees VRAM) but stay alive.

    The worker process stays registered as idle. Restart it to load its model
    again; idle workers are not advertised as ready for inference."""
    worker = registry.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    previous_status = registry.begin_unload(worker_id)
    if previous_status is None:
        raise HTTPException(status_code=409, detail="Worker is busy or changing state")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"http://127.0.0.1:{worker.port}/unload")
            resp.raise_for_status()
            result = resp.json() if resp.content else {}
    except httpx.HTTPError as e:
        worker.status = previous_status
        raise HTTPException(status_code=502, detail=f"Worker unload call failed: {e}")
    worker.status = "idle"
    return {"status": "unloaded", "worker_id": worker_id, "worker_response": result}


@app.post("/api/workers/{worker_id}/restart")
async def restart_worker(worker_id: str):
    """Kill and respawn a single worker with the same model + device.

    Useful after a code update or to recover a stuck worker without
    affecting the rest of the pool."""
    worker = registry.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    model = worker.model
    device = worker.device
    if not await worker_manager.kill_worker(worker_id):
        raise HTTPException(status_code=500, detail=f"Failed to kill worker {worker_id}")
    try:
        new_worker = await worker_manager.spawn_worker(model, device)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Respawn failed: {e}")
    return {
        "status": "restarted",
        "old_worker_id": worker_id,
        "worker_id": new_worker.worker_id,
        "model": new_worker.model,
        "port": new_worker.port,
        "device": new_worker.device,
    }


@app.get("/api/workers/{worker_id}/logs")
async def get_worker_logs(worker_id: str, lines: int = 200):
    """Tail the last N lines from a worker's log file."""
    worker = registry.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    log_path = WORKER_LOG_DIR / f"worker_{worker.model}_{worker.port}.log"
    if not log_path.exists():
        return {"worker_id": worker_id, "path": str(log_path),
                "lines": [], "exists": False}

    try:
        n = max(1, min(int(lines), 5000))
    except (ValueError, TypeError):
        n = 200

    def _tail():
        try:
            with open(log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                blocksize = 8192
                data = b""
                while size > 0 and data.count(b"\n") <= n:
                    read = min(blocksize, size)
                    size -= read
                    fh.seek(size)
                    data = fh.read(read) + data
                lines_out = data.decode("utf-8", errors="replace").splitlines()
                return lines_out[-n:]
        except OSError as e:
            return [f"(read error: {e})"]

    out_lines = await asyncio.to_thread(_tail)
    return {
        "worker_id": worker_id,
        "model": worker.model,
        "port": worker.port,
        "path": str(log_path),
        "lines": out_lines,
        "exists": True,
    }


@app.post("/api/models/{model}/scale")
async def scale_model(model: str, req: ScaleRequest):
    _ensure_known_model(model)
    try:
        workers = await worker_manager.scale_model(model, req.count, req.device)
        return {
            "status": "scaled",
            "model": model,
            "device": str(req.device or WORKER_DEFAULT_DEVICE),
            "count": len(workers),
            "workers": [w.worker_id for w in workers],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Model load/unload (now delegates to workers)
# ============================================================
def _model_load_key(model: str, device: str | None) -> str:
    return f"{model}:{WorkerManager.normalize_device(device or WORKER_DEFAULT_DEVICE)}"


def _loading_response(model: str, device: str | None, status_code: int = 202) -> JSONResponse:
    workers = registry.workers_for_model(model)
    return JSONResponse(
        {
            "status": "loading",
            "model": model,
            "device": str(device or WORKER_DEFAULT_DEVICE),
            "workers": [
                {"worker_id": w.worker_id, "status": w.status, "port": w.port}
                for w in workers
            ],
            "poll_url": f"/api/ready?model={model}&timeout=0",
            "workers_url": "/api/workers",
        },
        status_code=status_code,
    )


def _finish_model_load_task(
    task: asyncio.Task,
    key: str,
    model: str,
    device: str,
) -> None:
    """Consume a background load result and retain a pollable final state."""
    if _model_load_tasks.get(key) is task:
        _model_load_tasks.pop(key, None)
    state = {
        "model": model,
        "device": device,
        "updated_at": time.time(),
    }
    try:
        worker = task.result()
        state.update({
            "status": "loaded",
            "worker_id": worker.worker_id,
        })
    except asyncio.CancelledError:
        state["status"] = "cancelled"
    except Exception as exc:
        state.update({
            "status": "error",
            "error": str(exc)[-2000:],
        })
        logger.error("Background model load failed for %s on %s: %s",
                     model, device, exc)
    _model_load_states[key] = state


@app.post("/api/models/{model}/load")
async def load_model(
    model: str,
    device: Optional[str] = None,
    wait: bool = True,
    wait_timeout: float = 55.0,
):
    """Load a model by spawning a worker for it.

    Cold first-time loads can download weights and exceed common browser/proxy
    request timeouts. For those cases the route keeps the load running in the
    background and returns 202 with poll URLs instead of letting callers hang.
    """
    _ensure_known_model(model)
    if device:
        try:
            device = WorkerManager.normalize_device(device)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    existing = [w for w in _matching_workers(model, {"device": device})
                if w.status in ("ready", "busy")]
    if existing:
        return {"status": "already_loaded", "model": model,
                "workers": [w.worker_id for w in existing]}
    key = _model_load_key(model, device)
    task = _model_load_tasks.get(key)
    if task is None and _matching_workers(model, {"device": device}):
        return _loading_response(model, device)
    if task and task.done():
        _model_load_tasks.pop(key, None)
        try:
            worker = task.result()
            return {"status": "loaded", "model": model, "worker_id": worker.worker_id}
        except Exception as e:
            available = [w for w in _matching_workers(model, {"device": device})
                         if w.status in ("ready", "busy")]
            if available:
                return {"status": "loaded", "model": model,
                        "workers": [w.worker_id for w in available]}
            raise HTTPException(status_code=500, detail=str(e))
    if task is None:
        resolved_device = str(device or WORKER_DEFAULT_DEVICE)
        task = asyncio.create_task(worker_manager.spawn_worker(model, device))
        _model_load_tasks[key] = task
        _model_load_states[key] = {
            "model": model,
            "device": resolved_device,
            "status": "loading",
            "updated_at": time.time(),
        }
        # Consume success/failure even for wait=false callers and retain it in
        # /api/models/status. This avoids silent task exceptions and gives API
        # clients a reliable polling surface after the worker has exited.
        task.add_done_callback(
            lambda t, k=key, m=model, d=resolved_device:
            _finish_model_load_task(t, k, m, d)
        )
    if not wait:
        return _loading_response(model, device)
    try:
        timeout = max(1.0, min(float(wait_timeout or 55.0), 900.0))
        worker = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        _model_load_tasks.pop(key, None)
        return {"status": "loaded", "model": model, "worker_id": worker.worker_id}
    except asyncio.TimeoutError:
        return _loading_response(model, device)
    except Exception as e:
        _model_load_tasks.pop(key, None)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/models/{model}/unload")
async def unload_model(model: str):
    """Unload a model by killing all its workers."""
    _ensure_known_model(model)

    # A load request can be waiting for worker health/model initialization
    # while an unload arrives. Cancel and await it first; WorkerManager's
    # cancellation path reaps the process and releases its port.
    pending = []
    load_keys = []
    for key, task in list(_model_load_tasks.items()):
        if key.startswith(f"{model}:"):
            load_keys.append(key)
            if not task.done():
                task.cancel()
                pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for key in load_keys:
        _model_load_tasks.pop(key, None)
        _model_load_states.pop(key, None)

    def clear_model_load_states() -> None:
        for key, state in list(_model_load_states.items()):
            if state.get("model") == model:
                _model_load_states.pop(key, None)

    workers = registry.workers_for_model(model)
    if not workers:
        clear_model_load_states()
        reclaim = await asyncio.to_thread(
            worker_manager.reclaim_model_file_cache, model
        )
        return {
            "status": "not_loaded",
            "model": model,
            "loads_cancelled": len(pending),
            "memory_reclaim": reclaim,
        }

    killed = 0
    for w in workers:
        if await worker_manager.kill_worker(w.worker_id):
            killed += 1
    clear_model_load_states()
    return {
        "status": "unloaded",
        "model": model,
        "workers_killed": killed,
        "loads_cancelled": len(pending),
        "memory_reclaim": worker_manager.last_reclaim(model),
    }


# ============================================================
# Whisper management
# ============================================================
@app.get("/api/whisper")
async def whisper_info():
    whisper_workers = registry.workers_for_model("whisper")
    loaded_sizes = []

    # Query the whisper worker for its loaded sizes
    if whisper_workers:
        for w in whisper_workers:
            if w.status in ("ready", "busy"):
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"http://127.0.0.1:{w.port}/health")
                        if resp.status_code == 200:
                            loaded_sizes.extend(resp.json().get("loaded_sizes", []))
                except Exception:
                    pass

    return {
        "available_models": WHISPER_AVAILABLE_MODELS,
        "default": WHISPER_MODEL_SIZE,
        "loaded": sorted(set(loaded_sizes)),
        "enabled": WHISPER_ENABLED,
        "whisper_workers": [w.worker_id for w in whisper_workers],
    }


@app.post("/api/whisper/{size}/load")
async def load_whisper(size: str):
    if size not in WHISPER_AVAILABLE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid size. Choose from: {list(WHISPER_AVAILABLE_MODELS.keys())}"
        )

    worker = await _ensure_whisper_worker_ready()
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(f"http://127.0.0.1:{worker.port}/whisper/{size}/load")
            response.raise_for_status()
    finally:
        registry.mark_ready(worker.worker_id)
    return {"status": "loaded", "size": size, "info": WHISPER_AVAILABLE_MODELS[size]}


@app.post("/api/whisper/{size}/unload")
async def unload_whisper(size: str):
    if size not in WHISPER_AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail="Invalid Whisper size")
    for existing in registry.workers_for_model("whisper"):
        if existing.status not in ("ready", "busy"):
            continue
        worker = await asyncio.to_thread(_reserve_worker, "whisper", "unload", {"worker_id": existing.worker_id})
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(f"http://127.0.0.1:{worker.port}/whisper/{size}/unload")
                response.raise_for_status()
        finally:
            registry.mark_ready(worker.worker_id)
    return {"status": "unloaded", "size": size}


# ============================================================
# Job management endpoints
# ============================================================
@app.get("/api/jobs")
async def list_jobs(
    model: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List jobs with optional filtering.

    Query params:
        model:  only jobs from this model (e.g. ?model=kokoro)
        status: 'running' | 'completed' | 'failed' | 'cancelled'
        since:  ISO-8601 timestamp (e.g. 2026-05-01T00:00:00Z) — inclusive
        until:  ISO-8601 timestamp — inclusive
        limit:  max results (1-500, default 50)
        offset: skip the first N results (for pagination)
    """
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    # Pull a generous superset so we can filter/paginate in the gateway.
    # JobManager.list_jobs caches the index so this is cheap.
    all_jobs = await asyncio.to_thread(job_manager.list_jobs, limit=None)
    seen = {j["job_id"] for j in all_jobs}
    for manifest in DELIVERED_JOBS_DIR.glob("*.json"):
        try:
            archived = json.loads(manifest.read_text(encoding="utf-8"))
            if archived["job_id"] not in seen:
                all_jobs.append(job_manager._job_summary(archived))
        except (OSError, ValueError, KeyError):
            continue
    all_jobs.sort(key=lambda j: j.get("timestamp", ""), reverse=True)

    def _keep(j: dict) -> bool:
        if model and j.get("model") != model:
            return False
        if status and j.get("status") != status:
            return False
        ts = j.get("timestamp") or ""
        if since and ts < since:
            return False
        if until and ts > until:
            return False
        return True

    filtered = [j for j in all_jobs if _keep(j)]
    total = len(filtered)
    page = filtered[offset:offset + limit]
    return {
        "jobs": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {"model": model, "status": status, "since": since, "until": until},
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class ChunkEditRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000)


@app.put("/api/jobs/{job_id}/chunks/{chunk_idx}")
async def edit_chunk_text(job_id: str, chunk_idx: int, req: ChunkEditRequest):
    """Edit the text of a specific chunk in a job (for fixing failed chunks before recovery)."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    chunks = job.get("chunks", [])
    if chunk_idx < 0 or chunk_idx >= len(chunks):
        raise HTTPException(status_code=400, detail=f"Chunk index {chunk_idx} out of range (0-{len(chunks)-1})")

    # Update the chunk text (acquire per-job lock to prevent concurrent read-modify-write)
    with job_manager._job_lock(job_id):
        job_file = job_manager._find_job_file(job_id)
        if not job_file:
            raise HTTPException(status_code=404, detail="Job file not found")
        _reject_active_job(job_id)
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Chunk text must contain speech")
        job_data = job_manager._read_job(job_file)
        job_manager.invalidate_output(job_data)
        job_data["chunks"][chunk_idx]["text"] = req.text.strip()
        job_data["chunks"][chunk_idx]["char_length"] = len(req.text.strip())
        # Reset this chunk and every later chunk because recovery resumes from
        # a contiguous prefix. Leaving later chunks marked complete can skip
        # required regeneration when chunks previously completed out of order.
        for chunk in job_data["chunks"][chunk_idx:]:
            chunk["duration_sec"] = None
            chunk["processing_error"] = None
            chunk["verification_passed"] = None
            chunk["whisper_transcript"] = None
            chunk["whisper_similarity"] = None
        job_data["input_text"] = " ".join(c["text"] for c in job_data["chunks"])
        job_data.setdefault("parameters", {})["text"] = job_data["input_text"]
        job_data["chunks_completed"] = min(chunk_idx, job_data.get("chunks_completed", 0))
        stem = job_data.get("stem") or job_file.parent.name
        output_format = job_data.get("output_format", "wav")
        job_data["missing_files"] = [
            f"chunk_{i:03d}.wav" for i in range(chunk_idx, len(job_data["chunks"]))
        ] + [f"{stem}_final.{output_format}"]
        job_manager._write_job(job_file, job_data)
        chunks_completed = job_data["chunks_completed"]

    return {
        "status": "updated",
        "job_id": job_id,
        "chunk_idx": chunk_idx,
        "new_text": req.text.strip(),
        "chunks_completed": chunks_completed,
    }


@app.post("/api/jobs/{job_id}/recover")
async def recover_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory is invalid")
    model = job["model"]
    if model not in PROFILES:
        raise HTTPException(status_code=400, detail=f"Recovery not supported for model: {model}")
    with _running_jobs_lock:
        if any(job_id in active for active in _running_jobs.values()):
            raise HTTPException(status_code=409, detail="Job is already running or recovering")
        recovered = job_manager.recover_job(job_dir)
        if recovered is None:
            raise HTTPException(status_code=400, detail="Job not recoverable")
        _running_jobs.setdefault(model, set()).add(job_id)
    if model in PROFILES:
        try:
            future = executor.submit(_run_pipeline_recovery, model, recovered, job_dir)
        except Exception:
            # submit failed (e.g. executor shutting down) — drop the registration
            # so a later retry isn't permanently blocked by the in-flight guard.
            with _running_jobs_lock:
                active = _running_jobs.get(model, set())
                active.discard(job_id)
                if not active:
                    _running_jobs.pop(model, None)
            raise
        def _on_recovery_done(f, _job_id=job_id):
            exc = f.exception()
            if exc:
                logger.error("Recovery pipeline for %s failed: %s", _job_id, exc)
                try:
                    job_manager.fail_job(_job_id, f"Recovery failed: {exc}")
                except Exception:
                    pass
        future.add_done_callback(_on_recovery_done)
        return {"status": "recovering", "job_id": recovered["job_id"],
                "resuming_from_chunk": recovered["chunks_completed"]}
    raise HTTPException(status_code=400, detail=f"Recovery not supported for model: {model}")


@app.get("/api/jobs/{job_id}/output")
async def get_job_output(job_id: str):
    """Serve the final audio file for a completed job."""
    job = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    final_file = _safe_job_file_name(job.get("final_file"))
    if not final_file:
        raise HTTPException(status_code=404, detail="Job has no output file (not completed?)")
    job_dir = job_manager.get_job_dir(job_id)
    file_path = (job_dir / final_file) if job_dir is not None else _delivered_artifact_path(job, "audio")
    if file_path is None:
        raise HTTPException(status_code=404, detail="Delivered output file not found")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Output file not found: {final_file}")
    mime_map = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".m4a": "audio/mp4",
    }
    mime = mime_map.get(file_path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(file_path), media_type=mime, filename=file_path.name)


@app.get("/api/jobs/{job_id}/output/info")
async def get_job_output_info(job_id: str):
    """Return audio metadata (sample_rate, duration, channels, frames, size)
    for a completed job's final audio without streaming the bytes back."""
    job = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    final_file = _safe_job_file_name(job.get("final_file"))
    if not final_file:
        raise HTTPException(status_code=404, detail="Job has no output file (not completed?)")
    job_dir = job_manager.get_job_dir(job_id)
    file_path = (job_dir / final_file) if job_dir is not None else _delivered_artifact_path(job, "audio")
    if file_path is None:
        raise HTTPException(status_code=404, detail="Delivered output file not found")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Output file not found: {final_file}")

    info: dict = {
        "job_id": job_id,
        "filename": file_path.name,
        "path": str(file_path),
        "format": file_path.suffix.lower().lstrip("."),
        "size_bytes": file_path.stat().st_size,
    }
    try:
        with sf.SoundFile(str(file_path)) as f:
            info["sample_rate"] = f.samplerate
            info["channels"] = f.channels
            info["frames"] = f.frames
            info["duration_sec"] = round(f.frames / f.samplerate, 3) if f.samplerate else None
    except Exception as e:
        # Non-WAV formats (mp3/m4a) may not open with soundfile; report what
        # we got plus the read error so callers know why fields are missing.
        info["sample_rate"] = None
        info["duration_sec"] = None
        info["read_error"] = str(e)
    mime_map = {
        "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
        "flac": "audio/flac", "m4a": "audio/mp4",
    }
    info["artifact"] = {
        "path": info["path"],
        "ref": f"tts://jobs/{job_id}/output",
        "media_type": "audio",
        "mime": mime_map.get(info["format"], "application/octet-stream"),
        "duration": info.get("duration_sec"),
        "dimensions": None,
        "metadata": {
            "job_id": job_id,
            "filename": info["filename"],
            "format": info["format"],
            "size_bytes": info["size_bytes"],
            "sample_rate": info.get("sample_rate"),
            "channels": info.get("channels"),
            "frames": info.get("frames"),
        },
    }
    return info


@app.get("/api/jobs/{job_id}/verification")
async def get_job_verification(job_id: str):
    """Return a compact per-chunk Whisper verification report.

    Only meaningful for jobs run with verify_whisper=true — otherwise all
    fields will be None. Cheaper to display in a UI than the full job manifest.
    """
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    chunks = job.get("chunks", [])
    report = []
    passed = failed = unverified = 0
    similarities: list[float] = []
    for c in chunks:
        v = c.get("verification_passed")
        sim = c.get("whisper_similarity")
        report.append({
            "index": c.get("index"),
            "text": c.get("text"),
            "transcript": c.get("whisper_transcript"),
            "similarity": sim,
            "passed": v,
            "duration_sec": c.get("duration_sec"),
            "error": c.get("processing_error"),
        })
        if v is True:
            passed += 1
        elif v is False:
            failed += 1
        else:
            unverified += 1
        if isinstance(sim, (int, float)):
            similarities.append(float(sim))
    avg_sim = round(sum(similarities) / len(similarities), 4) if similarities else None
    return {
        "job_id": job_id,
        "model": job.get("model"),
        "status": job.get("status"),
        "total_chunks": len(chunks),
        "passed": passed,
        "failed": failed,
        "unverified": unverified,
        "average_similarity": avg_sim,
        "chunks": report,
    }


@app.get("/api/jobs/{job_id}/chunks/{chunk_idx}/audio")
async def get_job_chunk_audio(job_id: str, chunk_idx: int):
    """Serve a single chunk WAV from a job (useful before assembly completes)."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    chunks = job.get("chunks", [])
    if not chunks:
        raise HTTPException(status_code=404, detail="Job has no chunks")
    if chunk_idx < 0 or chunk_idx >= len(chunks):
        raise HTTPException(
            status_code=400,
            detail=f"Chunk index {chunk_idx} out of range (0-{len(chunks) - 1})",
        )
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory is invalid")
    chunk_name = _safe_job_file_name(chunks[chunk_idx].get("file")) or f"chunk_{chunk_idx:03d}.wav"
    file_path = job_dir / chunk_name
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Chunk audio not found (chunk may not be generated yet): {chunk_name}",
        )
    return FileResponse(str(file_path), media_type="audio/wav", filename=chunk_name)


# ---------------------------------------------------------------------------
# Audio editor endpoints (peaks, render, edits library)
# ---------------------------------------------------------------------------

_AUDIO_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4",
}


def _editor_safe_name(value: str) -> str:
    """Sanitize a user-supplied edit name into a filesystem-safe slug."""
    if not value:
        return "edit"
    # keep alnum, dash, underscore, dot, space → replace others with _
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", value).strip().strip(".")
    cleaned = cleaned[:80] if cleaned else "edit"
    return cleaned or "edit"


_AUDIO_OUTPUT_EXTS = {".wav", ".mp3", ".flac", ".ogg"}
_AUDIO_OUTPUT_FORMATS = {"wav", "mp3", "flac", "ogg"}


def _audio_safe_roots() -> tuple[Path, ...]:
    """Roots under which free-path audio sources / outputs are allowed."""
    return (VOICE_DIR.resolve(), OUTPUT_DIR.resolve(), PROJECTS_OUTPUT.resolve())


def _validate_free_audio_source(value: str) -> Path:
    """Validate a free audio source path.

    The path must be absolute, exist as a regular file, have an audio
    extension, and live under one of the safe audio roots (VOICE_DIR,
    OUTPUT_DIR, or PROJECTS_OUTPUT). Symlinks are followed before the
    boundary check so they can't be used to escape.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HTTPException(status_code=400, detail="Invalid source.path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="source.path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="source.path file not found")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="source.path is not a regular file")
    if resolved.suffix.lower() not in _AUDIO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source format: {resolved.suffix}",
        )
    if not any(_path_is_relative_to(resolved, base) for base in _audio_safe_roots()):
        raise HTTPException(
            status_code=400,
            detail=(f"source.path must be under {VOICE_DIR}, {OUTPUT_DIR}, "
                    f"or {PROJECTS_OUTPUT}"),
        )
    return resolved


def _validate_audio_output_path(value: str) -> Path:
    """Validate an audio output path. The file need not exist; the parent
    directory's resolved path must be inside an allowed root."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HTTPException(status_code=400, detail="Invalid output_path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="output_path must be absolute")
    # Non-strict resolve canonicalizes `..` even when the file doesn't exist yet.
    resolved = candidate.resolve()
    if resolved.suffix.lower() not in _AUDIO_OUTPUT_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {resolved.suffix}",
        )
    parent = resolved.parent.resolve()
    if not any(parent == base or _path_is_relative_to(parent, base)
               for base in _audio_safe_roots()):
        raise HTTPException(
            status_code=400,
            detail=(f"output_path parent must be under {VOICE_DIR}, {OUTPUT_DIR}, "
                    f"or {PROJECTS_OUTPUT}"),
        )
    return resolved


def _editor_job_dir(job_id: str) -> Path | None:
    directory = job_manager.get_job_dir(job_id)
    if directory is not None:
        return directory
    if _read_delivered_job(job_id):
        return DELIVERED_JOBS_DIR / job_id
    return None


def _resolve_audio_source(source: dict) -> Path:
    """Resolve a JSON source descriptor to a validated audio file path.

    source = {
        "kind": "final" | "chunk" | "edit" | "path",
        "job_id": str,                  # required for final/chunk/edit
        "index": int                    # required for chunk
        "name": str                     # required for edit
        "path": str                     # required for path (absolute, under safe roots)
    }

    For job-bound kinds the returned path is verified to be a regular file
    inside the job's expected directory (job_dir for final/chunk;
    job_dir/edits/ for edit). For "path" the file must live under
    VOICE_DIR, OUTPUT_DIR, or PROJECTS_OUTPUT.
    """
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="source must be an object")
    kind = source.get("kind")

    if kind == "path":
        return _validate_free_audio_source(source.get("path"))

    job_id = source.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=400, detail="source.job_id required")
    job = _get_job_or_delivered(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = _editor_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory invalid")

    if kind == "final" and job.get("purged_work_dir"):
        delivered = _delivered_artifact_path(job, "audio")
        if delivered is None:
            raise HTTPException(status_code=404, detail="Delivered audio is unavailable")
        return delivered

    if kind == "final":
        # Always read from the manifest — never trust a user-supplied final name.
        final_file = _safe_job_file_name(job.get("final_file"))
        if not final_file:
            raise HTTPException(status_code=404, detail="Final file not available")
        path = job_dir / final_file
        expected_parent = job_dir
    elif kind == "chunk":
        try:
            idx = int(source.get("index"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="source.index required for chunk")
        chunks = job.get("chunks", [])
        if idx < 0 or idx >= len(chunks):
            raise HTTPException(status_code=400,
                                detail=f"Chunk index {idx} out of range (0-{len(chunks)-1})")
        chunk_name = _safe_job_file_name(chunks[idx].get("file")) or f"chunk_{idx:03d}.wav"
        path = job_dir / chunk_name
        expected_parent = job_dir
    elif kind == "edit":
        edit_name = _safe_job_file_name(source.get("name"))
        if not edit_name:
            raise HTTPException(status_code=400, detail="source.name required for edit")
        edits_dir = job_dir / "edits"
        path = edits_dir / edit_name
        expected_parent = edits_dir
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source.kind: {kind}")

    # Resolve to absolute and ensure it points to a regular file inside the
    # expected parent. Catches `..` traversal and symlink escapes that the
    # filename-only check can't see on its own.
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail=f"File not found: {path.name}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Source is not a regular file")
    if resolved.parent != expected_parent.resolve():
        raise HTTPException(status_code=400, detail="Source escapes expected directory")
    return resolved


class AudioPeaksRequest(BaseModel):
    source: dict
    buckets: int = 1024


@app.get("/api/audio/effects")
async def audio_effects():
    """Return the effect manifest with default parameters for the editor UI."""
    return {"effects": audio_editor.EFFECT_DEFAULTS}


@app.post("/api/audio/peaks")
async def audio_peaks(req: AudioPeaksRequest):
    """Return waveform min/max bucket data for a source.

    Body: { source: {kind, job_id, ...}, buckets: int }
    """
    path = _resolve_audio_source(req.source)
    try:
        result = await asyncio.to_thread(audio_editor.extract_peaks, path, int(req.buckets))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.warning("Peaks extraction failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Peaks failed: {e}")
    return result


class AudioRenderRequest(BaseModel):
    source: dict
    edits: list[dict] = Field(default_factory=list)
    output_name: Optional[str] = None
    output_path: Optional[str] = None
    output_format: str = "wav"
    overwrite: bool = False


@app.post("/api/audio/render")
async def audio_render(req: AudioRenderRequest):
    """Apply an edit chain to a source and write the result.

    Output destination, in priority order:
      1. ``output_path`` (absolute, under VOICE_DIR / OUTPUT_DIR / PROJECTS_OUTPUT
         — extension picks the format).
      2. ``output_name`` under ``<job_dir>/edits/<output_name>.<output_format>``
         (job-bound sources only).
      3. Auto-versioned ``<job_dir>/edits/<source_stem>_vNN.<output_format>``
         (job-bound sources only).

    A "path" source requires ``output_path`` (no job to default into).
    """
    if req.output_path and req.output_name:
        raise HTTPException(
            status_code=400,
            detail="Specify either output_path or output_name, not both",
        )

    src_path = _resolve_audio_source(req.source)
    job_id = req.source.get("job_id") if req.source.get("kind") != "path" else None
    reserved_placeholder = None  # empty file we created to reserve an auto-version name

    if req.output_path:
        out_path = _validate_audio_output_path(req.output_path)
        for parent in out_path.parents:
            manifest = parent / "job.json"
            if manifest.is_file() and out_path.parent.name != "edits":
                raise HTTPException(status_code=409, detail="Save job changes as an edit; tracked artifacts cannot be overwritten")
        # _validate_audio_output_path already verified the extension is in
        # _AUDIO_OUTPUT_EXTS, so the format derivation here is sound.
        fmt = out_path.suffix.lower().lstrip(".")
        if out_path.exists():
            if not out_path.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"output_path exists and is not a regular file: {out_path}",
                )
            if not req.overwrite:
                raise HTTPException(status_code=409, detail=f"Output exists: {out_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        if not job_id:
            raise HTTPException(
                status_code=400,
                detail=("output_path is required when source.kind='path' "
                        "(no job to default into)"),
            )
        job_dir = _editor_job_dir(job_id)
        if job_dir is None:
            raise HTTPException(status_code=400, detail="Job directory invalid")

        fmt = (req.output_format or "wav").lower().lstrip(".")
        if fmt not in _AUDIO_OUTPUT_FORMATS:
            raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

        edits_dir = job_dir / "edits"
        edits_dir.mkdir(parents=True, exist_ok=True)

        base = _editor_safe_name(req.output_name or src_path.stem)
        if not req.output_name:
            # Auto-versioned name; strip a prior _vNN to keep chains tidy.
            base = re.sub(r"_v\d{2,3}$", "", base)
            # Atomically reserve the chosen filename with O_CREAT|O_EXCL so two
            # concurrent renders cannot both pick the same _vNN and clobber each
            # other (a bare exists() probe is a TOCTOU). The reserved placeholder
            # is later overwritten by render_to_file's os.replace.
            n = 1
            out_path = None
            while n <= 999:
                candidate = edits_dir / f"{base}_v{n:02d}.{fmt}"
                try:
                    fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    os.close(fd)
                    out_path = candidate
                    reserved_placeholder = candidate
                    break
                except FileExistsError:
                    n += 1
            if out_path is None:
                raise HTTPException(status_code=500, detail="Too many edit versions")
        else:
            out_path = edits_dir / f"{base}.{fmt}"
            if out_path.exists() and not req.overwrite:
                raise HTTPException(status_code=409, detail=f"Edit exists: {out_path.name}")

        # Belt-and-suspenders against symlink shenanigans.
        if out_path.parent.resolve() != edits_dir.resolve():
            raise HTTPException(status_code=400, detail="Output escapes expected directory")

    try:
        meta = await asyncio.to_thread(
            audio_editor.render_to_file, src_path, out_path, req.edits, fmt
        )
    except ValueError as e:
        if reserved_placeholder is not None:
            reserved_placeholder.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        if reserved_placeholder is not None:
            reserved_placeholder.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Remove the 0-byte reserved placeholder so a failed render doesn't leave
        # an empty _vNN file behind (render_to_file only os.replace()s on success).
        if reserved_placeholder is not None:
            reserved_placeholder.unlink(missing_ok=True)
        logger.exception("Audio render failed")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

    return {
        "status": "ok",
        "job_id": job_id,
        "edit_name": out_path.name,
        "edit_path": str(out_path),
        "output_path": str(out_path),
        "format": fmt,
        **meta,
    }


@app.get("/api/audio/edits/{job_id}")
async def list_audio_edits(job_id: str):
    """List existing edit files in <job_dir>/edits/."""
    job = _get_job_or_delivered(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = _editor_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory invalid")
    edits_dir = job_dir / "edits"
    if not edits_dir.exists():
        return {"edits": []}
    items = []
    for f in edits_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in _AUDIO_MIME:
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        items.append({
            "name": f.name,
            "format": f.suffix.lower().lstrip("."),
            "size_bytes": stat.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        })
    return {"edits": items}


def _resolve_edit_path(job_id: str, filename: str) -> Path:
    """Resolve an edit filename to a validated file inside <job_dir>/edits/."""
    if _get_job_or_delivered(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = _editor_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory invalid")
    safe = _safe_job_file_name(filename)
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    edits_dir = job_dir / "edits"
    path = edits_dir / safe
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Edit not found")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Edit is not a regular file")
    if resolved.parent != edits_dir.resolve():
        raise HTTPException(status_code=400, detail="Edit escapes expected directory")
    return resolved


@app.get("/api/audio/edits/{job_id}/{filename}")
async def get_audio_edit(job_id: str, filename: str):
    """Stream an edit file."""
    path = _resolve_edit_path(job_id, filename)
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=mime, filename=path.name)


@app.delete("/api/audio/edits/{job_id}/{filename}")
async def delete_audio_edit(job_id: str, filename: str):
    """Delete an edit file."""
    path = _resolve_edit_path(job_id, filename)
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"status": "deleted", "name": path.name}


def _scan_projects() -> list[dict]:
    """Synchronous scan of project subdirectories; called via to_thread."""
    projects: list[dict] = []
    if not PROJECTS_OUTPUT.exists():
        return projects
    for entry in sorted(PROJECTS_OUTPUT.iterdir()):
        if not entry.is_dir():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        try:
            job_count = sum(1 for _ in entry.rglob("job.json"))
        except OSError:
            job_count = 0
        projects.append({
            "name": entry.name,
            "path": str(entry),
            "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "job_count": job_count,
        })
    return projects


@app.get("/api/projects")
async def list_projects():
    """List project subdirectories under PROJECTS_OUTPUT.

    Each project is one directory; this returns its name, path, mtime, and
    job count (number of job.json files found inside, recursively)."""
    projects = await asyncio.to_thread(_scan_projects)
    return {"projects": projects, "root": str(PROJECTS_OUTPUT)}


class DeleteJobsRequest(BaseModel):
    job_ids: list[str]


@app.post("/api/jobs/delete")
async def delete_jobs(req: DeleteJobsRequest):
    """Delete one or more jobs and their files."""
    results = {}
    for jid in req.job_ids:
        try:
            _reject_active_job(jid)
            results[jid] = job_manager.delete_job(jid)
            archive = _delivered_manifest_path(jid)
            if archive.exists():
                edits = DELIVERED_JOBS_DIR / jid
                if edits.exists():
                    shutil.rmtree(edits)
                archive.unlink()
                results[jid] = True
        except (HTTPException, OSError):
            results[jid] = False
    deleted = sum(1 for v in results.values() if v)
    return {"deleted": deleted, "total": len(req.job_ids), "results": results}


# ============================================================
# Cancel endpoints
# ============================================================
@app.post("/api/tts/{model}/cancel")
async def cancel_model_job(model: str, body: CancelRequest = CancelRequest()):
    _ensure_known_model(model)
    if body.job_id:
        job = job_manager.get_job(body.job_id)
        if job is None or job.get("model") != model:
            raise HTTPException(status_code=404, detail="Job not found for this model")
        # Cancel a specific job
        if job_manager.request_cancel(body.job_id):
            return {"status": "cancel_requested", "job_id": body.job_id}
        return {"status": "job_not_found", "job_id": body.job_id}
    # Cancel all running jobs for this model
    with _running_jobs_lock:
        active = list(_running_jobs.get(model, set()))
    if active:
        cancelled = [jid for jid in active if job_manager.request_cancel(jid)]
        if cancelled:
            return {"status": "cancel_requested", "job_ids": cancelled}
    return {"status": "no_running_job", "model": model}


# ============================================================
# Pipeline TTS endpoint (single parameterized route)
# ============================================================
_TTS_MODELS = set(MODEL_SETUP.keys()) - {"whisper"}
_MODEL_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "f5": ("reference_audio",),
    "vibevoice": ("reference_audio",),
}


def _has_reference_audio(req: PipelineTTSRequest) -> bool:
    return bool(
        (req.voice or "").strip()
        or (req.reference_audio or "").strip()
        or any(str(value or "").strip() for value in (req.reference_audios or []))
    )


def _validate_model_requirements(model: str, req: PipelineTTSRequest) -> None:
    if model != "vibevoice" and req.reference_audios:
        raise HTTPException(status_code=400, detail="reference_audios is supported only by VibeVoice")
    required = _MODEL_REQUIRED_FIELDS.get(model, ())
    missing: list[str] = []
    if "reference_audio" in required and not _has_reference_audio(req):
        missing.append("reference_audio")
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{MODEL_SETUP[model].get('display', model)} requires {', '.join(missing)}",
                "model": model,
                "missing": missing,
            },
        )


@app.post("/api/tts/{model}")
async def tts_model(model: str, req: PipelineTTSRequest):
    if model not in _TTS_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown TTS model: {model}")
    _validate_model_requirements(model, req)
    return await _handle_pipeline_request(model, req)


@app.post("/api/tts/{model}/submit")
async def tts_model_submit(model: str, req: PipelineTTSRequest):
    """Async TTS submission. Returns job_id immediately; pipeline runs in
    the background. Poll GET /api/jobs/{job_id} for status, then download
    via GET /api/jobs/{job_id}/output when status == 'completed'."""
    if model not in _TTS_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown TTS model: {model}")
    _validate_model_requirements(model, req)
    return await _submit_pipeline_request(model, req)


@app.post("/api/tts/{model}/upload")
async def tts_model_upload(
    model: str,
    text: str = Form(...),
    reference_audio: UploadFile | None = File(None),
    reference_audio_name: str | None = Form(None),
    reference_text: str = Form(""),
    voice: str = Form(""),
    language: str = Form("en"),
    mode: str = Form("cloned"),
    output_format: str = Form("wav"),
    speed: float | None = Form(None),
    temperature: float | None = Form(None),
    repetition_penalty: float | None = Form(None),
    top_p: float | None = Form(None),
    top_k: int | None = Form(None),
    cfg_scale: float | None = Form(None),
    exaggeration: float | None = Form(None),
    cfg_weight: float | None = Form(None),
    cfg_alpha: float | None = Form(None),
    waveform_temperature: float | None = Form(None),
    seed: int | None = Form(None),
    nfe_step: int | None = Form(None),
    pitch: int | None = Form(None),
    volume: int | None = Form(None),
    speaker_idx: int | None = Form(None),
    voice_description: str | None = Form(None),
    de_reverb: float | None = Form(None),
    de_ess: float | None = Form(None),
    skip_post_process: bool = Form(False),
    auto_retry: int | None = Form(None),
    verify_whisper: bool = Form(False),
    whisper_model: str | None = Form(None),
    tolerance: float | None = Form(None),
    device: str | None = Form(None),
    worker_id: str | None = Form(None),
    save_path: str | None = Form(None),
    deliver_to: str | None = Form(None),
    generate_srt: bool = Form(False),
    srt_words_per_line: int = Form(3),
    srt_size: str = Form("base"),
    # Audio profile overrides (None = use model default)
    inter_pause_sec: float | None = Form(None),
    front_pad_sec: float | None = Form(None),
    padding_sec: float | None = Form(None),
    trim_db: float | None = Form(None),
    min_silence_ms: int | None = Form(None),
    front_protect_ms: int | None = Form(None),
    end_protect_ms: int | None = Form(None),
    clipping: float | None = Form(None),
    lufs: float | None = Form(None),
):
    """Multipart TTS endpoint — avoids base64 bloat for reference audio uploads.

    Accepts ALL the same parameters as the JSON endpoint.
    """
    if model not in _TTS_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown TTS model: {model}")

    ref_path = None
    try:
        # Save uploaded reference audio to temp file
        if reference_audio and reference_audio.filename:
            ext = (Path(reference_audio.filename).suffix or ".wav").lower()
            if ext not in _AUDIO_EXTS:
                raise HTTPException(status_code=400, detail=f"Unsupported reference audio format: {ext}")
            ref_path = OUTPUT_DIR / f"ref_{uuid.uuid4().hex}{ext}"
            content = await reference_audio.read(_MAX_REFERENCE_AUDIO_BYTES + 1)
            if len(content) > _MAX_REFERENCE_AUDIO_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Reference audio too large (max {_MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)}MB)",
                )
            ref_path.write_bytes(content)

        req = PipelineTTSRequest(
            text=text,
            voice=voice or None,
            reference_audio=str(ref_path) if ref_path else None,
            reference_audio_name=reference_audio_name or None,
            reference_text=reference_text or None,
            language=language,
            mode=mode,
            output_format=output_format,
            speed=speed,
            temperature=temperature,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            cfg_scale=cfg_scale,
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
            cfg_alpha=cfg_alpha,
            waveform_temperature=waveform_temperature,
            seed=seed,
            nfe_step=nfe_step,
            pitch=pitch,
            volume=volume,
            speaker_idx=speaker_idx,
            voice_description=voice_description or None,
            de_reverb=de_reverb,
            de_ess=de_ess,
            skip_post_process=skip_post_process,
            auto_retry=auto_retry,
            verify_whisper=verify_whisper,
            whisper_model=whisper_model,
            tolerance=tolerance,
            device=device,
            worker_id=worker_id,
            save_path=save_path,
            deliver_to=deliver_to,
            generate_srt=generate_srt,
            srt_words_per_line=srt_words_per_line,
            srt_size=srt_size,
            inter_pause_sec=inter_pause_sec,
            front_pad_sec=front_pad_sec,
            padding_sec=padding_sec,
            trim_db=trim_db,
            min_silence_ms=min_silence_ms,
            front_protect_ms=front_protect_ms,
            end_protect_ms=end_protect_ms,
            clipping=clipping,
            lufs=lufs,
        )
        return await _handle_pipeline_request(model, req)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    finally:
        if ref_path and ref_path.exists():
            ref_path.unlink(missing_ok=True)


async def _submit_pipeline_request(model: str, req: PipelineTTSRequest):
    """Async TTS — same prep as _handle_pipeline_request but returns
    job_id immediately while the pipeline runs in the background."""
    return await _handle_pipeline_request(model, req, async_submit=True)


def _prepare_pipeline_request(model: str, req: PipelineTTSRequest):
    _validate_model_requirements(model, req)
    text = req.text.strip()
    chunks = chunk_text_for_model(text, model)
    if not chunks:
        raise HTTPException(status_code=400, detail="Text must contain spoken content")
    if len(chunks) > _MAX_PIPELINE_CHUNKS:
        raise HTTPException(status_code=413, detail="Input creates too many chunks")
    if req.save_path and req.deliver_to:
        raise HTTPException(status_code=400, detail="save_path and deliver_to are mutually exclusive")
    if req.worker_id:
        selected = registry.get(req.worker_id)
        if selected is None or selected.model != model or (req.device and selected.device != req.device):
            raise HTTPException(status_code=400, detail="Selected worker does not match model/device")
    params = _effective_pipeline_params(model, req)
    params["auto_retry"] = req.auto_retry if req.auto_retry is not None else MAX_RETRIES
    fmt = req.output_format or "wav"
    if req.deliver_to:
        _, fmt = _resolve_deliver_to(req.deliver_to, fmt)
    params["output_format"] = fmt
    job_dir, stem = _resolve_output_paths(model, (req.save_path or "").strip())
    profile = dict(PROFILES.get(model, PROFILES["default"]))
    for key in ("inter_pause_sec", "front_pad_sec", "padding_sec", "trim_db",
                "min_silence_ms", "front_protect_ms", "end_protect_ms", "clipping", "lufs"):
        if params.get(key) is not None:
            profile[key] = params[key]
    return chunks, params, profile, job_dir, stem


def _persist_references(params: dict, job_dir: Path) -> None:
    """Snapshot references so recovery does not depend on an upload or voice edit."""
    copies = {}
    def save(value):
        if not value or not _looks_like_external_path(value):
            return value
        source = Path(value)
        if not source.is_file():
            return value
        if value not in copies:
            target = job_dir / f"reference_{len(copies)}{source.suffix.lower()}"
            shutil.copy2(source, target)
            copies[value] = str(target)
        return copies[value]
    for key in ("voice", "reference_audio"):
        params[key] = save(params.get(key))
    if params.get("reference_audios"):
        params["reference_audios"] = [save(v) for v in params["reference_audios"]]


async def _handle_pipeline_request(model: str, req: PipelineTTSRequest, *,
                                   async_submit: bool = False):
    chunks, params, profile, job_dir, stem = _prepare_pipeline_request(model, req)
    tmp_ref = None
    try:
        tmp_ref = _normalize_reference_inputs(params)
        if req.worker_id:
            selected = registry.get(req.worker_id)
            if selected is None or selected.model != model or (
                    req.device and selected.device != req.device):
                raise HTTPException(status_code=400, detail="Selected worker does not match model/device")
        job_dir.mkdir(parents=True, exist_ok=False)
        _persist_references(params, job_dir)
        job = job_manager.create_job(
            model=model, text=req.text.strip(), chunks=chunks, params=params,
            output_format=params["output_format"], sample_rate=profile["sample_rate"],
            stem=stem, job_dir=job_dir)
    finally:
        if tmp_ref:
            tmp_ref.unlink(missing_ok=True)
    job_id = job["job_id"]
    with _running_jobs_lock:
        _running_jobs.setdefault(model, set()).add(job_id)
    try:
        future = executor.submit(
            _run_pipeline, model, job_id, chunks, 0, params, profile, job_dir, stem,
            params["output_format"], params["auto_retry"], req.save_path or "",
            req.deliver_to, bool(req.generate_srt), req.srt_words_per_line or 3,
            req.srt_size or "base")
    except Exception as exc:
        with _running_jobs_lock:
            _running_jobs.get(model, set()).discard(job_id)
        job_manager.fail_job(job_id, str(exc))
        raise
    if async_submit:
        return JSONResponse({
            "status": "submitted", "job_id": job_id, "model": model,
            "total_chunks": len(chunks), "stem": stem,
            "output_format": params["output_format"],
            "poll_url": f"/api/jobs/{job_id}",
            "output_url": f"/api/jobs/{job_id}/output",
        }, status_code=202)
    # A disconnected HTTP client must not cancel the executor's persisted job.
    result = await asyncio.shield(asyncio.wrap_future(future))
    if "error" in result:
        result.setdefault("detail", result.get("reason", result["error"]))
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


# ============================================================
# Chunk processing helpers
# ============================================================
def _process_single_chunk(model: str, job_id: str, i: int, chunk_text: str,
                          params: dict, profile: dict, job_dir: Path,
                          sr: int, speed: float, de_reverb: float, de_ess: float,
                          tolerance: float, do_verify_whisper: bool,
                          skip_post_process: bool, max_retries: int,
                          http_client: httpx.Client) -> dict | None:
    """Process a single chunk: infer, post-process, verify. Returns error dict or None on success."""
    retry_count = 0
    while True:
        # Check cancellation before each attempt (catches running chunks
        # that were already in-flight when another chunk failed)
        if job_manager.is_cancelled(job_id):
            return {"error": "cancelled", "reason": "Job cancelled", "job_id": job_id}

        try:
            infer_params = dict(params)
            raw_data, chunk_sr = _infer_via_worker(model, chunk_text, infer_params,
                                                    job_id, http_client=http_client)

            front_pad = profile.get("front_pad_sec", 0.0)
            if front_pad > 0:
                pad = np.zeros(int(chunk_sr * front_pad), dtype=np.float32)
                raw_data = np.concatenate([pad, raw_data])

            tmp_wav = OUTPUT_DIR / f"raw_{model}_{i}_{uuid.uuid4().hex}.wav"
            try:
                sf.write(str(tmp_wav), raw_data, chunk_sr, subtype="PCM_16")

                if not skip_post_process:
                    post_process(str(tmp_wav), profile, speed, de_reverb, de_ess)

                if do_verify_whisper:
                    whisper_size = params.get("whisper_model") or WHISPER_MODEL_SIZE
                    passed, sim, transcript = _verify_whisper_via_worker(
                        str(tmp_wav), _subtitle_source_text(model, chunk_text),
                        params.get("language", "en"), tolerance, whisper_size,
                    )
                    job_manager.update_chunk(
                        job_id, i, status="success" if passed else "failed",
                        whisper_transcript=transcript, whisper_similarity=sim,
                        verification_passed=passed,
                    )
                    if not passed:
                        raise ValueError(f"Whisper verification failed (similarity={sim:.3f})")

                data, _ = sf.read(str(tmp_wav))
                duration_sec = len(data) / chunk_sr
                chunk_wav = job_dir / f"chunk_{i:03d}.wav"
                tmp_wav.replace(chunk_wav)
            except Exception:
                tmp_wav.unlink(missing_ok=True)
                raise

            # Re-check cancellation right before the success write. If a sibling
            # chunk already failed (which calls request_cancel + fail_job) or the
            # job was cancelled while this chunk was mid-inference, skip the
            # success update_chunk so we don't resurrect a finalized job's
            # progress/chunks_completed. Return None (no error) so the caller
            # does not re-finalize the already-terminal job.
            if job_manager.is_cancelled(job_id):
                logger.info("[%s %s] Chunk %03d completed but job cancelled — discarding result",
                            _ts(), model.upper(), i)
                return None
            job_manager.update_chunk(
                job_id, i, status="success", duration=duration_sec,
                audio_file=chunk_wav.name,
            )
            logger.info("[%s %s] Chunk %03d -> %.2fs (success)",
                        _ts(), model.upper(), i, duration_sec)
            return None  # success

        except Exception as e:
            if job_manager.is_cancelled(job_id):
                return {"error": "cancelled", "reason": "Job cancelled", "job_id": job_id}
            retry_count += 1
            error_msg = str(e) or "Unknown error"
            if retry_count > max_retries:
                logger.error("[%s %s] Chunk %03d failed after %d retries: %s",
                             _ts(), model.upper(), i, max_retries, error_msg)
                is_whisper_failure = "Whisper" in error_msg or "verification" in error_msg
                if not is_whisper_failure:
                    job_manager.update_chunk(job_id, i, status="failed", error=error_msg)
                return {
                    "error": "generation_failed",
                    "reason": f"Chunk {i} failed after {max_retries} retries",
                    "failed_at_chunk": i,
                    "job_id": job_id,
                    "job_folder": str(job_dir.name),
                    "recover_endpoint": f"POST /api/jobs/{job_id}/recover",
                }
            logger.warning("[%s %s] Chunk %03d retry %d/%d: %s",
                           _ts(), model.upper(), i, retry_count, max_retries, error_msg)
            time.sleep(1)


def _run_chunks_sequential(model, job_id, chunks, start_from, params, profile,
                           job_dir, sr, speed, de_reverb, de_ess, tolerance,
                           do_verify_whisper, skip_post_process, max_retries,
                           http_client, is_bark, bark_base_history,
                           history_reset_every):
    """Process chunks one at a time. Required for Bark (history chaining)."""
    for i in range(start_from, len(chunks)):
        if job_manager.is_cancelled(job_id):
            logger.info("[%s %s] Job %s cancelled at chunk %d",
                        _ts(), model.upper(), job_id, i)
            job_manager.cancel_job(job_id)
            return {"error": "Cancelled", "job_id": job_id, "cancelled_at_chunk": i}

        # Bark history management
        if is_bark:
            chunk_offset = i - start_from
            if chunk_offset > 0 and chunk_offset % history_reset_every == 0:
                logger.info("[%s BARK] Resetting history at chunk %d", _ts(), i)
                with _bark_history_lock:
                    _bark_history[job_id] = bark_base_history

        infer_params = dict(params)
        if is_bark:
            with _bark_history_lock:
                infer_params["history_prompt"] = _bark_history.get(job_id)

        result = _process_single_chunk(
            model, job_id, i, chunks[i], infer_params if is_bark else params,
            profile, job_dir, sr, speed, de_reverb, de_ess, tolerance,
            do_verify_whisper, skip_post_process, max_retries, http_client,
        )
        if result is not None:
            if str(result.get("error", "")).lower() == "cancelled":
                job_manager.cancel_job(job_id, result.get("reason", "Cancelled by user"))
            else:
                job_manager.fail_job(job_id, result.get("reason", "Chunk failed"))
            return result
    return None  # all chunks succeeded


def _run_chunks_parallel(model, job_id, chunks, start_from, params, profile,
                         job_dir, sr, speed, de_reverb, de_ess, tolerance,
                         do_verify_whisper, skip_post_process, max_retries,
                         http_client, n_workers):
    """Process chunks in parallel across multiple workers.
    Non-Bark models only — no history chaining dependency between chunks."""
    from concurrent.futures import ThreadPoolExecutor as ChunkPool, as_completed

    remaining = list(range(start_from, len(chunks)))
    # Limit parallelism to worker count (each chunk marks a worker busy)
    parallelism = min(n_workers, len(remaining))
    logger.info("[%s %s] Parallel chunk processing: %d chunks, %d workers",
                _ts(), model.upper(), len(remaining), parallelism)

    if http_client is None:
        from config import MODEL_INFER_TIMEOUT, DEFAULT_INFER_TIMEOUT
        infer_timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
        shared_client = httpx.Client(
            timeout=infer_timeout,
            limits=httpx.Limits(
                max_connections=parallelism + 2,
                max_keepalive_connections=parallelism,
            ),
        )
    else:
        shared_client = http_client

    try:
        with ChunkPool(max_workers=parallelism) as chunk_pool:
            futures = {}
            for i in remaining:
                if job_manager.is_cancelled(job_id):
                    break
                fut = chunk_pool.submit(
                    _process_single_chunk,
                    model, job_id, i, chunks[i], params, profile,
                    job_dir, sr, speed, de_reverb, de_ess, tolerance,
                    do_verify_whisper, skip_post_process, max_retries, shared_client,
                )
                futures[fut] = i

            failure = None
            for fut in as_completed(futures):
                try:
                    failure = fut.result()
                except Exception as exc:
                    failure = {"error": "generation_failed", "reason": str(exc),
                               "failed_at_chunk": futures[fut], "job_id": job_id}
                if failure is not None:
                    job_manager.request_cancel(job_id)
                    for other in futures:
                        other.cancel()
                    break
        # ThreadPoolExecutor has drained all running siblings before publishing
        # a terminal status, so polling/deletion can no longer race their writes.
        if failure is not None:
            if str(failure.get("error", "")).lower() == "cancelled":
                job_manager.cancel_job(job_id)
            else:
                job_manager.fail_job(job_id, failure.get("reason", "Chunk failed"))
            return failure

        if job_manager.is_cancelled(job_id):
            job_manager.cancel_job(job_id)
            return {"error": "Cancelled", "job_id": job_id}

        return None  # all chunks succeeded
    finally:
        if http_client is None and shared_client is not None:
            shared_client.close()


# ============================================================
# External delivery helpers
# ============================================================
def _resolve_deliver_to(deliver_to: str, output_format: str) -> tuple[Path, str]:
    """Resolve a deliver_to string into (absolute_audio_path, output_format).

    - If the path has an audio extension, use it as-is and coerce output_format
      to match the extension.
    - If the path has no extension, append ``.{output_format}``.
    - If the path has a non-audio extension, reject.
    User trusts all absolute paths (no allowlist) — this runs on a single-user
    local desktop app.
    """
    if not deliver_to:
        raise HTTPException(status_code=400, detail="deliver_to is empty")
    path = Path(deliver_to).expanduser()
    if not path.is_absolute():
        raise HTTPException(
            status_code=400,
            detail=f"deliver_to must be an absolute path (got {deliver_to!r})",
        )
    suffix = path.suffix.lower().lstrip(".")
    if not suffix:
        path = path.with_suffix(f".{output_format}")
        suffix = output_format
    elif suffix not in _OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(f"deliver_to has unsupported extension '.{suffix}'. "
                    f"Use one of: {sorted(_OUTPUT_FORMATS)}"),
        )
    return path, suffix


def _generate_srt_in_pipeline(audio_path: Path, job_dir: Path, stem: str,
                              words_per_line: int, size: str,
                              source_text: str | None = None,
                              audio_duration: float | None = None,
                              model: str | None = None) -> dict:
    """Run Whisper synchronously inside the pipeline thread and write
    ``<stem>.srt`` + ``<stem>_timing.json`` next to the audio inside ``job_dir``.

    Returns a small summary dict. Raises on hard failures so the caller can log
    and continue without delivery (since the audio still completed)."""
    source_text = _subtitle_source_text(model, source_text)
    _ensure_pipeline_worker_ready_sync("whisper", {})
    worker = _reserve_worker("whisper", "srt")
    try:
        with httpx.Client(timeout=600.0) as client:
            resp = client.post(
                f"http://127.0.0.1:{worker.port}/transcribe",
                json={
                    "audio_path": str(audio_path),
                    "size": size,
                    "word_timestamps": True,
                    "initial_prompt": (source_text or "")[:2000] or None,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        logger.warning("SRT generation failed: %s", e)
        return {"error": str(e)}
    finally:
        registry.mark_ready(worker.worker_id)

    raw_words = result.get("words") or []
    raw_text = (result.get("text") or "").strip()
    language = result.get("language") or ""
    source_text = _subtitle_source_text(model, source_text)
    if source_text:
        words, alignment = _source_guided_word_timings(
            source_text, raw_words, audio_duration
        )
        text = source_text
    else:
        words = raw_words
        text = raw_text
        alignment = {"mode": "raw_asr", "raw_word_count": len(raw_words)}

    srt_path = job_dir / f"{stem}.srt"
    timing_path = job_dir / f"{stem}_timing.json"
    srt_text = _build_srt(words, words_per_line)
    try:
        srt_path.write_text(srt_text, encoding="utf-8")
        timing_path.write_text(
            json.dumps({
                "words": words,
                "text": text,
                "language": language,
                "source_text": source_text,
                "raw_words": raw_words,
                "raw_text": raw_text,
                "alignment": alignment,
            },
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("SRT sidecar write failed: %s", e)
        return {"error": str(e)}

    return {
        "srt_path": str(srt_path),
        "timing_path": str(timing_path),
        "line_count": srt_text.count("-->") if srt_text else 0,
        "word_count": len(words),
        "language": language,
        "source_guided": bool(source_text),
        "alignment": alignment,
    }


def _deliver_to_external(job_dir: Path, final_filename: str, stem: str,
                         deliver_audio_path: Path) -> dict:
    """Copy the final audio (and SRT/timing sidecars if present) from the
    work dir to the external destination. Creates parent dirs as needed.

    Returns a dict with the actual on-disk destination paths.
    """
    src_audio = job_dir / final_filename
    if not src_audio.exists():
        raise FileNotFoundError(f"Final audio not in work dir: {src_audio}")

    deliver_audio_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_audio, deliver_audio_path)
    delivered = {"audio": str(deliver_audio_path)}

    src_srt = job_dir / f"{stem}.srt"
    src_timing = job_dir / f"{stem}_timing.json"
    if src_srt.exists():
        dst_srt = deliver_audio_path.with_suffix(".srt")
        shutil.copy2(src_srt, dst_srt)
        delivered["srt"] = str(dst_srt)
    if src_timing.exists():
        dst_timing = deliver_audio_path.parent / f"{deliver_audio_path.stem}_timing.json"
        shutil.copy2(src_timing, dst_timing)
        delivered["timing"] = str(dst_timing)
    return delivered


# ============================================================
# Pipeline core (runs in thread pool)
# ============================================================
def _matching_workers(model: str, params: dict):
    device = params.get("device")
    if device:
        device = WorkerManager.normalize_device(device)
    return [w for w in registry.workers_for_model(model)
            if w.status in ("ready", "busy", "starting", "loading")
            and (not device or w.device == device)
            and (not params.get("worker_id") or w.worker_id == params["worker_id"])]


def _ensure_pipeline_worker_ready_sync(model: str, params: dict) -> None:
    if _matching_workers(model, params):
        return
    if params.get("worker_id"):
        raise RuntimeError("Selected worker is no longer available")
    if not WORKER_AUTO_SPAWN:
        raise RuntimeError(f"No worker available for model '{model}'")
    with _pipeline_spawn_locks.setdefault(model, threading.Lock()):
        if not _matching_workers(model, params):
            installed = _check_model_installed(model)
            if not (installed.get("status") == "ready" or
                    (installed.get("status") == "packages_only" and installed.get("weights_on_demand"))):
                with _active_installs_lock:
                    if _active_installs:
                        raise RuntimeError("Model installation is already active; retry after it completes")
                    _active_installs[model] = True
                    _install_cancel_models.discard(model)
                if not _run_install_background(model):
                    raise RuntimeError(f"Could not install {model}; inspect setup status")
            asyncio.run(worker_manager.spawn_worker(model, params.get("device")))


def _reserve_worker(model, job_id, params=None, exclude=None):
    params = params or {}
    deadline = time.monotonic() + MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
    while True:
        if job_id and job_manager.is_cancelled(job_id):
            raise InterruptedError("Job cancelled while waiting for worker")
        worker = registry.atomic_pick_and_mark_busy(
            model, job_id, device=params.get("device"), worker_id=params.get("worker_id"),
            exclude=exclude)
        if worker:
            return worker
        candidates = [w for w in _matching_workers(model, params)
                      if w.worker_id not in (exclude or set())]
        if not candidates or time.monotonic() >= deadline:
            raise RuntimeError(f"No available worker for '{model}'")
        time.sleep(0.05)


def _reject_active_job(job_id):
    with _running_jobs_lock:
        if any(job_id in ids for ids in _running_jobs.values()):
            raise HTTPException(status_code=409, detail="Job is active; cancel and wait before modifying it")


def _check_job_cancelled(job_id):
    if job_manager.is_cancelled(job_id):
        raise InterruptedError("Job cancelled")



def _run_pipeline(model: str, job_id: str, chunks: list[str],
                  start_from: int, params: dict, profile: dict,
                  job_dir: Path, stem: str, output_format: str,
                  max_retries: int, save_path_input: str,
                  deliver_to: str | None = None,
                  generate_srt: bool = False,
                  srt_words_per_line: int = 3,
                  srt_size: str = "base") -> dict:
    """Execute the full TTS pipeline, delegating inference to workers via HTTP."""
    logger.info("[%s %s] Pipeline thread started for job %s (thread=%s, chunks=%d)",
                _ts(), model.upper(), job_id, threading.current_thread().name, len(chunks))

    with _running_jobs_lock:
        _running_jobs.setdefault(model, set()).add(job_id)
    sr = profile["sample_rate"]

    speed = float(params.get("speed", 1.0))
    de_reverb = float(params.get("de_reverb", 0.7))
    de_ess = min(1.0, max(0.0, float(params.get("de_ess", 0.0))))
    tolerance = float(params.get("tolerance", WHISPER_DEFAULT_TOLERANCE))
    do_verify_whisper = params.get("verify_whisper", False)
    skip_post_process = params.get("skip_post_process", False)

    try:
        _check_job_cancelled(job_id)
        job_manager.update_progress(
            job_id,
            "loading_worker",
            f"Preparing {model} worker",
            current=start_from,
            total=len(chunks),
        )
        _ensure_pipeline_worker_ready_sync(model, params)

        # Bark: initialize history prompt state
        bark_base_history = None
        is_bark = model == "bark"
        if is_bark:
            voice_preset = params.get("voice") or "v2/en_speaker_6"
            bark_base_history = voice_preset
            with _bark_history_lock:
                _bark_history[job_id] = bark_base_history
            history_reset_every = profile.get("history_reset_every", 5)

        # Determine parallelism: Bark must be sequential (history chaining),
        # other models can process chunks in parallel across multiple workers
        n_workers = len(registry.get_ready_workers(model))
        can_parallel = not is_bark and n_workers > 1 and len(chunks) - start_from > 1

        if can_parallel:
            job_manager.update_progress(
                job_id,
                "synthesizing",
                f"Synthesizing {len(chunks) - start_from} chunk(s) across {n_workers} workers",
                current=start_from,
                total=len(chunks),
            )
            # Each parallel thread creates its own httpx.Client to avoid
            # connection pool contention and cascading timeout failures.
            result = _run_chunks_parallel(
                model, job_id, chunks, start_from, params, profile,
                job_dir, sr, speed, de_reverb, de_ess, tolerance,
                do_verify_whisper, skip_post_process, max_retries,
                None, n_workers,
            )
            if result is not None:
                return result  # error or cancellation
        else:
            job_manager.update_progress(
                job_id,
                "synthesizing",
                f"Synthesizing {len(chunks) - start_from} chunk(s)",
                current=start_from,
                total=len(chunks),
            )
            # Sequential: share one client across all chunks (reuses TCP connection)
            with httpx.Client() as http_client:
                result = _run_chunks_sequential(
                    model, job_id, chunks, start_from, params, profile,
                    job_dir, sr, speed, de_reverb, de_ess, tolerance,
                    do_verify_whisper, skip_post_process, max_retries,
                    http_client, is_bark, bark_base_history,
                    history_reset_every if is_bark else 0,
                )
                if result is not None:
                    return result  # error or cancellation

        _check_job_cancelled(job_id)
        # --- Assembly ---
        missing = [
            f"chunk_{i:03d}.wav" for i in range(len(chunks))
            if not (job_dir / f"chunk_{i:03d}.wav").exists()
        ]
        if missing:
            job_manager.fail_job(job_id, f"Missing {len(missing)} chunks")
            return {
                "error": "incomplete",
                "status": "incomplete",
                "message": f"Missing {len(missing)} chunk(s)",
                "missing_count": len(missing),
                "job_id": job_id,
                "job_folder": str(job_dir.name),
                "recover_endpoint": f"POST /api/jobs/{job_id}/recover",
            }

        chunk_files = [job_dir / f"chunk_{i:03d}.wav" for i in range(len(chunks))]

        assembled_wav = OUTPUT_DIR / f"assembled_{uuid.uuid4().hex}.wav"
        try:
            job_manager.update_progress(
                job_id,
                "assembling",
                "Assembling final audio",
                current=len(chunks),
                total=len(chunks),
            )
            assemble_chunks(
                chunk_files, str(assembled_wav), sr,
                inter_pause=profile.get("inter_pause_sec", 0.25),
                front_pad=profile.get("padding_sec", 0.5),
                end_pad=profile.get("padding_sec", 0.5),
            )

            # Calculate duration from the assembled WAV (before format conversion,
            # since sf.read cannot read mp3/ogg/m4a)
            total_duration = sf.info(str(assembled_wav)).duration

            # Format conversion
            final_filename = f"{stem}_final.{output_format}"
            final_path = job_dir / final_filename

            if output_format != "wav":
                convert_format(str(assembled_wav), str(final_path), output_format)
                assembled_wav.unlink(missing_ok=True)
            else:
                assembled_wav.replace(final_path)
        except Exception:
            assembled_wav.unlink(missing_ok=True)
            raise

        _check_job_cancelled(job_id)
        # ----- Optional SRT generation (must happen before delivery/purge) -----
        srt_info: dict | None = None
        if generate_srt:
            try:
                job_manager.update_progress(
                    job_id,
                    "generating_srt",
                    "Generating subtitle timing",
                    current=len(chunks),
                    total=len(chunks),
                )
                srt_info = _generate_srt_in_pipeline(
                    final_path, job_dir, stem,
                    srt_words_per_line, srt_size,
                    str(params.get("text") or " ".join(chunks)),
                    total_duration,
                    model,
                )
                logger.info("[%s SRT] job %s → %s", _ts(), job_id, srt_info)
            except Exception as e:
                logger.warning("SRT generation raised: %s", e)
                srt_info = {"error": str(e)}

        _check_job_cancelled(job_id)
        # ----- Optional external delivery + work-dir purge -----
        delivered_info: dict | None = None
        if deliver_to:
            try:
                job_manager.update_progress(
                    job_id,
                    "delivering",
                    "Delivering final artifact",
                    current=len(chunks),
                    total=len(chunks),
                )
                dest_path, _ = _resolve_deliver_to(deliver_to, output_format)
                delivered_info = _deliver_to_external(
                    job_dir, final_filename, stem, dest_path,
                )
                logger.info("[%s DELIVER] job %s → %s",
                            _ts(), job_id, delivered_info)
            except Exception as e:
                logger.error("[%s DELIVER] job %s failed: %s",
                             _ts(), job_id, e)
                raise RuntimeError(f"Requested delivery failed: {e}") from e

        _check_job_cancelled(job_id)
        job_manager.update_progress(
            job_id,
            "finalizing",
            "Finalizing job metadata",
            current=len(chunks),
            total=len(chunks),
        )
        if not job_manager.complete_job(job_id, final_filename, total_duration, srt_info=srt_info):
            _check_job_cancelled(job_id)
            raise RuntimeError("Could not persist completed job")

        logger.info("[%s %s] Job %s complete: %s (%.1fs)",
                    _ts(), model.upper(), job_id, final_filename, total_duration)

        if deliver_to and delivered_info and "error" not in delivered_info:
            try:
                # Purge work dir only after successful delivery. On failure the
                # job stays around so /recover or manual download still works.
                _archive_delivered_job_manifest(job_id, delivered_info)
                if job_manager.delete_job(job_id, force=True):
                    logger.info("[%s PURGE] job %s work dir cleaned",
                                _ts(), job_id)
            except Exception as e:
                logger.error("[%s PURGE] job %s failed: %s",
                             _ts(), job_id, e)
                delivered_info = {"error": str(e)}

        resp = {
            "status": "completed",
            "job_id": job_id,
            "filename": final_filename,
            "saved_to": (delivered_info or {}).get("audio", str(final_path)),
            "output_url": f"/api/jobs/{job_id}/output",
            "sample_rate": sr,
            "duration_sec": round(total_duration, 3),
            "format": output_format,
        }
        if srt_info is not None:
            resp["srt"] = srt_info
        if delivered_info is not None:
            resp["delivered_to"] = delivered_info

        # Inline base64 only when the audio is still on this server's disk —
        # if it was delivered externally, the work dir is gone.
        if not save_path_input and not deliver_to:
            final_size = final_path.stat().st_size
            if final_size <= _MAX_INLINE_AUDIO_BYTES:
                resp["audio_base64"] = base64.b64encode(final_path.read_bytes()).decode("utf-8")
            else:
                resp["audio_base64_omitted"] = True
                resp["output_url"] = f"/api/jobs/{job_id}/output"
                resp["message"] = (
                    f"Audio saved to job output; inline response omitted "
                    f"because file is {final_size} bytes"
                )

        return resp

    except InterruptedError as e:
        job_manager.cancel_job(job_id, str(e))
        return {"error": "cancelled", "status": "cancelled", "job_id": job_id}
    except Exception as e:
        error_str = str(e) or "Unknown error"
        logger.error("[%s %s] Unexpected error: %s", _ts(), model.upper(), error_str)
        job_manager.fail_job(job_id, error_str)
        return {
            "error": "generation_failed",
            "reason": error_str,
            "job_id": job_id,
            "job_folder": str(job_dir.name),
        }
    finally:
        with _running_jobs_lock:
            active = _running_jobs.get(model, set())
            active.discard(job_id)
            if not active:
                _running_jobs.pop(model, None)
        with _bark_history_lock:
            _bark_history.pop(job_id, None)
        job_manager.cleanup_cancel_flag(job_id)


def _run_pipeline_recovery(model: str, job: dict, job_dir: Path):
    """Resume a recovered job from where it left off, restoring original settings."""
    chunks = [c["text"] for c in job["chunks"]]
    start_from = job["chunks_completed"]
    # parameters may be absent in a legacy/partially-written job.json; downstream
    # reads all use params.get(...), so an empty dict yields default behavior
    # instead of a KeyError surfacing only as a generic background "Recovery failed".
    params = dict(job.get("parameters") or {})
    # Worker IDs belong to one gateway lifetime. Recovery keeps the requested
    # device but can replace a worker that exited since the original attempt.
    if params.get("worker_id") and not registry.get(params["worker_id"]):
        params.pop("worker_id")
    output_format = job.get("output_format", "wav")
    # Use the original stem from job.json if available; fall back to dir name
    stem = job.get("stem") or job_dir.name
    max_retries = params.get("auto_retry") if params.get("auto_retry") is not None else MAX_RETRIES

    # Restore original audio profile overrides from saved params
    profile = dict(PROFILES.get(model, PROFILES["default"]))
    _profile_keys = ("inter_pause_sec", "front_pad_sec", "padding_sec",
                     "trim_db", "min_silence_ms", "front_protect_ms",
                     "end_protect_ms", "clipping", "lufs")
    for k in _profile_keys:
        v = params.get(k)
        if v is not None:
            profile[k] = v

    _run_pipeline(
        model, job["job_id"], chunks, start_from, params,
        profile, job_dir, stem, output_format, max_retries, str(job_dir),
        params.get("deliver_to") or None,
        bool(params.get("generate_srt")),
        int(params.get("srt_words_per_line") or 3),
        (params.get("srt_size") or "base"),
    )


# ============================================================
# Worker-delegated inference
# ============================================================
def _finalize_worker(worker_id: str, timed_out: bool = False) -> None:
    """Check if a worker is still alive and mark ready or dead accordingly.

    If timed_out=True, the worker is assumed stuck (e.g. hung CUDA generate())
    and will be forcibly killed at the process level.
    """
    worker_info = registry.get(worker_id)
    if not worker_info:
        return

    if timed_out and worker_info.process and worker_info.process.poll() is None:
        # Worker process is alive but not responding — kill entire process group
        pid = worker_info.process.pid
        logger.warning("Worker %s timed out — killing stuck process (pid=%s)",
                      worker_id, pid)
        try:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, 9)
            except (OSError, ProcessLookupError):
                worker_info.process.kill()
            try:
                worker_info.process.wait(timeout=5)
            except Exception:
                pass
        except Exception as e:
            logger.error("Failed to kill stuck worker %s: %s", worker_id, e)
        # Re-verify the process actually died (a SIGKILL stuck in an
        # uninterruptible CUDA driver call may survive the first kill);
        # mirror WorkerManager._force_kill's recheck before releasing the slot.
        try:
            os.kill(pid, 0)  # signal 0 = liveness probe
            logger.warning("Worker %s pid %d still alive after kill — re-sending SIGKILL",
                           worker_id, pid)
            try:
                os.killpg(os.getpgid(pid), 9)
            except (OSError, ProcessLookupError):
                os.kill(pid, 9)
        except (OSError, ProcessLookupError):
            pass  # confirmed dead
        if worker_info.log_fh:
            try:
                worker_info.log_fh.close()
            except Exception:
                pass
            worker_info.log_fh = None
        worker_info.process = None
        if registry.unregister(worker_id) is not None:
            registry.release_port(worker_info.port)
        # Reclaim VRAM/host memory after a forced timeout kill, matching the
        # canonical WorkerManager.kill_worker path.
        try:
            worker_manager._reclaim_memory()
        except Exception:
            pass
        return

    if worker_info.process and worker_info.process.poll() is not None:
        logger.warning("Worker %s process died (exit code %d)",
                      worker_id, worker_info.process.returncode)
        if worker_info.log_fh:
            try:
                worker_info.log_fh.close()
            except Exception:
                pass
            worker_info.log_fh = None
        if registry.unregister(worker_id) is not None:
            registry.release_port(worker_info.port)
    else:
        registry.mark_ready(worker_id)


def _infer_via_worker(model: str, text: str, params: dict,
                      job_id: str | None = None,
                      http_client: httpx.Client | None = None) -> tuple:
    """Send inference request to a worker and decode the response.

    Retries with different workers on connection failures.
    Args:
        http_client: Optional reusable httpx.Client (avoids per-chunk connection overhead).
    Returns (numpy_array, sample_rate).
    """
    logger.info("[%s %s] _infer_via_worker called for job %s, text=%r",
                _ts(), model.upper(), job_id, text[:80] + "..." if len(text) > 80 else text)

    # Pre-serialize bark history (do once, reuse across retries)
    send_params = dict(params)
    if model == "bark" and "history_prompt" in send_params:
        hp = send_params["history_prompt"]
        if isinstance(hp, tuple) and len(hp) == 3 and hasattr(hp[0], 'dtype'):
            encoded = []
            for arr in hp:
                buf = io.BytesIO()
                np.save(buf, arr)
                encoded.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
            send_params["history_prompt"] = {"_bark_b64": encoded}

    max_worker_attempts = 3
    last_error = None
    tried_workers: set[str] = set()

    # Large models may need longer for first inference (includes model loading)
    infer_timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)

    for attempt in range(max_worker_attempts):
        try:
            worker = _reserve_worker(model, job_id, params, tried_workers)
        except RuntimeError as exc:
            last_error = str(exc)
            break

        tried_workers.add(worker.worker_id)
        worker_id = worker.worker_id

        infer_start = time.time()
        try:
            url = f"http://127.0.0.1:{worker.port}/infer"
            logger.info("[%s %s] Sending POST %s (worker=%s, timeout=%.0fs)",
                        _ts(), model.upper(), url, worker_id, infer_timeout)

            # Reuse caller's client if provided, else create a one-shot client
            if http_client is not None:
                resp = http_client.post(url, json={"text": text, "params": send_params},
                                        timeout=infer_timeout)
            else:
                with httpx.Client(timeout=infer_timeout) as client:
                    resp = client.post(url, json={"text": text, "params": send_params})
            logger.info("[%s %s] Worker %s responded with status %d",
                        _ts(), model.upper(), worker_id, resp.status_code)

            if resp.status_code != 200:
                detail = resp.json().get("detail", resp.text) if resp.headers.get(
                    "content-type", "").startswith("application/json") else resp.text
                raise RuntimeError(
                    f"Worker {worker_id} returned {resp.status_code}: {detail}")

            data = resp.json()
            audio_b64 = data["audio_b64"]
            sample_rate = data["sample_rate"]

            buf = io.BytesIO(base64.b64decode(audio_b64))
            audio_array = np.load(buf, allow_pickle=False)

            # For bark, capture updated history
            if model == "bark" and "bark_history" in data and job_id:
                history_parts = []
                for part_b64 in data["bark_history"]:
                    hbuf = io.BytesIO(base64.b64decode(part_b64))
                    history_parts.append(np.load(hbuf, allow_pickle=False))
                with _bark_history_lock:
                    _bark_history[job_id] = tuple(history_parts)

            # Success - mark worker ready
            registry.mark_ready(worker_id)
            return audio_array, sample_rate

        except (httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout) as e:
            elapsed = time.time() - infer_start
            logger.error("Worker %s inference TIMED OUT after %.0fs (attempt %d/%d): %s",
                        worker_id, elapsed, attempt + 1, max_worker_attempts, e)
            _finalize_worker(worker_id, timed_out=True)
            last_error = f"Inference timed out after {int(elapsed)}s"
            continue

        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                httpx.ConnectTimeout, httpx.RemoteProtocolError,
                ConnectionError) as e:
            logger.warning("Worker %s connection failed (attempt %d/%d): %s",
                          worker_id, attempt + 1, max_worker_attempts, e)
            _finalize_worker(worker_id)
            last_error = str(e)
            continue

        except Exception:
            _finalize_worker(worker_id)
            raise

    if last_error:
        raise RuntimeError(
            f"All {len(tried_workers)} worker(s) for '{model}' failed: {last_error}")
    raise RuntimeError(f"No ready worker available for model '{model}'")


def _verify_whisper_via_worker(audio_path: str, expected_text: str,
                               language: str, tolerance: float,
                               whisper_size: str) -> tuple:
    """Verify audio via whisper worker, falling back to local whisper.

    Returns (passed: bool, similarity: float, transcript: str).
    """
    # Try whisper worker first
    worker = None
    try:
        _ensure_pipeline_worker_ready_sync("whisper", {})
        worker = _reserve_worker("whisper", "verify")
    except Exception as exc:
        logger.warning("Whisper worker unavailable: %s", exc)
    if worker:
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{worker.port}/transcribe",
                    json={"audio_path": audio_path, "size": whisper_size},
                )
            if resp.status_code == 200:
                result = resp.json()
                transcript = result.get("text", "")
                # Sanitize and use word-level comparison (consistent with local path)
                from text_utils import sanitize_for_whisper
                orig_san = sanitize_for_whisper(expected_text)
                trans_san = sanitize_for_whisper(transcript)
                sim = SequenceMatcher(None, orig_san.split(), trans_san.split()).ratio()
                passed = (sim * 100) >= tolerance
                return passed, sim, transcript
        except Exception as e:
            logger.warning("Whisper worker failed, falling back to local: %s", e)
        finally:
            registry.mark_ready(worker.worker_id)

    # Fallback: try local whisper (if torch available from some other source)
    # Cache the model to avoid reloading multi-GB weights on every call
    try:
        import whisper
        with _whisper_cache_lock:
            if whisper_size in _whisper_cache:
                _whisper_cache.move_to_end(whisper_size)
            else:
                logger.info("Loading local whisper model '%s' (caching for reuse)", whisper_size)
                _whisper_cache[whisper_size] = whisper.load_model(
                    whisper_size,
                    download_root=str(MODELS_DIR / "whisper"),
                )
                while len(_whisper_cache) > _WHISPER_CACHE_MAX:
                    evicted_key, evicted_model = _whisper_cache.popitem(last=False)
                    logger.info("Evicted whisper model '%s' from cache", evicted_key)
                    del evicted_model
                    gc.collect()
                    try:
                        import torch as _t
                        if _t.cuda.is_available():
                            _t.cuda.empty_cache()
                    except (ImportError, RuntimeError):
                        pass
            cached_model = _whisper_cache[whisper_size]
        return verify_with_whisper(audio_path, expected_text, None, tolerance, cached_model)
    except ImportError:
        logger.warning("No whisper available (no worker, no local torch)")
        raise RuntimeError("Whisper verification requested but Whisper is unavailable")


# ============================================================
# Path helpers
# ============================================================
# Shared path-containment primitive.
from pathsafe import is_within as _path_is_relative_to
from sidecar_paths import recorded_sidecar_path


def _safe_job_file_name(value: str | None) -> str | None:
    if not value or "\x00" in value:
        return None
    p = Path(value)
    if p.name != value or p.is_absolute() or "/" in value or "\\" in value:
        return None
    return value


def _under_allowed_audio_root(path: Path) -> bool:
    allowed = (VOICE_DIR.resolve(), OUTPUT_DIR.resolve(), PROJECTS_OUTPUT.resolve())
    resolved = path.resolve()
    return any(resolved == base or _path_is_relative_to(resolved, base) for base in allowed)


def _safe_path_exists(value: str | Path) -> bool:
    try:
        return Path(value).expanduser().exists()
    except (OSError, ValueError):
        return False


def _looks_like_external_path(value: str) -> bool:
    if not value:
        return False
    if "\x00" in value:
        return True
    if value.startswith(("/", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    if "\\" in value or ".." in value.split("/") or ".." in value.split("\\"):
        return True
    return _safe_path_exists(value)


def _validate_reference_audio_path(value: str, field_name: str = "reference_audio") -> str:
    """Validate an existing reference audio path or VOICE_DIR filename."""
    if not value or "\x00" in value:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")

    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and not candidate.exists():
        candidate = VOICE_DIR / Path(value).name

    resolved = candidate.resolve()
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"{field_name} file not found")
    if resolved.suffix.lower() not in _AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported {field_name} format: {resolved.suffix}")
    if not _under_allowed_audio_root(resolved):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be under {VOICE_DIR}, {OUTPUT_DIR}, or {PROJECTS_OUTPUT}",
        )
    return str(resolved)


def _normalize_reference_inputs(params: dict) -> Path | None:
    """Decode/validate reference audio values before they reach worker processes."""
    tmp_ref: Path | None = None
    if params.get("reference_audios"):
        params["reference_audios"] = [
            _validate_reference_audio_path(value, "reference_audios")
            for value in params["reference_audios"]
        ]

    voice = params.get("voice")
    if isinstance(voice, str) and _looks_like_external_path(voice):
        params["voice"] = _validate_reference_audio_path(voice, "voice")

    ref = params.get("reference_audio")
    if not isinstance(ref, str) or not ref.strip():
        return None

    ref = ref.strip()
    # File paths or saved voice filenames are validated directly.  A slash by
    # itself is not enough because Bark built-in voices use v2/name syntax.
    if (
        ref.startswith(("/", "~")) or
        re.match(r"^[A-Za-z]:[\\/]", ref) or
        "\\" in ref or
        ref.lower().endswith(tuple(_AUDIO_EXTS)) or
        _safe_path_exists(ref)
    ):
        params["reference_audio"] = _validate_reference_audio_path(ref)
        if not params.get("voice"):
            params["voice"] = params["reference_audio"]
        return None

    # Otherwise treat the value as inline base64 audio.
    max_b64_len = ((_MAX_REFERENCE_AUDIO_BYTES + 2) // 3) * 4
    if len(ref) > max_b64_len:
        raise HTTPException(
            status_code=413,
            detail=f"Reference audio too large (max {_MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)}MB)",
        )

    try:
        audio_bytes = base64.b64decode(ref, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid reference_audio base64: {e}") from e

    if len(audio_bytes) > _MAX_REFERENCE_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Reference audio too large (max {_MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)}MB)",
        )

    ref_name = params.get("reference_audio_name") or ""
    ext = (Path(ref_name).suffix or ".wav").lower()
    if ext not in _AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported reference audio format: {ext}")

    tmp_ref = OUTPUT_DIR / f"ref_{uuid.uuid4().hex}{ext}"
    try:
        tmp_ref.write_bytes(audio_bytes)
    except OSError:
        tmp_ref.unlink(missing_ok=True)
        raise
    params["reference_audio"] = str(tmp_ref)
    if not params.get("voice"):
        params["voice"] = str(tmp_ref)
    return tmp_ref


def _resolve_output_paths(model: str, save_path_input: str) -> tuple[Path, str]:
    """Determine job directory and file stem from save_path."""
    def _unique_save_job_dir(parent: Path, stem: str) -> Path:
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or model
        return parent / f"{safe_stem}_{uuid.uuid4().hex[:8]}"

    if not save_path_input:
        # Seconds-resolution timestamps collide when several async jobs are
        # submitted together. Keep the readable timestamp but add a short
        # random suffix so job folders and final files are unique.
        stem = f"{model}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        job_dir = JOBS_DIR / f"temp_{stem}"
        return job_dir, stem

    # Allowed base directories for output
    _ALLOWED_BASES = (PROJECTS_OUTPUT.resolve(), OUTPUT_DIR.resolve())

    # Reject bare traversal components
    if ".." in save_path_input.split("/") or ".." in save_path_input.split("\\"):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    if "/" in save_path_input or "\\" in save_path_input:
        full_path = Path(save_path_input).expanduser().resolve()
        # Security: reject paths outside allowed directories
        if not any(full_path == base or _path_is_relative_to(full_path, base)
                   for base in _ALLOWED_BASES):
            raise HTTPException(
                status_code=400,
                detail=f"save_path must be under {PROJECTS_OUTPUT} or {OUTPUT_DIR}"
            )
        if full_path.suffix:
            stem = full_path.stem
            job_dir = _unique_save_job_dir(full_path.parent, stem)
        else:
            stem = full_path.name
            job_dir = _unique_save_job_dir(full_path.parent, stem)
    else:
        # Validate the bare name doesn't resolve outside PROJECTS_OUTPUT
        resolved = (PROJECTS_OUTPUT / save_path_input).resolve()
        if not (resolved == PROJECTS_OUTPUT.resolve()
                or _path_is_relative_to(resolved, PROJECTS_OUTPUT.resolve())):
            raise HTTPException(status_code=400, detail="Path traversal not allowed")
        if resolved.suffix:
            stem = resolved.stem
            job_dir = _unique_save_job_dir(resolved.parent, stem)
        else:
            stem = resolved.name
            job_dir = _unique_save_job_dir(resolved.parent, stem)

    if not any(_path_is_relative_to(job_dir, root) and job_dir != root for root in _ALLOWED_BASES):
        raise HTTPException(status_code=400, detail="save_path must name an output inside an allowed root")
    return job_dir, stem


# ============================================================
# Voice endpoint — unified dynamic route
# ============================================================

_XTTS_BUILTIN_VOICES = [
    "Aaron Dreschner", "Abrahan Mack", "Adde Michal", "Alexandra Hisakawa",
    "Alison Dietlinde", "Alma María", "Ana Florence", "Andrew Chipper",
    "Annmarie Nele", "Asya Anara", "Badr Odhiambo", "Baldur Sanjin",
    "Barbora MacLean", "Brenda Stern", "Camilla Holmström",
    "Chandra MacFarland", "Claribel Dervla", "Craig Gutsy",
    "Daisy Studious", "Damien Black", "Damjan Chapman",
    "Dionisio Schuyler", "Eugenio Mataracı", "Ferran Simen",
    "Filip Traverse", "Gilberto Mathias", "Gitta Nikolina", "Gracie Wise",
    "Henriette Usha", "Ige Behringer", "Ilkin Urbano",
    "Kazuhiko Atallah", "Kumar Dahl", "Lidiya Szekeres",
    "Lilya Stainthorpe", "Ludvig Milivoj", "Luis Moray", "Maja Ruoho",
    "Marcos Rudaski", "Narelle Moon", "Nova Hogarth", "Rosemary Okafor",
    "Royston Min", "Sofia Hellen", "Suad Qasim", "Szofi Granger",
    "Tammie Ema", "Tammy Grit", "Tanja Adelina", "Torcull Diarmuid",
    "Uta Obando", "Viktor Eka", "Viktor Menelaos", "Vjollca Johnnie",
    "Wulf Carlevaro", "Xavier Hayasaka", "Zacharie Aimilios",
    "Zofija Kendrick",
]


def _kokoro_voices() -> list[str]:
    """Scan Kokoro voice directory for .pt files."""
    voices_dir = Path(MODELS_DIR) / "kokoro" / "voices"
    if voices_dir.exists():
        return sorted(p.stem for p in voices_dir.glob("*.pt"))
    return ["af_heart"]


def _bark_voices() -> list[str]:
    """Return the official bundled v2 Bark presets.

    Setup installs the matching prompt tensors selectively from suno/bark.
    Keeping this deterministic lets clients discover valid choices before
    setup while /api/setup/status remains authoritative for readiness.
    """
    languages = (
        "en", "de", "es", "fr", "hi", "it", "ja",
        "ko", "pl", "pt", "ru", "tr", "zh",
    )
    return [
        f"v2/{language}_speaker_{speaker}"
        for language in languages
        for speaker in range(10)
    ]


_edge_voice_cache: tuple[float, list[str], list[dict]] | None = None


async def _edge_voices_live() -> tuple[list[str], list[dict], str]:
    """Return Edge's live catalog, bounded and cached, with static fallback."""
    global _edge_voice_cache
    now = time.monotonic()
    if _edge_voice_cache and now - _edge_voice_cache[0] < 6 * 60 * 60:
        return _edge_voice_cache[1], _edge_voice_cache[2], "live_cache"

    fallback = _VOICE_BUILDERS["edge"]()
    try:
        import edge_tts
        rows = await asyncio.wait_for(edge_tts.list_voices(), timeout=8.0)
        details = sorted(
            (
                {
                    "name": row.get("ShortName") or row.get("Name"),
                    "locale": row.get("Locale"),
                    "gender": row.get("Gender"),
                    "friendly_name": row.get("FriendlyName"),
                    "status": row.get("Status"),
                }
                for row in rows
                if row.get("ShortName") or row.get("Name")
            ),
            key=lambda item: (item.get("locale") or "", item.get("name") or ""),
        )
        voices = [item["name"] for item in details]
        if voices:
            _edge_voice_cache = (now, voices, details)
            return voices, details, "live"
    except Exception as exc:
        logger.warning("Edge live voice catalog unavailable; using fallback: %s", exc)
    return fallback, [], "fallback"


# Models with custom voice-list logic (all others use reference voices or empty)
_VOICE_BUILDERS: dict[str, callable] = {
    "xtts":     lambda: list(_XTTS_BUILTIN_VOICES),
    "kokoro":   _kokoro_voices,
    "bark":     _bark_voices,
    "qwen":     lambda: ["Chelsie", "Ethan"],
    "speecht5": lambda: ["female_1", "male_1", "female_2", "male_2", "female_3", "male_3", "neutral"],
    "parler":   lambda: [
        "A warm female voice, clear and close up, moderate speed",
        "A deep male voice, slightly slow, very clear recording",
        "Jon's voice is monotone yet slightly fast, very close recording",
        "Laura's voice is expressive and animated, moderate speed, high quality",
    ],
    "outetts":  lambda: ["EN-FEMALE-1-NEUTRAL"],
    "edge":     lambda: [
        "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural",
        "en-US-DavisNeural", "en-US-SaraNeural", "en-US-AndrewNeural",
        "en-GB-SoniaNeural", "en-GB-RyanNeural",
        "en-AU-NatashaNeural", "en-AU-WilliamNeural",
        "fr-FR-DeniseNeural", "fr-FR-HenriNeural",
        "de-DE-KatjaNeural", "de-DE-ConradNeural",
        "es-ES-ElviraNeural", "es-ES-AlvaroNeural",
        "ja-JP-NanamiNeural", "ja-JP-KeitaNeural",
        "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
        "ko-KR-SunHiNeural", "ko-KR-InJoonNeural",
        "pt-BR-FranciscaNeural", "it-IT-ElsaNeural",
    ],
    "voxtral":  lambda: [
        "casual_male", "casual_female", "cheerful_female",
        "neutral_male", "neutral_female", "ar_male",
        "de_male", "de_female", "es_male", "es_female",
        "fr_male", "fr_female", "hi_male", "hi_female",
        "it_male", "it_female", "nl_male", "nl_female",
        "pt_male", "pt_female",
    ],
    "csm":      lambda: ["speaker_0", "speaker_1", "speaker_2", "speaker_3"],
    "orpheus":  lambda: ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"],
}

# Per-model voice notes
_VOICE_NOTES: dict[str, str] = {
    "xtts":       "Built-in voices: set 'voice' to a speaker name and 'mode' to 'built-in'. For voice cloning: set 'voice' to a WAV file path and 'mode' to 'cloned' (default).",
    "kokoro":     "Prefix meanings: a=American, b=British, j=Japanese, z=Mandarin, e=Spanish, f=French, h=Hindi, i=Italian, p=Portuguese. f/m after prefix = female/male.",
    "bark":       "Bundled v2 presets cover en, de, es, fr, hi, it, ja, ko, pl, pt, ru, tr, zh with speakers 0-9. Default: v2/en_speaker_6. Voice is kept consistent across chunks via history prompt chaining.",
    "fish":       "Fish Speech works without a reference, or clones from a short, clean, dry WAV. For cloning, set 'voice' to an uploaded voice and 'reference_text' to its exact transcript. Prefer a 5-15 second single-speaker sample; long audiobook excerpts reduced intelligibility in the guarded probe.",
    "chatterbox": "Chatterbox uses reference audio for voice cloning. Upload WAV files via the Voices tab. Adjustable parameters: exaggeration (0-2, default 0.5), cfg_weight (0-1, default 0.5).",
    "f5":         "F5-TTS requires reference audio + transcript for voice cloning. Upload WAV files via the Voices tab and set 'reference_text' to the transcript of that audio.",
    "dia":        "Dia uses [S1] and [S2] speaker tags inline in text for dialogue. Upload reference audio via the Voices tab for voice cloning. Supports non-verbal expressions: (laughs), (sighs), (clears throat), etc.",
    "qwen":       "Two pinned built-in voices: Chelsie is warm and clear (default); Ethan is bright and energetic. Qwen Omni does not clone uploaded voices through this API.",
    "vibevoice":  "VibeVoice is designed for long-form natural turn-taking (not overlapping speech). Use 'Speaker N:' lines and pass one to four voice paths in JSON 'reference_audios' in speaker order; singular 'voice' or 'reference_audio' also works.",
    "higgs":      "Higgs works without a reference, accepts a natural-language scene/speaker description, or clones from reference audio plus its exact transcript. It is best for long-form and dialogue-style prosody; cold load is expensive, so batch related lines while the worker is warm.",
    "speecht5":   "Built-in speakers: female_1, male_1, female_2, male_2, female_3, male_3, neutral. Based on CMU-Arctic xvector embeddings. Output is 16kHz.",
    "parler":     "Describe the voice in natural language! The 'voice' field is a text description like 'A warm female voice, clear and close up, moderate speed'. Use speaker names (Jon, Laura, etc.) for consistent voices. Punctuation controls pacing.",
    "outetts":    "OuteTTS 1.0 supports 14 languages and one bundled voice: EN-FEMALE-1-NEUTRAL. For deterministic cloning, set 'voice' to a WAV file from the Voices tab and provide its exact 'reference_text'; use a clean clip no longer than 15 seconds. Generation is bounded to the local 0.6B HF model.",
    "vits":       "Fast single-speaker English (LJSpeech). No voice cloning — lightweight and quick. Model auto-downloads on first use via Coqui TTS.",
    "edge":       "Microsoft Edge neural voices (cloud, requires internet). The API returns the current live catalog (300+ voices at last verification) with a static fallback. Format: 'en-US-JennyNeural'. No GPU or model download needed.",
    "voxtral":    "Mistral Voxtral 4B TTS — set 'voice' to a preset name (e.g. 'casual_male') or to a WAV file from the Voices tab for cloning. Language is auto-detected from text (en, fr, es, de, it, pt, nl, ar, hi). 24kHz, runs on vLLM. CC BY-NC license: non-commercial use only.",
    "voxcpm2":    "OpenBMB VoxCPM2 — voice cloning from reference audio (set 'voice' to a WAV from the Voices tab; add 'reference_text' for ultimate cloning fidelity), or voice design by prepending a parenthesized description to your text, e.g. '(young woman, gentle)Hello there.'. 30 languages auto-detected. 48kHz output.",
    "csm":        "Sesame CSM-1B — conversational speech model. speaker_0 through speaker_3 are role IDs, not guaranteed fixed voice identities. It sounds best with context: set 'voice' to a reference WAV and provide its exact 'reference_text'. English is the supported language.",
    "orpheus":    "Orpheus 3B English finetune — built-in voices: tara, leah, jess, leo, dan, mia, zac, zoe. Inline non-spoken emotion controls: <laugh>, <sigh>, <chuckle>, <cough>, <sniffle>, <groan>, <yawn>, <gasp>. Use repetition_penalty >= 1.1 for stable speech.",
}

# Extra response fields for specific models
_VOICE_EXTRAS: dict[str, dict] = {
    "bark": {"default_voice": "v2/en_speaker_6", "example": "[laughs] That was fun!"},
    "dia": {"example": "[S1] Hello there! (laughs) [S2] Hey, how are you?"},
    "fish": {"example": "The silver river flows quietly."},
    "higgs": {"example": "The lantern glows beside us."},
    "qwen": {"default_voice": "Chelsie", "example": "The lantern glows beside us."},
    "vibevoice": {"example": "Speaker 1: Welcome.\nSpeaker 2: Glad to be here."},
    "voxtral": {"default_voice": "casual_male", "example": "The lantern glows beside us."},
    "voxcpm2": {"example": "(A warm, calm adult voice)The lantern glows beside us."},
    "csm": {"default_voice": "speaker_0", "example": "The lantern glows beside us."},
    "orpheus": {"default_voice": "tara", "example": "The lantern glows beside us. <chuckle>"},
}


@app.get("/api/tts/{model}/voices")
async def model_voices(model: str):
    setup = MODEL_SETUP.get(model)
    if not setup:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model}")

    # Get voice list: custom builder for models with built-in voices, or empty.
    # Reference voice files are served separately via /api/voices.
    voice_details: list[dict] = []
    voice_source = "static"
    if model == "edge":
        voices, voice_details, voice_source = await _edge_voices_live()
    elif model in _VOICE_BUILDERS:
        voices = _VOICE_BUILDERS[model]()
    else:
        voices = []

    result: dict = {"voices": voices}
    if model == "edge":
        result["catalog_source"] = voice_source
        result["voice_count"] = len(voices)
        if voice_details:
            result["voice_details"] = voice_details
    if model in _VOICE_NOTES:
        result["note"] = _VOICE_NOTES[model]
    if model in _VOICE_EXTRAS:
        result.update(_VOICE_EXTRAS[model])
    return result


# ============================================================
# Setup / Installation endpoints (for web UI)
# ============================================================

def _check_model_installed(model_id: str) -> dict:
    """Check if a model's packages and weights are installed."""
    info = MODEL_SETUP.get(model_id, {})
    override = MODEL_OVERRIDE_MAP.get(model_id)

    def _weights_ready(path: Path) -> bool:
        if not path.is_dir():
            return False
        required = info.get("required_weight_files", ()) if path.name == info.get("weights_dir") else ()
        if required:
            try:
                if not all((path / str(name)).is_file() and (path / str(name)).stat().st_size > 0
                           for name in required):
                    return False
            except OSError:
                return False
        else:
            try:
                manifest = json.loads((path / ".download_complete.json").read_text(encoding="utf-8"))
                files = manifest["files"]
                if not files or not all((path / name).is_file() and (path / name).stat().st_size == size
                                        for name, size in files.items()):
                    return False
            except (OSError, ValueError, KeyError):
                return False
        hf_download = path / ".cache" / "huggingface" / "download"
        if hf_download.exists():
            if any(hf_download.rglob("*.incomplete")):
                return False
        return True

    # Check packages — verify the actual Python package can be found
    pkgs_ok = False
    if override:
        override_dir = OVERRIDES_DIR / override
        pkgs_ok = (override_dir / ".install_complete").is_file() and (override_dir / ".requirements_hash").is_file()
    else:
        # Base-venv models: check if the key package is importable
        pkg_checks = {
            "kokoro": "kokoro",
            "dia": "dia",
            "fish": "fish_speech",
            "f5": "f5_tts",
            "qwen": "transformers",
            "whisper": "whisper",
            "edge": "edge_tts",
            "speecht5": "sentencepiece",
            "csm": "transformers",
        }
        pkg = pkg_checks.get(model_id)
        if pkg:
            py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
            base_sp = VENV_DIR / "lib" / py_ver / "site-packages"
            # Direct package directory (normal installs)
            pkgs_ok = (base_sp / pkg).exists() or (base_sp / f"{pkg}.py").exists()
            # Use targeted glob instead of iterating all of site-packages
            if not pkgs_ok and base_sp.exists():
                pkg_dash = pkg.replace("_", "-")
                pkgs_ok = (
                    bool(list(base_sp.glob(f"{pkg_dash}-*.dist-info"))) or
                    bool(list(base_sp.glob(f"{pkg}-*.dist-info")))
                )
            # Editable installs: check for .pth or .egg-link files by name
            if not pkgs_ok and base_sp.exists():
                pkg_dash = pkg.replace("_", "-")
                pkgs_ok = (
                    bool(list(base_sp.glob(f"__editable__.{pkg}*.pth"))) or
                    bool(list(base_sp.glob(f"__editable__.{pkg_dash}*.pth"))) or
                    (base_sp / f"{pkg_dash}.egg-link").exists() or
                    (base_sp / f"{pkg}.egg-link").exists()
                )
            # Last resort: check if cloned repo exists (for git-based installs)
            if not pkgs_ok and model_id == "fish":
                repo_dir = REPOS_DIR / "fish-speech" / "fish_speech"
                pkgs_ok = repo_dir.exists()
        else:
            pkgs_ok = False  # unknown model

    # Check weights
    weights_dir = info.get("weights_dir")
    weights_ok = False
    if weights_dir:
        wd = MODELS_DIR / weights_dir
        weights_ok = _weights_ready(wd)
        for extra_dir in info.get("extra_weights_dirs", ()):
            weights_ok = weights_ok and _weights_ready(MODELS_DIR / str(extra_dir))
    elif info.get("weights_repo") is None:
        # No weights_repo and no weights_dir = on-demand download model.
        # Don't claim weights are ready just because packages exist —
        # the actual weights download on first use and can be multi-GB.
        if model_id == "edge":
            weights_ok = pkgs_ok  # cloud service; there are no local weights
        elif model_id in ("outetts", "vits"):
            weights_ok = False
        else:
            weights_ok = False  # weights not yet downloaded

    if not pkgs_ok and not weights_ok:
        status = "not_installed"
    elif pkgs_ok and weights_ok:
        status = "ready"
    elif pkgs_ok and not weights_ok:
        status = "packages_only"  # packages installed, weights download on first use
    else:
        status = "partial"

    return {
        "model": model_id,
        "display": info.get("display", model_id),
        "packages_installed": pkgs_ok,
        "weights_downloaded": weights_ok,
        "weights_on_demand": model_id == "vits",
        "status": status,
    }


@app.get("/api/setup/status")
async def setup_status():
    """Check installation status of all models."""
    statuses = {}
    with _active_installs_lock:
        installing = set(_active_installs.keys())
        install_details = {
            name: dict(meta)
            for name, meta in _active_install_meta.items()
        }
        active_install_details = {
            name: dict(meta)
            for name, meta in _active_install_meta.items()
            if name in installing
        }
        recent_install_details = {
            name: dict(meta)
            for name, meta in _active_install_meta.items()
            if name not in installing
        }
    active_models = sorted(m for m in installing if m != "all")
    # _check_model_installed does many blocking stat/iterdir/glob calls per model
    # against site-packages/MODELS_DIR (slow on WSL-mounted paths). Offload the
    # whole ~22-model scan to a thread so it doesn't stall the event loop (which
    # also serves /health, job polling, and SSE keepalives).
    scanned = await asyncio.to_thread(
        lambda: {m: _check_model_installed(m) for m in MODEL_SETUP}
    )
    for model_id in MODEL_SETUP:
        info = scanned[model_id]
        if model_id in installing:
            info["status"] = "installing"
            if model_id in install_details:
                info["install"] = install_details[model_id]
        statuses[model_id] = info
    return {
        "models": statuses,
        "active_installs": {
            "all": "all" in installing,
            "models": active_models,
            "count": len(active_models),
            "details": active_install_details,
            "recent": recent_install_details,
        },
    }


_HF_TOKEN_FILE = HF_TOKEN_FILE  # alias for local use


class HFTokenRequest(BaseModel):
    token: str


@app.get("/api/setup/hf-token")
async def get_hf_token():
    """Check if a HuggingFace token is saved."""
    def _read() -> str | None:
        if _HF_TOKEN_FILE.exists():
            return _HF_TOKEN_FILE.read_text().strip()
        return None
    raw = await asyncio.to_thread(_read)
    if raw is not None:
        masked = raw[:4] + "..." + raw[-4:] if len(raw) > 8 else "***"
        return {"saved": True, "masked": masked}
    return {"saved": False, "masked": None}


@app.post("/api/setup/hf-token")
async def set_hf_token(req: HFTokenRequest):
    """Save a HuggingFace token for gated model downloads."""
    token = req.token.strip()
    if not token:
        def _remove():
            for token_file in (_HF_TOKEN_FILE, LEGACY_HF_TOKEN_FILE):
                if token_file.exists():
                    token_file.unlink()
        await asyncio.to_thread(_remove)
        return {"status": "removed"}

    def _write():
        _HF_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HF_TOKEN_FILE.write_text(token)
        _HF_TOKEN_FILE.chmod(0o600)  # readable only by owner
    await asyncio.to_thread(_write)
    return {"status": "saved", "masked": token[:4] + "..." + token[-4:]}


def _run_install_background(model: str) -> bool:
    """Run install_model.sh in background, streaming output to logger."""
    import subprocess as sp
    script = str(BASE_DIR / "install_model.sh")
    timeout_sec = _setup_install_timeout_sec(model)

    logger.info("=== Installing %s ===", model)
    # _active_installs[model] is already set by the caller before thread start
    with _active_installs_lock:
        # A retry/reinstall is a new run.  Do not leak terminal fields such as
        # finished_at, returncode, last_error, or the prior run's started_at
        # into the active status response.
        meta = {
            "model": model,
            "started_at": time.time(),
            "timeout_sec": timeout_sec,
            "status": "starting",
        }
        _active_install_meta[model] = meta

    # Keep setup work from monopolizing the host. Affinity is inherited by
    # pip/build/download children, while thread env vars cover native pools.
    cpu_count = max(1, os.cpu_count() or 1)
    install_cpu_count = max(1, min(
        cpu_count,
        int(os.environ.get("TTS_INSTALL_CPU_COUNT", "1")),
    ))
    cpu_set = f"0-{install_cpu_count - 1}" if install_cpu_count > 1 else "0"
    command = ["bash", script, model]
    if shutil.which("taskset"):
        command = ["taskset", "-c", cpu_set, *command]
    if shutil.which("nice"):
        command = ["nice", "-n", "10", *command]
    install_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": str(install_cpu_count),
        "MKL_NUM_THREADS": str(install_cpu_count),
        "OPENBLAS_NUM_THREADS": str(install_cpu_count),
        "NUMEXPR_NUM_THREADS": str(install_cpu_count),
        "MAX_JOBS": str(install_cpu_count),
        "CMAKE_BUILD_PARALLEL_LEVEL": str(install_cpu_count),
        "CARGO_BUILD_JOBS": str(install_cpu_count),
        "RAYON_NUM_THREADS": str(install_cpu_count),
        "MAKEFLAGS": f"-j{install_cpu_count}",
        "TOKENIZERS_PARALLELISM": "false",
        # hf-xet can create its own wide native pool despite Python/taskset
        # limits and has saturated the host during large snapshots. Prefer the
        # regular HTTP backend with one resumable download worker.
        "HF_HUB_DISABLE_XET": "1",
        "TTS_DOWNLOAD_WORKERS": "1",
    }

    proc = None
    ok = False
    try:
        with _active_installs_lock:
            cancelled_before_start = _install_cancel_all.is_set() or model in _install_cancel_models
        if cancelled_before_start:
            with _active_installs_lock:
                _active_install_meta[model]["status"] = "cancelled"
            return False
        proc = sp.Popen(
            command,
            stdout=sp.PIPE, stderr=sp.STDOUT,
            text=True, bufsize=1,
            env=install_env,
            start_new_session=True,
        )
        with _active_installs_lock:
            _active_install_processes[model] = proc
            meta = _active_install_meta.setdefault(model, {})
            meta.update({
                "pid": proc.pid,
                "status": "running",
                "cpu_set": cpu_set,
                "nice": 10,
                "last_update_at": time.time(),
            })

        lines: queue_mod.Queue[str | None] = queue_mod.Queue()

        def _reader():
            try:
                assert proc is not None and proc.stdout is not None
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        deadline = time.monotonic() + timeout_sec
        reader_done = False
        while True:
            try:
                line = lines.get(timeout=0.5)
                if line is None:
                    reader_done = True
                else:
                    line = line.rstrip()
                    if line:
                        logger.info("[install:%s] %s", model, line)
                        with _active_installs_lock:
                            meta = _active_install_meta.setdefault(model, {})
                            meta["last_line"] = line[-500:]
                            meta["last_update_at"] = time.time()
            except queue_mod.Empty:
                pass

            if proc.poll() is not None and reader_done:
                break

            if time.monotonic() > deadline:
                raise sp.TimeoutExpired(proc.args, timeout_sec)

        # Drain any final queued lines.
        while True:
            try:
                line = lines.get_nowait()
            except queue_mod.Empty:
                break
            if line:
                line = line.rstrip()
                if line:
                    logger.info("[install:%s] %s", model, line)
                    with _active_installs_lock:
                        meta = _active_install_meta.setdefault(model, {})
                        meta["last_line"] = line[-500:]
                        meta["last_update_at"] = time.time()

        if proc.returncode == 0:
            logger.info("=== %s installed successfully ===", model)
            ok = True
        else:
            with _active_installs_lock:
                cancelled = _install_cancel_all.is_set() or model in _install_cancel_models
            if cancelled:
                logger.warning("=== %s install CANCELLED (exit %d) ===", model, proc.returncode)
                with _active_installs_lock:
                    _active_install_meta.setdefault(model, {})["status"] = "cancelled"
            else:
                logger.error("=== %s install FAILED (exit %d) ===", model, proc.returncode)
                with _active_installs_lock:
                    meta = _active_install_meta.setdefault(model, {})
                    meta["status"] = "failed"
                    meta["returncode"] = proc.returncode

    except sp.TimeoutExpired:
        logger.error("=== %s install TIMED OUT (%d min) ===", model, max(1, timeout_sec // 60))
        with _active_installs_lock:
            meta = _active_install_meta.setdefault(model, {})
            meta["status"] = "timeout"
            meta["last_error"] = f"timed out after {timeout_sec} seconds"
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError, AttributeError):
                proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
    except Exception as e:
        logger.error("=== %s install ERROR: %s ===", model, e)
        with _active_installs_lock:
            meta = _active_install_meta.setdefault(model, {})
            meta["status"] = "error"
            meta["last_error"] = str(e)
    finally:
        # Reap the child on every exit path (incl. unexpected exceptions), so a
        # non-TimeoutExpired error can't leave the install_model.sh process tree
        # (pip/HF downloads) running orphaned. Idempotent with the TimeoutExpired
        # branch (poll() guards re-kill). Also close stdout to unblock the reader
        # thread and release the pipe FD.
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError, AttributeError):
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass

        # Pip wheels/build trees are disposable once the install process has
        # exited. Clear them on success, failure, or cancellation so several
        # sequential engine installs cannot strand many GiB in the distro.
        cache_targets = {"pip": CACHE_DIR / "pip"}
        cache_freed: dict[str, int] = {}
        for cache_name, cache_path in cache_targets.items():
            cache_before = _du_path(cache_path)["bytes"]
            try:
                if cache_path.exists():
                    for child in cache_path.iterdir():
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not fully clear install %s cache: %s",
                               cache_name, exc)
            cache_after = _du_path(cache_path)["bytes"]
            cache_freed[cache_name] = max(0, cache_before - cache_after)
        reclaim = worker_manager.reclaim_model_file_cache(model)
        with _active_installs_lock:
            meta = _active_install_meta.setdefault(model, {})
            meta["install_cache_freed"] = cache_freed
            meta["memory_reclaim"] = reclaim
            if ok:
                meta["status"] = "completed"
            meta["finished_at"] = time.time()
            _active_installs.pop(model, None)
            if _active_install_processes.get(model) is proc:
                _active_install_processes.pop(model, None)
    return ok


def _run_install_all_background():
    """Install all models sequentially in one background thread."""
    failures = []
    try:
        with _active_installs_lock:
            _active_install_meta["all"] = {
                "model": "all",
                "started_at": time.time(),
                "status": "running",
                "last_update_at": time.time(),
            }
        for m in MODEL_SETUP:
            if _install_cancel_all.is_set():
                logger.warning("[install:all] Cancel requested; stopping before %s", m)
                break
            with _active_installs_lock:
                meta = _active_install_meta.setdefault("all", {})
                meta["current_model"] = m
                meta["last_update_at"] = time.time()
            status = _check_model_installed(m).get("status")
            if status == "ready":
                logger.info("[install:all] Skipping %s; already ready", m)
                continue
            with _active_installs_lock:
                if _install_cancel_all.is_set():
                    logger.warning("[install:all] Cancel requested; stopping before %s", m)
                    break
                if m in _active_installs:
                    continue
                _active_installs[m] = True
            ok = False
            for attempt in range(1, _SETUP_INSTALL_ATTEMPTS + 1):
                with _active_installs_lock:
                    if m in _install_cancel_models:
                        break
                ok = _run_install_background(m)
                if ok or _install_cancel_all.is_set() or m in _install_cancel_models:
                    break
                if attempt < _SETUP_INSTALL_ATTEMPTS:
                    logger.warning(
                        "[install:all] %s failed on attempt %d/%d; retrying in 30 seconds",
                        m, attempt, _SETUP_INSTALL_ATTEMPTS,
                    )
                    _install_cancel_all.wait(30)
                    with _active_installs_lock:
                        _active_installs[m] = True
            if not ok:
                failures.append(m)
            if not ok and not _install_cancel_all.is_set():
                logger.warning("[install:all] Continuing after failed setup for %s", m)
    finally:
        with _active_installs_lock:
            meta = _active_install_meta.setdefault("all", {})
            meta["status"] = "cancelled" if _install_cancel_all.is_set() else ("failed" if failures else "completed")
            meta["failed_models"] = failures
            meta["finished_at"] = time.time()
            _active_installs.pop("all", None)
            _install_cancel_all.clear()


def _terminate_install_process(proc: Any) -> str:
    """Terminate one active installer subprocess tree."""
    if proc is None:
        return "missing"
    try:
        if proc.poll() is not None:
            return f"already_exited:{proc.returncode}"
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        signal_name = "sigterm"
    except (OSError, ProcessLookupError, AttributeError):
        try:
            proc.terminate()
            signal_name = "terminate"
        except Exception as exc:
            return f"terminate_failed:{exc}"
    try:
        proc.wait(timeout=10)
        return f"{signal_name}:exited:{proc.returncode}"
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        signal_name = "sigkill"
    except (OSError, ProcessLookupError, AttributeError):
        try:
            proc.kill()
            signal_name = "kill"
        except Exception as exc:
            return f"kill_failed:{exc}"
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    return f"{signal_name}:exited:{getattr(proc, 'returncode', None)}"


@app.post("/api/setup/cancel/{model}")
async def cancel_setup_install(model: str):
    """Cancel active setup installs without guessing host processes."""
    if model != "all" and model not in MODEL_SETUP:
        raise HTTPException(404, f"Unknown model: {model}")
    with _active_installs_lock:
        if model == "all":
            _install_cancel_all.set()
            targets = dict(_active_install_processes)
        else:
            _install_cancel_models.add(model)
            targets = {
                model: proc for name, proc in _active_install_processes.items()
                if name == model
            }
        active = sorted(_active_installs.keys())
    if not targets:
        return {"status": "cancel_requested" if active else "not_running", "model": model, "active": active}
    stopped = {name: _terminate_install_process(proc) for name, proc in targets.items()}
    return {"status": "cancel_requested", "model": model, "stopped": stopped, "active_before": active}


@app.post("/api/setup/install/{model}")
async def install_model_endpoint(model: str):
    """Install a model's packages and download weights (runs in background)."""
    if model == "all":
        with _active_installs_lock:
            if _active_installs:
                return {"status": "already_installing",
                        "models": list(_active_installs.keys())}
            _install_cancel_all.clear()
            _install_cancel_models.clear()
            _active_installs["all"] = True
        t = threading.Thread(target=_run_install_all_background, daemon=True)
        t.start()
        return {"status": "installing_all",
                "message": "Installing all models sequentially. Check the Log tab."}

    if model not in MODEL_SETUP:
        raise HTTPException(404, f"Unknown model: {model}")

    with _active_installs_lock:
        if model in _active_installs:
            return {"model": model, "status": "already_installing"}
        if len(_active_installs) >= 1:
            return {"model": model, "status": "busy",
                    "message": f"Already installing {len(_active_installs)} model(s). Wait for them to finish."}
        # Mark as installing atomically with the check to prevent TOCTOU race
        _install_cancel_models.discard(model)
        _active_installs[model] = True

    # Kill any running workers so they don't keep using stale packages
    for w in registry.workers_for_model(model):
        logger.info("Killing worker %s before reinstalling %s", w.worker_id, model)
        await worker_manager.kill_worker(w.worker_id)

    t = threading.Thread(target=_run_install_background, args=(model,), daemon=True)
    t.start()

    return {"model": model, "status": "installing",
            "message": "Installation started. Check the Log tab for progress."}


@app.delete("/api/setup/{model}")
async def remove_model(model: str):
    """Remove a model's weights and override directory."""
    global _maintenance_busy
    if (_active_api_requests > 1 or _active_exports or _active_installs
            or any(_running_jobs.values())
            or any(w.status in ("busy", "starting", "loading") for w in registry.all_workers())
            or any(not task.done() for task in _model_load_tasks.values())):
        raise HTTPException(409, "Wait for active requests, jobs, model loads and installations before removing a model")
    _maintenance_busy = True
    try:
        return await _remove_model_files(model)
    finally:
        _maintenance_busy = False


async def _remove_model_files(model: str):
    info = MODEL_SETUP.get(model)
    if not info:
        raise HTTPException(404, f"Unknown model: {model}")

    # Kill any running workers for this model first
    for w in registry.workers_for_model(model):
        logger.info("Killing stale worker %s before removing %s", w.worker_id, model)
        await worker_manager.kill_worker(w.worker_id)

    removed = []
    retained = []
    # Remove weights
    weights_dir = info.get("weights_dir")
    if weights_dir:
        wd = MODELS_DIR / weights_dir
        if wd.exists():
            await asyncio.to_thread(shutil.rmtree, wd)
            removed.append(f"weights: {wd}")
    for extra_dir in info.get("extra_weights_dirs", ()):
        extra_path = MODELS_DIR / str(extra_dir)
        if extra_path.exists():
            await asyncio.to_thread(shutil.rmtree, extra_path)
            removed.append(f"weights: {extra_path}")

    # Remove override
    override = MODEL_OVERRIDE_MAP.get(model)
    if override:
        od = OVERRIDES_DIR / override
        if od.exists():
            shared_with = [
                sibling for sibling, sibling_override in MODEL_OVERRIDE_MAP.items()
                if sibling != model and sibling_override == override
            ]
            retain_for: list[str] = []
            for sibling in shared_with:
                # A live sibling worker always owns the shared runtime. For
                # weight-backed siblings, installed weights also mean removing
                # the package layer would turn a valid model into a partial one.
                if registry.workers_for_model(sibling):
                    retain_for.append(sibling)
                    continue
                sibling_info = MODEL_SETUP.get(sibling, {})
                if sibling_info.get("weights_dir"):
                    sibling_status = _check_model_installed(sibling).get("status")
                    if sibling_status in ("ready", "partial"):
                        retain_for.append(sibling)
            if retain_for:
                retained.append({
                    "path": str(od),
                    "reason": "shared_override_in_use",
                    "models": sorted(retain_for),
                })
            else:
                await asyncio.to_thread(shutil.rmtree, od)
                removed.append(f"override: {od}")

    return {"model": model, "removed": removed, "retained": retained}


# ============================================================
# Configuration endpoint (for web UI)
# ============================================================

@app.get("/api/config")
async def get_config():
    """Return server configuration for the web UI."""
    return {
        "models": MODEL_SETUP,
        "defaults": MODEL_DEFAULTS,
        "fields": MODEL_FIELDS,
        "params": PARAM_CONFIG,
        "param_overrides": PARAM_OVERRIDES,
        "tooltips": TOOLTIPS,
        "profiles": PROFILES,
        "override_map": MODEL_OVERRIDE_MAP,
        "gateway_port": _GATEWAY_PORT,
        "bridge_port": _BRIDGE_PORT,
        "models_dir": str(MODELS_DIR),
        "voices_dir": str(VOICE_DIR),
        "output_dir": str(OUTPUT_DIR),
        "venv_dir": str(VENV_DIR),
    }


# ============================================================
# Voice management
# ============================================================

_VOICE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def _voice_transcript_path(path: Path, *, for_write=False) -> Path:
    current = path.with_name(path.name + ".txt")
    if current.exists() or for_write:
        return current
    legacy = path.with_suffix(".txt")
    siblings = [p for p in path.parent.glob(path.stem + ".*")
                if p.suffix.lower() in _AUDIO_EXTS]
    return legacy if len(siblings) == 1 and legacy.exists() else current


@app.get("/api/voices")
async def list_voices():
    """List reference audio files in the voices directory."""
    voices = []
    if VOICE_DIR.exists():
        for f in sorted(VOICE_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in _VOICE_AUDIO_EXTS:
                # Check for sidecar transcription file
                txt_path = _voice_transcript_path(f)
                transcription = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else None
                voices.append({
                    "name": f.stem,
                    "filename": f.name,
                    "path": str(f),
                    "format": f.suffix[1:],
                    "size": f.stat().st_size,
                    "transcription": transcription,
                })
    return {"voices": voices}


@app.post("/api/voices/upload")
async def upload_voice(file: UploadFile):
    """Upload a reference audio file."""
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize filename: strip path components, reject traversal attempts
    safe_name = Path(file.filename or "").name  # strip directory components
    if not safe_name or safe_name.startswith(".") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Validate audio extension
    if Path(safe_name).suffix.lower() not in _VOICE_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Allowed: {', '.join(_VOICE_AUDIO_EXTS)}")
    dest = VOICE_DIR / safe_name
    # Ensure dest is actually inside VOICE_DIR (belt and suspenders)
    if not dest.resolve().is_relative_to(VOICE_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Read with size limit (50MB — voice reference clips shouldn't be larger)
    _MAX_VOICE_SIZE = 50 * 1024 * 1024
    content = await file.read(_MAX_VOICE_SIZE + 1)
    if len(content) > _MAX_VOICE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_VOICE_SIZE // (1024*1024)}MB)")
    staging = dest.with_name(f".upload_{uuid.uuid4().hex}{dest.suffix}")
    try:
        staging.write_bytes(content)
        from audio_io import read_audio
        data, _ = await asyncio.to_thread(read_audio, staging, max_seconds=1)
        if not len(data):
            raise ValueError("Empty audio")
        old_transcript = _voice_transcript_path(dest)
        staging.replace(dest)
        old_transcript.unlink(missing_ok=True)
        _voice_transcript_path(dest, for_write=True).unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid voice audio: {exc}") from exc
    finally:
        staging.unlink(missing_ok=True)
    return {"name": safe_name, "path": str(dest), "size": len(content)}


def _safe_voice_path(filename: str) -> Path:
    """Resolve and validate a voice filename. Raises HTTPException on bad input."""
    safe = Path(filename).name
    path = VOICE_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Voice file not found")
    if not path.resolve().is_relative_to(VOICE_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return path


@app.get("/api/voices/{filename}/audio")
async def get_voice_audio(filename: str):
    """Stream a voice audio file for playback."""
    path = _safe_voice_path(filename)
    mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
    return FileResponse(str(path), media_type=mime_map.get(path.suffix.lower(), "audio/wav"))


@app.delete("/api/voices/{filename}")
async def delete_voice(filename: str):
    """Delete a single voice file and its transcription sidecar."""
    path = _safe_voice_path(filename)
    txt = _voice_transcript_path(path)
    path.unlink()
    if txt.exists():
        txt.unlink()
    return {"deleted": filename}


@app.post("/api/voices/delete")
async def delete_voices_bulk(filenames: list[str]):
    """Bulk-delete voice files."""
    deleted = []
    for fn in filenames:
        safe = Path(fn).name
        path = VOICE_DIR / safe
        if path.exists() and path.resolve().is_relative_to(VOICE_DIR.resolve()):
            txt = _voice_transcript_path(path)
            path.unlink()
            if txt.exists():
                txt.unlink()
            deleted.append(fn)
    return {"deleted": deleted}


class TranscribeRequestBody(BaseModel):
    """Generic transcribe request body. Either ``source`` (job/path descriptor)
    must be set."""
    source: dict
    size: str = "base"
    word_timestamps: bool = False
    language: Optional[str] = None
    task: str = "transcribe"


@app.post("/api/transcribe")
async def transcribe_generic(req: TranscribeRequestBody):
    """Transcribe any audio file already known to the TTS server.

    Source descriptor matches the audio editor's ``_resolve_audio_source``:
      {kind: "final" | "chunk" | "edit", job_id, [index|name]}
      {kind: "path", path: "<absolute audio path under VOICE_DIR/OUTPUT_DIR/PROJECTS_OUTPUT>"}

    For ad-hoc files not under the safe roots, use POST /api/transcribe/upload.
    """
    path = _resolve_audio_source(req.source)
    worker = await _ensure_whisper_worker_ready()
    # Mark the worker busy for the full transcription so the health loop's
    # busy special-case (poll-only) applies and it won't be killed mid-job.
    registry.mark_busy(worker.worker_id, "transcribe")
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/transcribe",
                json={
                    "audio_path": str(path),
                    "size": req.size,
                    "word_timestamps": bool(req.word_timestamps),
                    "language": req.language,
                    "task": req.task,
                },
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Whisper worker call failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        registry.mark_ready(worker.worker_id)


@app.post("/api/transcribe/upload")
async def transcribe_upload(
    file: UploadFile,
    size: str = Form("base"),
    word_timestamps: bool = Form(False),
    language: Optional[str] = Form(None),
    task: str = Form("transcribe"),
):
    """Transcribe an uploaded audio file. The file is staged into OUTPUT_DIR
    for the worker to read, then deleted after the response is built."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")
    ext = (Path(file.filename).suffix or ".wav").lower()
    if ext not in _AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {ext}")

    tmp_path = OUTPUT_DIR / f"transcribe_{uuid.uuid4().hex}{ext}"
    content = await file.read(_MAX_REFERENCE_AUDIO_BYTES + 1)
    if len(content) > _MAX_REFERENCE_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio too large (max {_MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)}MB)",
        )
    tmp_path.write_bytes(content)

    worker = None
    try:
        worker = await _ensure_whisper_worker_ready()
        # Mark busy for the full transcription so the health loop won't kill it.
        registry.mark_busy(worker.worker_id, "transcribe")
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/transcribe",
                json={
                    "audio_path": str(tmp_path),
                    "size": size,
                    "word_timestamps": bool(word_timestamps),
                    "language": language,
                    "task": task,
                },
            )
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Whisper worker call failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        if worker is not None:
            registry.mark_ready(worker.worker_id)
        tmp_path.unlink(missing_ok=True)


@app.post("/api/voices/{filename}/transcribe")
async def transcribe_voice(filename: str, size: str = "base",
                           word_timestamps: bool = False,
                           language: Optional[str] = None,
                           task: str = "transcribe"):
    """Transcribe a voice file using Whisper and cache the result.

    Query params:
        size:             whisper size (tiny/base/small/medium/large)
        word_timestamps:  return per-word timings in addition to plain text
        language:         ISO 639-1 code to force a language (omit for auto)
        task:             'transcribe' (default) or 'translate' (to English)
    """
    path = _safe_voice_path(filename)
    original = path.stat()
    worker = await _ensure_whisper_worker_ready()
    # Mark busy for the full transcription so the health loop won't kill it.
    registry.mark_busy(worker.worker_id, "transcribe")

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/transcribe",
                json={
                    "audio_path": str(path),
                    "size": size,
                    "word_timestamps": bool(word_timestamps),
                    "language": language,
                    "task": task,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        registry.mark_ready(worker.worker_id)

    # A replacement or rename while Whisper was reading must not receive the
    # transcript for an earlier clip.
    try:
        current = path.stat()
    except FileNotFoundError:
        raise HTTPException(409, "Voice changed during transcription; transcribe it again")
    if (current.st_ino, current.st_size, current.st_mtime_ns) != (
            original.st_ino, original.st_size, original.st_mtime_ns):
        raise HTTPException(409, "Voice changed during transcription; transcribe it again")
    # Cache transcription as sidecar .txt file (plain text only — no timings)
    text = result.get("text", "").strip()
    _voice_transcript_path(path, for_write=True).write_text(text, encoding="utf-8")

    out = {"filename": filename, "text": text, "language": result.get("language", "")}
    if word_timestamps and "words" in result:
        out["words"] = result["words"]
    return out


# ============================================================
# Log streaming (SSE)
# ============================================================

_LOG_QUEUE_MAX = 500  # max buffered entries per subscriber — oldest dropped on overflow
_log_subscribers: list[queue_mod.Queue] = []


class SSELogHandler(logging.Handler):
    """Pushes log records to all SSE subscribers (thread-safe)."""

    _emit_count = 0

    def emit(self, record):
        entry = {
            "timestamp": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        dead = []
        with _log_subscribers_lock:
            subscribers = _log_subscribers[:]
        for q in subscribers:
            try:
                q.put_nowait(entry)
            except queue_mod.Full:
                # Bounded queue full — drop oldest entry and retry
                try:
                    q.get_nowait()
                    q.put_nowait(entry)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        # Prune dead queues periodically (every 100 emits or when dead found)
        self._emit_count += 1
        if dead or self._emit_count >= 100:
            self._emit_count = 0
            with _log_subscribers_lock:
                for q in dead:
                    try:
                        _log_subscribers.remove(q)
                    except ValueError:
                        pass


# Attach the SSE handler to root logger
_sse_handler = SSELogHandler()
_sse_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_sse_handler)


@app.get("/api/logs/stream")
async def log_stream():
    """Server-Sent Events stream for real-time logs."""
    from starlette.responses import StreamingResponse

    q: queue_mod.Queue = queue_mod.Queue(maxsize=_LOG_QUEUE_MAX)
    with _log_subscribers_lock:
        _log_subscribers.append(q)

    async def event_generator():
        try:
            while True:
                try:
                    entry = await asyncio.to_thread(q.get, timeout=30)
                except Exception:
                    # Timeout -- send SSE keepalive comment
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _log_subscribers_lock:
                try:
                    _log_subscribers.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Discovery / version / about
# ============================================================
import platform
import zipfile as _zipfile
import datetime as _dt

# Captured at import time so /api/about can report uptime.
_SERVER_STARTED_AT = time.time()

# Persistent log file written by the bridge / setup. Used by /api/logs/tail
# so callers don't have to keep an SSE stream open just to scroll history.
_PERSISTENT_LOG_FILES = {
    "server": RUN_DIR / "tts_server_output.log",
    "bridge": RUN_DIR / "tts_server_debug.log",
    "setup": RUN_DIR / "tts_server_setup.log",
    "startup": RUN_DIR / "tts_server_startup.log",
}


def _git_describe() -> str | None:
    """Best-effort short git commit, only if a .git directory exists at the
    app root. Used purely for diagnostics — never required."""
    try:
        import subprocess as _sp
        r = _sp.run(["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(APP_DIR), capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _detect_binary(name: str) -> dict:
    """Return {present, version} for a system binary used by the pipeline."""
    try:
        import subprocess as _sp
        path = shutil.which(name)
        if not path:
            return {"present": False, "path": None, "version": None}
        # ffmpeg / espeak-ng / sox all support --version on stdout / stderr
        r = _sp.run([path, "--version"], capture_output=True, text=True, timeout=5)
        ver = (r.stdout or r.stderr or "").splitlines()[:1]
        return {"present": True, "path": path,
                "version": ver[0].strip() if ver else None}
    except Exception as e:
        return {"present": False, "path": None, "version": None, "error": str(e)}


def _gpu_summary() -> list[dict]:
    """Snapshot from nvidia-smi if available — same call as /api/devices but
    without going through worker_manager's device cache."""
    try:
        import subprocess as _sp
        r = _sp.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                out.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "vram_total_mb": int(parts[2] or 0),
                    "vram_used_mb": int(parts[3] or 0),
                    "vram_free_mb": int(parts[4] or 0),
                    "utilization_pct": int(parts[5] or 0),
                    "driver": parts[6],
                })
        return out
    except Exception:
        return []


@app.get("/api/version")
async def version():
    """Return version metadata for callers that just need a quick check."""
    git = await asyncio.to_thread(_git_describe)
    return {
        "app": "tts_server",
        "version": APP_VERSION,
        "api_version": "1.0",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": git,
    }


@app.get("/api/about")
async def about():
    """One-stop summary: paths, ports, uptime, GPU, version, model count.
    Designed for an external app to render a status page or onboarding screen."""
    # Both subprocess calls are blocking — keep them off the event loop.
    git, gpu = await asyncio.gather(
        asyncio.to_thread(_git_describe),
        asyncio.to_thread(_gpu_summary),
    )
    workers = registry.all_workers()
    return {
        "version": APP_VERSION,
        "started_at": _dt.datetime.fromtimestamp(_SERVER_STARTED_AT).isoformat(),
        "uptime_sec": int(time.time() - _SERVER_STARTED_AT),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "host": platform.node(),
        "git_commit": git,
        "ports": {"gateway": _GATEWAY_PORT, "bridge": _BRIDGE_PORT},
        "paths": {
            "app_dir": str(APP_DIR),
            "models_dir": str(MODELS_DIR),
            "voices_dir": str(VOICE_DIR),
            "output_dir": str(OUTPUT_DIR),
            "projects_dir": str(PROJECTS_OUTPUT),
            "venv_dir": str(VENV_DIR),
            "overrides_dir": str(OVERRIDES_DIR),
            "repos_dir": str(REPOS_DIR),
            "cache_dir": str(CACHE_DIR),
            "secrets_dir": str(SECRETS_DIR),
            "run_dir": str(RUN_DIR),
        },
        "gpu": gpu,
        "workers": {
            "count": len(workers),
            "by_status": {
                k: sum(1 for w in workers if w.status == k)
                for k in ("starting", "loading", "ready", "busy", "dead")
            },
            "by_model": {
                m: sum(1 for w in workers if w.model == m)
                for m in {w.model for w in workers}
            },
        },
        "models": {
            "registered": len(MODEL_SETUP),
            "loaded": sorted({w.model for w in workers if w.status in ("ready", "busy")}),
        },
    }


@app.get("/api/capabilities")
async def capabilities():
    """Lightweight machine-readable contract for peer apps and agents."""
    workers = registry.all_workers()
    loaded = sorted({w.model for w in workers if w.status in ("ready", "busy")})
    return {
        "app": "tts_server",
        "title": "TTS Server",
        "version": APP_VERSION,
        "capabilities": [
            "text_to_speech",
            "voice_cloning",
            "speech_to_text",
            "audio_editing",
            "srt_generation",
            "voice_library",
        ],
        "status_endpoints": [
            "GET /health",
            "GET /api/live",
            "GET /api/capabilities",
            "GET /api/models/status",
            "GET /api/workers",
            "GET /api/jobs/{job_id}",
        ],
        "model_endpoints": [
            "GET /api/models",
            "POST /api/workers/spawn",
            "POST /api/models/{model}/load",
            "POST /api/models/{model}/unload",
        ],
        "generation_endpoints": [
            "POST /api/tts/{model}/submit",
            "GET /api/jobs/{job_id}",
            "GET /api/jobs/{job_id}/output",
            "GET /api/jobs/{job_id}/manifest",
            "POST /api/transcribe",
        ],
        "destructive_endpoints": [
            "DELETE /api/workers/{worker_id}",
            "POST /api/models/{model}/unload",
            "POST /api/maintenance/restart-workers",
            "POST /api/shutdown",
        ],
        "artifact_roots": {
            "output": str(OUTPUT_DIR),
            "projects": str(PROJECTS_OUTPUT),
            "voices": str(VOICE_DIR),
            "models": str(MODELS_DIR),
            "cache": str(CACHE_DIR),
        },
        "models": {
            "registered": sorted(MODEL_SETUP.keys()),
            "loaded": loaded,
        },
        "loaded_resources": {
            "workers": registry.to_dict_list(),
        },
        "policy": {
            "prefer_async_submit_for_long_text": True,
            "reuse_ready_workers_for_batches": True,
            "unload_workers_after_final_artifacts": True,
        },
    }


# ============================================================
# Diagnostics / disk / env
# ============================================================
def _du_path(path: Path) -> dict:
    """Recursive size + file count for one directory; tolerant of missing dirs."""
    total = 0
    files = 0
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": 0, "files": 0}
    try:
        for root, _dirs, fs in os.walk(path, followlinks=False):
            for f in fs:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                    files += 1
                except OSError:
                    continue
    except OSError as e:
        return {"path": str(path), "exists": True, "bytes": total,
                "files": files, "error": str(e)}
    return {"path": str(path), "exists": True, "bytes": total, "files": files}


@app.get("/api/disk")
async def disk_usage():
    """Per-directory size + free space on the underlying mount.

    Heavy for first call on a populated MODELS_DIR (walks the whole tree),
    so callers should poll sparingly. Runs on a thread to avoid blocking
    the event loop.
    """
    targets = {
        "models": MODELS_DIR,
        "cache": CACHE_DIR,
        "voices": VOICE_DIR,
        "output": OUTPUT_DIR,
        "projects_output": PROJECTS_OUTPUT,
        "venv": VENV_DIR,
        "overrides": OVERRIDES_DIR,
        "repos": REPOS_DIR,
        "jobs": JOBS_DIR,
        "logs": WORKER_LOG_DIR,
    }
    sizes = await asyncio.to_thread(
        lambda: {name: _du_path(p) for name, p in targets.items()}
    )

    # Mount-level free/total (use the WSL ext4 root for the cache/data tree
    # and the host mount for the output/voices tree)
    def _disk_for(p: Path) -> dict:
        try:
            usage = shutil.disk_usage(str(p) if p.exists() else str(p.parent))
            return {"total": usage.total, "used": usage.used, "free": usage.free}
        except OSError as e:
            return {"error": str(e)}

    return {
        "directories": sizes,
        "mounts": {
            "models": _disk_for(MODELS_DIR),
            "output": _disk_for(OUTPUT_DIR),
        },
    }


@app.get("/api/env")
async def env_info():
    """Return only the subset of environment variables this app cares about.
    Useful for verifying caches actually point inside /opt/tts_server."""
    keys = (
        "HOME", "TMPDIR", "PATH",
        "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
        "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE", "HF_DATASETS_CACHE", "HF_MODULES_CACHE",
        "TORCH_HOME", "COQUI_TTS_CACHE", "TTS_HOME", "COQUI_TOS_AGREED",
        "PIP_CACHE_DIR",
        "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
        "PYTORCH_CUDA_ALLOC_CONF",
        "TTS_SERVER_APP_DIR", "TTS_SERVER_RUN_DIR",
        "TTS_SERVER_MAX_TEXT_CHARS", "TTS_SERVER_MAX_CHUNKS",
        "TTS_SERVER_MAX_INLINE_AUDIO_BYTES",
        "BRIDGE_PORT", "TTS_PORT",
    )
    return {"env": {k: os.environ.get(k) for k in keys}}


@app.get("/api/diagnostics")
async def diagnostics():
    """End-to-end self-check. Reports system binaries, GPU, HF token presence,
    install status of all models, worker health, env-var hygiene, and disk free.

    Designed to be the first call a remote operator runs when something
    isn't working.
    """
    def _sync_collect():
        bins = {name: _detect_binary(name)
                for name in ("ffmpeg", "espeak-ng", "sox", "git", "rubberband", "nvidia-smi", "bash")}
        gpu = _gpu_summary()
        hf_token_present = HF_TOKEN_FILE.exists() and HF_TOKEN_FILE.stat().st_size > 0
        legacy_token = LEGACY_HF_TOKEN_FILE.exists()

        # Check that critical caches actually point inside /opt/tts_server
        env = os.environ
        opt_paths = ("/opt/tts_server",)
        cache_in_opt = {
            k: any(str(env.get(k, "")).startswith(p) for p in opt_paths)
            for k in ("HF_HOME", "HF_HUB_CACHE", "TORCH_HOME",
                      "XDG_CACHE_HOME", "TMPDIR")
        }

        # Quick disk free on the data partition
        try:
            du = shutil.disk_usage(str(MODELS_DIR))
            disk_free_gb = du.free / (1024**3)
            disk_total_gb = du.total / (1024**3)
        except OSError:
            disk_free_gb = disk_total_gb = None

        # Setup install status (cheap heuristic; same as /api/setup/status)
        models_installed = {
            mid: _check_model_installed(mid) for mid in MODEL_SETUP
        }

        # Worker rollup
        ws = registry.all_workers()
        worker_summary = {
            "total": len(ws),
            "ready": sum(1 for w in ws if w.status == "ready"),
            "loading": sum(1 for w in ws if w.status == "loading"),
            "busy": sum(1 for w in ws if w.status == "busy"),
            "dead": sum(1 for w in ws if w.status == "dead"),
            "starting": sum(1 for w in ws if w.status == "starting"),
        }

        return {
            "ok": (
                bins["ffmpeg"]["present"] and bins["espeak-ng"]["present"]
                and disk_free_gb is not None and disk_free_gb > 5
                and worker_summary["dead"] == 0
            ),
            "binaries": bins,
            "gpu": gpu,
            "hf_token": {"present": hf_token_present, "legacy": legacy_token},
            "cache_in_opt": cache_in_opt,
            "disk": {"free_gb": round(disk_free_gb or 0, 2),
                     "total_gb": round(disk_total_gb or 0, 2)},
            "models": models_installed,
            "workers": worker_summary,
        }

    return await asyncio.to_thread(_sync_collect)


# ============================================================
# Per-model schema / capabilities
# ============================================================
# Map per-model param overrides. Most models accept the union of PARAM_CONFIG
# entries, but some are restricted (e.g. edge only takes pitch/volume).
_MODEL_PARAMS: dict[str, tuple[str, ...]] = {
    "kokoro":     ("speed",),
    "xtts":       ("temperature", "speed", "repetition_penalty", "top_p", "top_k"),
    "bark":       ("temperature", "waveform_temperature", "speed"),
    "chatterbox": ("temperature", "speed", "repetition_penalty",
                   "exaggeration", "cfg_weight"),
    "f5":         ("nfe_step", "cfg_scale", "speed", "seed"),
    "fish":       ("temperature", "speed", "repetition_penalty", "top_p", "seed"),
    "dia":        ("temperature", "cfg_scale", "speed", "top_p", "top_k"),
    "qwen":       ("temperature", "speed", "top_p", "top_k", "seed"),
    "vibevoice":  ("cfg_scale", "speed", "seed"),
    "higgs":      ("temperature", "speed", "top_p", "top_k", "seed"),
    "speecht5":   ("speed",),
    "parler":     ("temperature", "speed"),
    "outetts":    ("temperature", "speed", "repetition_penalty", "top_p", "top_k", "seed"),
    "vits":       ("speed",),
    "edge":       ("speed", "pitch", "volume"),
    "voxtral":    ("speed", "cfg_alpha", "seed"),
    "voxcpm2":    ("speed", "cfg_scale", "nfe_step"),
    "csm":        ("speed", "temperature", "top_p", "top_k", "seed"),
    "orpheus":    ("speed", "temperature", "top_p", "repetition_penalty", "seed"),
    "whisper":    (),
}


def _build_model_schema(model_id: str) -> dict:
    info = MODEL_SETUP.get(model_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")

    params: dict[str, dict] = {}
    accepted = _MODEL_PARAMS.get(model_id, tuple(PARAM_CONFIG.keys()))
    defaults = MODEL_DEFAULTS.get(model_id, {})
    for name in accepted:
        cfg = PARAM_CONFIG.get(name)
        if not cfg:
            continue
        label, lo, hi, step, fmt = cfg
        params[name] = {
            "label": label,
            "min": lo, "max": hi, "step": step, "format": fmt,
            "default": defaults.get(name, _PIPELINE_PARAM_DEFAULTS.get(name)),
            "tooltip": TOOLTIPS.get(name),
        }
    # Pipeline-side knobs are accepted by all models
    for name in ("de_reverb", "de_ess", "tolerance"):
        cfg = PARAM_CONFIG.get(name)
        if not cfg:
            continue
        label, lo, hi, step, fmt = cfg
        params[name] = {
            "label": label, "min": lo, "max": hi, "step": step, "format": fmt,
            "default": _PIPELINE_PARAM_DEFAULTS.get(name),
            "tooltip": TOOLTIPS.get(name), "pipeline": True,
        }
    return {
        "id": model_id,
        "name": info.get("display", model_id),
        "desc": info.get("desc"),
        "weights_repo": info.get("weights_repo"),
        "weights_dir": info.get("weights_dir"),
        "extra_weights_dirs": info.get("extra_weights_dirs", []),
        "weights_size": info.get("weights_size"),
        "capabilities": {
            "ref_audio": bool(info.get("ref_audio")),
            "ref_text": bool(info.get("ref_text")),
            "style_tags": bool(info.get("style_tags")),
            "builtin_voices": bool(info.get("builtin_voices")),
        },
        "usage": _VOICE_NOTES.get(model_id),
        "example": _VOICE_EXTRAS.get(model_id, {}).get("example"),
        "default_voice": _VOICE_EXTRAS.get(model_id, {}).get("default_voice"),
        "required_fields": list(_MODEL_REQUIRED_FIELDS.get(model_id, ())),
        "fields": MODEL_FIELDS.get(model_id, {}),
        "params": params,
        "output_formats": sorted(_OUTPUT_FORMATS),
        "infer_timeout_sec": MODEL_INFER_TIMEOUT.get(model_id, DEFAULT_INFER_TIMEOUT),
    }


@app.get("/api/schema/tts")
async def schema_all():
    """Schema for every registered model. Heavier than /api/models — includes
    every accepted param, default, range, capability flag."""
    return {"models": {mid: _build_model_schema(mid) for mid in MODEL_SETUP}}


@app.get("/api/schema/tts/{model}")
async def schema_one(model: str):
    return _build_model_schema(model)


@app.get("/api/tts/{model}/capabilities")
async def model_capabilities(model: str):
    """Compact capability summary plus the model's voice list. Useful for a
    dynamic UI: 'do I show a Reference Audio field for this model?'"""
    schema = _build_model_schema(model)
    voices: list = []
    voice_details: list[dict] = []
    catalog_source: str | None = None
    if model == "edge":
        voices, voice_details, catalog_source = await _edge_voices_live()
    elif model in _VOICE_BUILDERS:
        try:
            voices = _VOICE_BUILDERS[model]()
        except Exception:
            voices = []
    result = {
        "id": model,
        "name": schema["name"],
        "description": schema["desc"],
        "capabilities": schema["capabilities"],
        "usage": schema["usage"],
        "example": schema["example"],
        "default_voice": schema["default_voice"],
        "params": list(schema["params"].keys()),
        "fields": list(schema["fields"].keys()),
        "voices": voices,
        "voice_count": len(voices),
        "output_formats": schema["output_formats"],
    }
    if model == "edge":
        result["catalog_source"] = catalog_source
        result["voice_details"] = voice_details
    return result


# ============================================================
# Dryrun (preview chunking + estimate without inference)
# ============================================================
@app.post("/api/tts/{model}/dryrun")
async def tts_dryrun(model: str, req: PipelineTTSRequest):
    """Return the chunk plan for a TTS request without running inference.

    Lets a caller check chunk count, validate the request body, see the
    estimated runtime, and find the actual save_path resolved by the server,
    all without occupying a worker. No job is created."""
    if model not in _TTS_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown TTS model: {model}")
    chunks, effective, _, jdir, stem = _prepare_pipeline_request(model, req)
    tmp_ref = None
    try:
        tmp_ref = _normalize_reference_inputs(effective)
    finally:
        if tmp_ref:
            tmp_ref.unlink(missing_ok=True)
    raw_text = req.text.strip()
    too_many = False
    resolved_dir, resolved_stem, save_err = str(jdir), stem, None

    # Rough estimate: per-chunk inference time = MODEL_INFER_TIMEOUT/10
    # (real chunks are normally much faster than the timeout, this is just
    # a conservative upper bound for a UI spinner).
    per_chunk = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT) / 10.0
    est_sec = int(len(chunks) * per_chunk)

    return {
        "model": model,
        "text_chars": len(raw_text),
        "chunks": [{"index": i, "chars": len(c), "text": c}
                   for i, c in enumerate(chunks)],
        "chunk_count": len(chunks),
        "too_many_chunks": too_many,
        "max_chunks": _MAX_PIPELINE_CHUNKS,
        "estimated_runtime_sec": est_sec,
        "save_path": {
            "input": req.save_path,
            "resolved_dir": resolved_dir,
            "resolved_stem": resolved_stem,
            "error": save_err,
        },
        "output_format": (req.output_format or "wav").lower(),
        "verify_whisper": bool(req.verify_whisper),
        "effective_params": {
            name: effective.get(name)
            for name in (*_MODEL_PARAMS.get(model, ()),
                         "de_reverb", "de_ess", "tolerance")
            if effective.get(name) is not None
        },
    }


# ============================================================
# Job manifest / zip / stream / chunk-rerun
# ============================================================
def _safe_job_dir(job_id: str) -> Path:
    """Resolve a job_id to its directory or raise 404."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None or not job_dir.exists():
        raise HTTPException(status_code=400, detail="Job directory is invalid")
    return job_dir


@app.get("/api/jobs/{job_id}/manifest")
async def get_job_manifest(job_id: str):
    """Return the full job.json plus a directory listing (size + mtime)."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = job_manager.get_job_dir(job_id)
    files = []
    if job_dir and job_dir.exists():
        for f in sorted(job_dir.iterdir()):
            try:
                st = f.stat()
                files.append({
                    "name": f.name,
                    "size": st.st_size,
                    "modified": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "is_dir": f.is_dir(),
                })
            except OSError:
                continue
    return {"job": job, "directory": str(job_dir) if job_dir else None,
            "files": files}


def _build_zip_to_tempfile(src: Path) -> Path:
    """Build a .zip of ``src`` into a temp file and return its path.

    Streaming the zip generator directly is awkward because zipfile needs
    seekable output for ZIP_DEFLATED. Building to disk lets the client
    download a normal Content-Length response and avoids the BytesIO
    balloon (a 1GB project would otherwise allocate 1GB of RAM).
    """
    import tempfile as _tf
    fd, tmppath = _tf.mkstemp(prefix=f"{src.name}_", suffix=".zip", dir=str(TMP_DIR))
    os.close(fd)
    out = Path(tmppath)
    try:
        with _zipfile.ZipFile(out, "w", _zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for root, _dirs, fs in os.walk(src):
                for f in fs:
                    fp = Path(root) / f
                    arc = fp.relative_to(src.parent)
                    if fp.is_symlink():
                        raise ValueError(f"Cannot export symlink: {fp.name}")
                    zf.write(fp, arcname=str(arc))
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out


def _unlink_quiet(path: Path):
    global _active_exports
    _active_exports = max(0, _active_exports - 1)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/jobs/{job_id}/zip")
async def get_job_zip(job_id: str):
    """Build and serve a .zip of the job directory.

    The archive is built into TMP_DIR (inside WSL ext4 — fast) on a worker
    thread, then served as a regular FileResponse. A BackgroundTask deletes
    the temp file once the response has finished.
    """
    from starlette.background import BackgroundTask

    job_dir = _safe_job_dir(job_id)
    global _active_exports
    _active_exports += 1
    try:
        zip_path = await asyncio.to_thread(_build_zip_to_tempfile, job_dir)
    except BaseException:
        _active_exports -= 1
        raise
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"{job_dir.name}.zip",
        background=BackgroundTask(_unlink_quiet, zip_path),
    )


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, max_seconds: float = 3600.0):
    """SSE stream of job progress. Polls job_manager every 0.5s and emits
    deltas (status/chunks_completed). Closes when the job reaches a terminal
    state, the client disconnects, or ``max_seconds`` elapses (default 1h).

    A bounded duration prevents a hung pipeline from pinning the polling
    thread forever — clients can simply reconnect if they still care.
    """
    from starlette.responses import StreamingResponse as _SR

    job0 = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job0 is None:
        raise HTTPException(status_code=404, detail="Job not found")

    deadline = time.monotonic() + max(60.0, min(float(max_seconds), 24 * 3600.0))

    async def _gen():
        last = (None, -1, None)
        idle_ticks = 0
        while True:
            if time.monotonic() >= deadline:
                yield f"data: {json.dumps({'status': 'stream_timeout', 'job_id': job_id})}\n\n"
                return
            job = await asyncio.to_thread(_get_job_or_delivered, job_id)
            if job is None:
                yield f"data: {json.dumps({'status': 'gone', 'job_id': job_id})}\n\n"
                return
            progress = job.get("progress") or {}
            cur = (job.get("status"), job.get("chunks_completed", 0), progress.get("stage"))
            if cur != last:
                last = cur
                yield f"data: {json.dumps({'status': cur[0], 'chunks_completed': cur[1], 'total_chunks': job.get('total_chunks'), 'job_id': job_id, 'progress': progress})}\n\n"
                idle_ticks = 0
            else:
                idle_ticks += 1
                # keepalive every 15s of no change so proxies don't time out
                if idle_ticks % 30 == 0:
                    yield ": keepalive\n\n"
            if cur[0] in ("completed", "failed", "cancelled", "incomplete"):
                return
            await asyncio.sleep(0.5)

    return _SR(_gen(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/chunks/{chunk_idx}/rerun")
async def rerun_job_chunk(job_id: str, chunk_idx: int):
    """Re-render a single chunk by clearing its state and triggering recovery.
    The recover endpoint already resumes from the first incomplete chunk;
    this just resets one chunk (and everything after it for consistency)
    and kicks recovery."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    chunks = job.get("chunks", [])
    if chunk_idx < 0 or chunk_idx >= len(chunks):
        raise HTTPException(
            status_code=400,
            detail=f"Chunk index {chunk_idx} out of range (0-{len(chunks) - 1})",
        )

    # Reuse the edit-chunk path's reset semantics by writing the same text
    # back. This keeps recovery truncation logic in one place.
    with job_manager._job_lock(job_id):
        job_file = job_manager._find_job_file(job_id)
        if not job_file:
            raise HTTPException(status_code=404, detail="Job file not found")
        _reject_active_job(job_id)
        data = job_manager._read_job(job_file)
        job_manager.invalidate_output(data)
        for c in data["chunks"][chunk_idx:]:
            c["duration_sec"] = None
            c["processing_error"] = None
            c["verification_passed"] = None
            c["whisper_transcript"] = None
            c["whisper_similarity"] = None
        data["chunks_completed"] = min(chunk_idx, data.get("chunks_completed", 0))
        stem = data.get("stem") or job_file.parent.name
        output_format = data.get("output_format", "wav")
        data["missing_files"] = [
            f"chunk_{i:03d}.wav" for i in range(chunk_idx, len(data["chunks"]))
        ] + [f"{stem}_final.{output_format}"]
        job_manager._write_job(job_file, data)

    # Trigger recovery (same as /recover)
    return await recover_job(job_id)


# ============================================================
# SRT generation (Whisper-based subtitles for completed jobs)
# ============================================================
def _fmt_srt_timestamp(seconds) -> str:
    """Format seconds → SRT timestamp 'HH:MM:SS,mmm'."""
    if not isinstance(seconds, (int, float)):
        return "00:00:00,000"
    # Round once to total milliseconds, then decompose. Incrementing only the
    # seconds field when milliseconds round to 1000 can produce invalid SRT
    # timestamps such as 00:00:60,000 at minute/hour boundaries.
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hrs, remainder = divmod(total_ms, 3_600_000)
    mins, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def _build_srt(words: list[dict], words_per_line: int) -> str:
    """Group Whisper word timings into N-word lines and return SRT text."""
    if not words or words_per_line < 1:
        return ""
    lines: list[str] = []
    idx = 1
    for start in range(0, len(words), words_per_line):
        group = words[start:start + words_per_line]
        if not group:
            continue
        t0 = group[0].get("start")
        t1 = group[-1].get("end")
        if t0 is None or t1 is None:
            continue
        text = " ".join((w.get("word") or "").strip() for w in group if w.get("word"))
        if not text:
            continue
        lines.append(
            f"{idx}\n{_fmt_srt_timestamp(t0)} --> {_fmt_srt_timestamp(t1)}\n{text}\n"
        )
        idx += 1
    return "\n".join(lines) + ("\n" if lines else "")


def _subtitle_token_key(token: str) -> str:
    """Case-fold a subtitle token for source/ASR sequence alignment."""
    return "".join(ch for ch in token.casefold() if ch.isalnum())


def _subtitle_source_text(model: str | None, text: str | None) -> str:
    """Return only the portion of an engine script intended to be spoken.

    Engine-native voice/style controls are not narration. Keeping them in
    source-guided captions invents words and distorts subsequent timestamps,
    so strip known controls while retaining the original script in the job
    record.
    """
    source = str(text or "").strip()
    if str(model or "").casefold() == "voxcpm2":
        source = re.sub(r"^\s*\([^\r\n)]{1,500}\)\s*", "", source, count=1)
    elif str(model or "").casefold() == "orpheus":
        source = re.sub(
            r"<(?:laugh|sigh|chuckle|cough|sniffle|groan|yawn|gasp)>",
            " ", source, flags=re.IGNORECASE,
        )
        source = re.sub(r"\s+", " ", source)
    if model == "dia":
        source = re.sub(r"\[S[12]\]", " ", source)
    elif model == "vibevoice":
        source = re.sub(r"Speaker\s+\d+\s*:", " ", source, flags=re.I)
    return source.strip()


def _spread_source_tokens(tokens: list[str], start: float, end: float) -> list[dict]:
    """Distribute source tokens over one Whisper-timed span by text weight."""
    if not tokens:
        return []
    start = max(0.0, float(start))
    end = max(start, float(end))
    weights = [max(1, len(_subtitle_token_key(token))) for token in tokens]
    total = float(sum(weights))
    cursor = start
    out: list[dict] = []
    for index, (token, weight) in enumerate(zip(tokens, weights)):
        token_end = end if index == len(tokens) - 1 else cursor + (end - start) * weight / total
        out.append({"word": token, "start": cursor, "end": max(cursor, token_end)})
        cursor = out[-1]["end"]
    return out


def _source_guided_word_timings(
    source_text: str,
    raw_words: list[dict],
    audio_duration: float | None = None,
) -> tuple[list[dict], dict]:
    """Preserve the TTS script while anchoring it to Whisper word timings.

    Whisper remains the acoustic clock. Sequence matching keeps exact words on
    their observed spans, substitutes known source spellings for near-homophone
    ASR errors, and interpolates words Whisper omitted. Raw ASR is retained in
    the timing sidecar so this correction is fully auditable.
    """
    source_tokens = re.findall(r"\S+", (source_text or "").strip())
    valid_raw: list[dict] = []
    for item in raw_words or []:
        word = str(item.get("word") or "").strip()
        start = item.get("start")
        end = item.get("end")
        if not word or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        valid_raw.append({"word": word, "start": max(0.0, float(start)), "end": max(float(start), float(end))})

    raw_keys = [_subtitle_token_key(item["word"]) for item in valid_raw]
    source_keys = [_subtitle_token_key(token) for token in source_tokens]
    similarity = SequenceMatcher(None, source_keys, raw_keys, autojunk=False).ratio() if source_keys or raw_keys else 1.0
    meta = {
        "mode": "source_guided",
        "similarity": round(float(similarity), 6),
        "source_word_count": len(source_tokens),
        "raw_word_count": len(valid_raw),
    }
    if not source_tokens:
        return valid_raw, {**meta, "mode": "raw_asr"}

    if not valid_raw:
        duration = float(audio_duration or 0.0)
        if duration <= 0:
            return [], {**meta, "timing_available": False}
        return _spread_source_tokens(source_tokens, 0.0, duration), {**meta, "timing_available": True}

    aligned: list[dict | None] = [None] * len(source_tokens)
    matcher = SequenceMatcher(None, source_keys, raw_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                raw = valid_raw[j1 + offset]
                aligned[i1 + offset] = {
                    "word": source_tokens[i1 + offset],
                    "start": raw["start"],
                    "end": raw["end"],
                }
        elif tag == "replace" and j2 > j1:
            replacement = _spread_source_tokens(
                source_tokens[i1:i2], valid_raw[j1]["start"], valid_raw[j2 - 1]["end"]
            )
            aligned[i1:i2] = replacement

    raw_durations = sorted(max(0.02, item["end"] - item["start"]) for item in valid_raw)
    typical = min(0.6, max(0.12, raw_durations[len(raw_durations) // 2]))
    index = 0
    while index < len(aligned):
        if aligned[index] is not None:
            index += 1
            continue
        run_start = index
        while index < len(aligned) and aligned[index] is None:
            index += 1
        run_end = index
        count = run_end - run_start
        left = aligned[run_start - 1]["end"] if run_start else valid_raw[0]["start"]
        if run_end < len(aligned) and aligned[run_end] is not None:
            right = aligned[run_end]["start"]
        else:
            right = max(left + 0.08 * count, valid_raw[-1]["end"] + typical * count)
            if isinstance(audio_duration, (int, float)) and audio_duration > left:
                right = min(right, float(audio_duration))
        if right <= left:
            right = left + 0.08 * count
        aligned[run_start:run_end] = _spread_source_tokens(source_tokens[run_start:run_end], left, right)

    # Enforce monotonic timestamps after interpolation and malformed ASR spans.
    cursor = 0.0
    final: list[dict] = []
    for item in aligned:
        if item is None:
            continue
        start = max(cursor, float(item["start"]))
        end = max(start + 0.02, float(item["end"]))
        if audio_duration is not None:
            start, end = min(start, audio_duration), min(end, audio_duration)
        final.append({"word": item["word"], "start": round(start, 3), "end": round(end, 3)})
        cursor = end
    return final, {**meta, "timing_available": bool(final)}


async def _ensure_whisper_worker_ready(timeout_sec: int = 600):
    await asyncio.to_thread(_ensure_pipeline_worker_ready_sync, "whisper", {})
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        worker = registry.atomic_pick_and_mark_busy("whisper", "transcribe")
        if worker:
            return worker
        await asyncio.sleep(0.05)
    raise HTTPException(status_code=503, detail="Timed out waiting for Whisper")



class JobSrtRequest(BaseModel):
    words_per_line: int = Field(3, ge=1, le=20)
    size: str = "base"
    source_guided: bool = True

    @field_validator("size")
    @classmethod
    def size_valid(cls, value: str) -> str:
        if value not in WHISPER_AVAILABLE_MODELS:
            raise ValueError(f"Invalid whisper size '{value}'. Choose from: {list(WHISPER_AVAILABLE_MODELS.keys())}")
        return value


@app.post("/api/jobs/{job_id}/srt")
async def generate_job_srt(job_id: str, req: JobSrtRequest = JobSrtRequest()):
    """Generate SRT subtitles for a completed job using Whisper.

    Writes ``<stem>.srt`` and ``<stem>_timing.json`` into the job directory
    next to the final audio file. By default, Whisper supplies acoustic timing
    while the known TTS source supplies exact caption text. Set
    ``source_guided=false`` to request uncorrected ASR captions instead.
    """
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    final_file = _safe_job_file_name(job.get("final_file"))
    if not final_file:
        raise HTTPException(status_code=400, detail="Job has no final audio (not completed?)")
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="Job directory is invalid")
    audio_path = job_dir / final_file
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"Final audio not found: {final_file}")

    source_text = _subtitle_source_text(
        str(job.get("model") or ""), job.get("input_text")
    )
    worker = await _ensure_whisper_worker_ready()
    # Mark busy for the full transcription so the health loop won't kill it.
    registry.mark_busy(worker.worker_id, "transcribe")
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/transcribe",
                json={
                    "audio_path": str(audio_path),
                    "size": req.size,
                    "word_timestamps": True,
                    "initial_prompt": source_text[:2000] if req.source_guided and source_text else None,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Whisper worker call failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        registry.mark_ready(worker.worker_id)

    raw_words = result.get("words") or []
    raw_text = (result.get("text") or "").strip()
    language = result.get("language") or ""
    if req.source_guided and source_text:
        words, alignment = _source_guided_word_timings(
            source_text, raw_words, job.get("total_duration_sec")
        )
        text = source_text
    else:
        words = raw_words
        text = raw_text
        alignment = {
            "mode": "raw_asr",
            "source_word_count": len(re.findall(r"\S+", source_text)),
            "raw_word_count": len(raw_words),
        }

    stem = Path(final_file).stem
    srt_path = job_dir / f"{stem}.srt"
    timing_path = job_dir / f"{stem}_timing.json"

    srt_text = _build_srt(words, req.words_per_line)
    try:
        srt_path.write_text(srt_text, encoding="utf-8")
        timing_path.write_text(
            json.dumps({
                "words": words,
                "text": text,
                "language": language,
                "source_text": source_text,
                "raw_words": raw_words,
                "raw_text": raw_text,
                "alignment": alignment,
            },
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write SRT sidecars: {e}")

    line_count = srt_text.count("-->") if srt_text else 0
    logger.info("[%s SRT] job %s → %s (%d lines, %d words, lang=%s)",
                _ts(), job_id, srt_path.name, line_count, len(words), language)

    return {
        "status": "ok",
        "job_id": job_id,
        "srt_filename": srt_path.name,
        "srt_path": str(srt_path),
        "timing_filename": timing_path.name,
        "timing_path": str(timing_path),
        "line_count": line_count,
        "word_count": len(words),
        "words_per_line": req.words_per_line,
        "language": language,
        "text": text,
        "raw_text": raw_text,
        "source_guided": bool(req.source_guided and source_text),
        "alignment": alignment,
        "srt": srt_text,
    }


@app.get("/api/jobs/{job_id}/srt")
async def get_job_srt(job_id: str):
    """Download a previously generated .srt file for a job.

    Returns 404 if the SRT has not been generated yet (call POST first)."""
    job = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job and job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Current job revision is not complete")
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    final_file = _safe_job_file_name(job.get("final_file"))
    if not final_file:
        raise HTTPException(status_code=400, detail="Job has no final audio (not completed?)")
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None:
        srt_path = _delivered_artifact_path(job, "srt")
    else:
        stem = Path(final_file).stem
        srt_path = recorded_sidecar_path(
            job,
            job_dir,
            (("srt_path", None), ("srt", "srt_path")),
            f"{stem}.srt",
        )
    if srt_path is None:
        raise HTTPException(status_code=404, detail="Delivered SRT not found")
    if not srt_path.exists():
        raise HTTPException(
            status_code=404,
            detail="SRT not generated yet. POST /api/jobs/{job_id}/srt first.",
        )
    return FileResponse(str(srt_path), media_type="application/x-subrip", filename=srt_path.name)


@app.get("/api/jobs/{job_id}/srt/timing")
async def get_job_srt_timing(job_id: str):
    """Download the timing JSON sidecar (full word-level timestamps) for a job.

    Returns 404 if SRT generation has not been run yet."""
    job = await asyncio.to_thread(_get_job_or_delivered, job_id)
    if job and job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Current job revision is not complete")
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    final_file = _safe_job_file_name(job.get("final_file"))
    if not final_file:
        raise HTTPException(status_code=400, detail="Job has no final audio (not completed?)")
    job_dir = job_manager.get_job_dir(job_id)
    if job_dir is None:
        timing_path = _delivered_artifact_path(job, "timing")
    else:
        stem = Path(final_file).stem
        timing_path = recorded_sidecar_path(
            job,
            job_dir,
            (("srt_timing_path", None), ("srt", "timing_path")),
            f"{stem}_timing.json",
        )
    if timing_path is None:
        raise HTTPException(status_code=404, detail="Delivered timing JSON not found")
    if not timing_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Timing JSON not generated yet. POST /api/jobs/{job_id}/srt first.",
        )
    return FileResponse(str(timing_path), media_type="application/json", filename=timing_path.name)


# ============================================================
# Project ops
# ============================================================
def _safe_project_name(name: str) -> str:
    """Reject traversal / absolute paths. Project names must be a single dir."""
    if not name or "\x00" in name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid project name")
    p = (PROJECTS_OUTPUT / name).resolve()
    if not _path_is_relative_to(p, PROJECTS_OUTPUT):
        raise HTTPException(status_code=400, detail="Invalid project name")
    return name


def _scan_project_detail(proj: Path) -> dict:
    """rglob/scandir is slow on /mnt — keep this off the event loop."""
    jobs: list[dict] = []
    files: list[dict] = []
    for job_json in sorted(proj.rglob("job.json")):
        try:
            data = json.loads(job_json.read_text(encoding="utf-8"))
            jobs.append({
                "job_id": data.get("job_id"),
                "model": data.get("model"),
                "status": data.get("status"),
                "chunks_completed": data.get("chunks_completed"),
                "total_chunks": data.get("total_chunks"),
                "final_file": data.get("final_file"),
                "directory": str(job_json.parent),
            })
        except Exception:
            continue
    try:
        for f in sorted(proj.iterdir()):
            if not f.is_file():
                continue
            try:
                st = f.stat()
                files.append({"name": f.name, "size": st.st_size,
                              "modified": _dt.datetime.fromtimestamp(st.st_mtime).isoformat()})
            except OSError:
                continue
    except OSError:
        pass
    return {"jobs": jobs, "files": files}


@app.get("/api/projects/{name}")
async def project_detail(name: str):
    """List jobs and files inside one project directory."""
    name = _safe_project_name(name)
    proj = PROJECTS_OUTPUT / name
    if not proj.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    body = await asyncio.to_thread(_scan_project_detail, proj)
    return {"name": name, "path": str(proj), **body}


def _delete_project_sync(proj: Path) -> list[str]:
    """rmtree + collect job_ids in one thread; returns purged job ids."""
    job_ids: list[str] = []
    for job_json in proj.rglob("job.json"):
        try:
            jid = json.loads(job_json.read_text(encoding="utf-8")).get("job_id")
            if jid:
                job_ids.append(jid)
        except Exception:
            continue
    with _running_jobs_lock:
        if any(jid in ids for ids in _running_jobs.values() for jid in job_ids):
            raise HTTPException(status_code=409, detail="Project contains an active job")
        for job_json in proj.rglob("job.json"):
            if json.loads(job_json.read_text(encoding="utf-8")).get("status") == "running":
                raise HTTPException(status_code=409, detail="Project contains a running job")
        shutil.rmtree(proj)
    return job_ids


@app.delete("/api/projects/{name}")
async def project_delete(name: str):
    """Delete an entire project directory and all jobs under it. Irreversible."""
    name = _safe_project_name(name)
    proj = PROJECTS_OUTPUT / name
    if not proj.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    job_ids = await asyncio.to_thread(_delete_project_sync, proj)
    for jid in job_ids:
        try:
            await asyncio.to_thread(job_manager.delete_job, jid, True)
        except Exception:
            pass
    return {"name": name, "deleted": True, "jobs_purged": len(job_ids)}


@app.get("/api/projects/{name}/zip")
async def project_zip(name: str):
    """Build and serve a .zip of the project directory."""
    from starlette.background import BackgroundTask

    name = _safe_project_name(name)
    proj = PROJECTS_OUTPUT / name
    if not proj.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    global _active_exports
    _active_exports += 1
    try:
        zip_path = await asyncio.to_thread(_build_zip_to_tempfile, proj)
    except BaseException:
        _active_exports -= 1
        raise
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"{name}.zip",
        background=BackgroundTask(_unlink_quiet, zip_path),
    )


# ============================================================
# Voice metadata / rename
# ============================================================
@app.get("/api/voices/{filename}/info")
async def voice_info(filename: str):
    """Detailed voice metadata: size, duration, sample rate, transcription."""
    path = _safe_voice_path(filename)
    info = {
        "name": path.stem, "filename": path.name, "path": str(path),
        "size": path.stat().st_size,
        "format": path.suffix[1:].lower(),
    }
    # Best-effort metadata (don't fail if soundfile can't open it)
    try:
        with sf.SoundFile(str(path)) as f:
            info["sample_rate"] = f.samplerate
            info["channels"] = f.channels
            info["frames"] = f.frames
            info["duration_sec"] = round(f.frames / f.samplerate, 3) if f.samplerate else None
    except Exception as e:
        info["sample_rate"] = None
        info["duration_sec"] = None
        info["error"] = str(e)
    txt = _voice_transcript_path(path)
    info["transcription"] = txt.read_text(encoding="utf-8").strip() if txt.exists() else None
    return info


class VoiceRenameRequest(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=128)


@app.post("/api/voices/{filename}/rename")
async def voice_rename(filename: str, req: VoiceRenameRequest):
    """Rename a voice file in place, preserving its sidecar transcription."""
    path = _safe_voice_path(filename)
    new = req.new_name.strip()
    if "/" in new or "\\" in new or "\x00" in new or new.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid new name")
    # Preserve original extension if the user didn't supply one
    if not Path(new).suffix:
        new = new + path.suffix
    if Path(new).suffix.lower() not in _VOICE_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail="Invalid voice extension")
    dest = VOICE_DIR / Path(new).name
    if not dest.resolve().is_relative_to(VOICE_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid new name")
    if dest.exists():
        raise HTTPException(status_code=409, detail="Target name already exists")
    if dest.suffix.lower() != path.suffix.lower():
        raise HTTPException(status_code=400, detail="Renaming cannot change audio format")
    sidecar = _voice_transcript_path(path)
    sidecar_dest = _voice_transcript_path(dest, for_write=True)
    if sidecar_dest.exists():
        raise HTTPException(status_code=409, detail="Target transcript already exists")
    try:
        path.rename(dest)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Rename failed: {e}") from e
    if sidecar.exists():
        try:
            sidecar.rename(sidecar_dest)
        except OSError as e:
            # Audio file moved successfully — best-effort sidecar rename.
            logger.warning("Voice sidecar rename failed (%s -> %s): %s",
                           sidecar, sidecar_dest, e)
    return {"old": filename, "new": dest.name, "path": str(dest)}


# ============================================================
# Logs — tail persistent file (not the SSE live stream)
# ============================================================
@app.get("/api/logs/tail")
async def logs_tail(file: str = "server", lines: int = 200):
    """Read the last N lines from one of the persistent log files. Faster
    than the SSE stream when a caller just wants recent history."""
    if file not in _PERSISTENT_LOG_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown log: {file}. Choose from: {sorted(_PERSISTENT_LOG_FILES)}",
        )
    path = _PERSISTENT_LOG_FILES[file]
    if not path.exists():
        return {"file": file, "path": str(path), "lines": [], "exists": False}
    try:
        n = max(1, min(int(lines), 5000))
    except (ValueError, TypeError):
        n = 200

    def _tail():
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                blocksize = 8192
                data = b""
                while size > 0 and data.count(b"\n") <= n:
                    read = min(blocksize, size)
                    size -= read
                    fh.seek(size)
                    data = fh.read(read) + data
                lines_out = data.decode("utf-8", errors="replace").splitlines()
                return lines_out[-n:]
        except OSError as e:
            return [f"(read error: {e})"]

    out_lines = await asyncio.to_thread(_tail)
    return {"file": file, "path": str(path), "lines": out_lines, "exists": True}


# ============================================================
# Maintenance
# ============================================================
class GcJobsRequest(BaseModel):
    older_than_hours: int = Field(72, ge=1, le=24 * 365)


@app.post("/api/maintenance/gc-jobs")
async def maintenance_gc_jobs(req: GcJobsRequest = GcJobsRequest()):
    """Delete jobs older than N hours. Wraps job_manager.cleanup_old_jobs."""
    deleted = await asyncio.to_thread(job_manager.cleanup_old_jobs, req.older_than_hours)
    return {"older_than_hours": req.older_than_hours, "deleted": deleted}


class ClearCacheRequest(BaseModel):
    kinds: list[str] = Field(default_factory=lambda: ["tmp"])


@app.post("/api/maintenance/clear-cache")
async def maintenance_clear_cache(req: ClearCacheRequest = ClearCacheRequest()):
    global _maintenance_busy
    if (_active_api_requests > 1 or _active_exports or registry.worker_count()
            or _active_installs or any(_running_jobs.values())
            or any(not task.done() for task in _model_load_tasks.values())):
        raise HTTPException(status_code=409, detail="Wait for active requests, downloads, jobs and installs; unload workers before clearing caches")
    _maintenance_busy = True
    try:
        """Wipe selected cache trees. 'all' is a synonym for every kind.
        DOES NOT touch model weights (data/) or the venv — those live in
        /opt/tts_server/data and /opt/tts_server/venv respectively and are
        managed by /api/setup/install/{model} / /api/setup/{model}."""
        cache_root = CACHE_DIR
        candidates = {
            "tmp":  cache_root / "tmp",
            "pip":  cache_root / "pip",
            "home": cache_root / "home",  # Triton/FlashInfer/JIT runtime caches
            "hub":  MODELS_DIR / "hub",     # HF hub blobs (rebuilt on next download)
            "datasets": MODELS_DIR / "datasets",
            "torch": MODELS_DIR / "torch",
            "xdg":   cache_root / "xdg-cache",
            "xdg-config": cache_root / "xdg-config",
            "xdg-data": cache_root / "xdg-data",
            "xdg-state": cache_root / "xdg-state",
            "modules": MODELS_DIR / "modules",
            "logs": WORKER_LOG_DIR,
        }
        kinds = req.kinds or ["tmp"]
        if "all" in kinds:
            kinds = list(candidates.keys())

        def _wipe(target: Path) -> dict:
            """Delete cache contents, reporting any failed deletion."""
            if not target.exists():
                return {"ok": True, "skipped": "missing", "path": str(target)}
            try:
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                return {"ok": True, "path": str(target)}
            except Exception as e:
                return {"ok": False, "error": str(e), "path": str(target)}

        cleared: dict[str, dict] = {}
        for kind in kinds:
            target = candidates.get(kind)
            if not target:
                cleared[kind] = {"ok": False, "error": "unknown kind"}
                continue
            before = (await asyncio.to_thread(_du_path, target))["bytes"]
            result = await asyncio.to_thread(_wipe, target)
            after = (await asyncio.to_thread(_du_path, target))["bytes"]
            result["bytes_before"] = before
            result["bytes_after"] = after
            result["bytes_freed"] = max(0, before - after)
            cleared[kind] = result
        return {"cleared": cleared}
    finally:
        _maintenance_busy = False


@app.post("/api/maintenance/kill-stale-workers")
async def maintenance_kill_stale_workers():
    """Kill any worker that's not in (ready, busy). Catches stuck loaders
    and workers that crashed but didn't get reaped by the health loop."""
    killed = []
    for w in registry.all_workers():
        if w.status not in ("ready", "busy"):
            if await worker_manager.kill_worker(w.worker_id):
                killed.append(w.worker_id)
    return {"killed": killed, "count": len(killed)}


@app.post("/api/maintenance/restart-workers")
async def maintenance_restart_workers(model: Optional[str] = None):
    """Kill all workers (optionally only for one model) so the next request
    forces a fresh spawn. Useful after an install or a code update."""
    if model and model not in MODEL_SETUP:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model}")
    workers = registry.workers_for_model(model) if model else registry.all_workers()
    killed = []
    for w in workers:
        if await worker_manager.kill_worker(w.worker_id):
            killed.append(w.worker_id)
    return {"killed": killed, "count": len(killed), "model": model}


@app.post("/api/maintenance/cleanup-temp")
async def maintenance_cleanup_temp(min_age_minutes: int = 30):
    global _maintenance_busy
    if (_active_api_requests > 1 or _active_exports or registry.worker_count()
            or _active_installs or any(_running_jobs.values())
            or any(not task.done() for task in _model_load_tasks.values())):
        raise HTTPException(status_code=409, detail="Wait for active requests, downloads, jobs and installs; unload workers before clearing caches")
    _maintenance_busy = True
    try:
        """Remove orphaned temp files older than ``min_age_minutes``.

        The age guard prevents nuking files an active worker just opened —
        NamedTemporaryFile names a fresh file the moment a chunk starts, so
        sweeping with no age check would race the pipeline.
        """
        cutoff = time.time() - max(0, int(min_age_minutes)) * 60

        def _sweep() -> list[str]:
            removed: list[str] = []
            # OUTPUT_DIR — pipeline scratch from _process_single_chunk + assembly
            for pattern in ("raw_*_*.wav", "assembled_*.wav", "ref_*.*"):
                for tmp in OUTPUT_DIR.glob(pattern):
                    try:
                        if tmp.stat().st_mtime > cutoff:
                            continue
                        tmp.unlink()
                        removed.append(str(tmp))
                    except OSError:
                        continue
            # TMP_DIR — worker-side NamedTemporaryFile output
            # (Python's default prefix is "tmp" with no underscore)
            for pattern in ("tmp*.wav", "tmp*.mp3", "raw_*.wav", "ref_*.*"):
                for tmp in TMP_DIR.glob(pattern):
                    try:
                        if tmp.stat().st_mtime > cutoff:
                            continue
                        tmp.unlink()
                        removed.append(str(tmp))
                    except OSError:
                        continue
            return removed

        removed = await asyncio.to_thread(_sweep)
        return {"removed": removed, "count": len(removed),
                "min_age_minutes": min_age_minutes}


    finally:
        _maintenance_busy = False


@app.post("/api/shutdown")
async def api_shutdown():
    """Cleanly shut down the gateway, workers, and the Windows window.

    Order matters:
      1. Graceful worker unload (kill_all_workers does HTTP /unload then SIGKILL).
      2. Unpublish discovery so peer apps stop seeing us.
      3. Let the native host close its own window after the response.
      4. Signal only this gateway; never kill Windows processes by image name.
    """
    (RUN_DIR / "shutdown.requested").touch()
    logger.info("Shutdown requested via /api/shutdown")

    try:
        killed = await worker_manager.kill_all_workers()
        logger.info("Shutdown: killed %d workers", killed)
    except Exception as e:
        logger.warning("Shutdown: kill_all_workers raised: %s", e)
        killed = 0

    try:
        worker_manager.kill_orphan_workers()
    except Exception:
        pass

    try:
        discovery.unpublish(
            _REGISTRY_NAME,
            fallback_dir=str(RUN_DIR),
            expected_pid=os.getpid(),
            expected_token=_API_TOKEN,
        )
    except Exception:
        pass

    def _delayed_exit():
        # Give the HTTP response time to reach the UI, which then invokes the
        # native closeApp binding. Headless sessions use the same self-stop.
        time.sleep(2.0)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting_down", "workers_killed": killed}


# ============================================================
# Static file serving (Web UI)
# ============================================================
from fastapi.staticfiles import StaticFiles

_static_dir = BASE_DIR / "static"


def _client_is_loopback(request: Request) -> bool:
    """True only when the request originates from the local host (127.0.0.0/8 or ::1)."""
    client = request.client
    host = client.host if client else None
    # The bridge overwrites this header with its actual peer address.
    # Direct callers may suppress bootstrap with this header, never elevate it.
    forwarded = request.headers.get("x-tts-client-ip")
    if forwarded:
        import ipaddress
        try:
            if not ipaddress.ip_address(forwarded).is_loopback:
                return False
        except ValueError:
            return False
    if not host:
        return False
    if host == "::1" or host.startswith("127."):
        return True
    # IPv4-mapped IPv6 loopback, e.g. ::ffff:127.0.0.1
    if host.startswith("::ffff:") and host[7:].startswith("127."):
        return True
    return False


def _serve_index_with_token(request: Request):
    index = _static_dir / "index.html"
    html = index.read_text(encoding="utf-8")
    # Only hand out the API token to loopback clients. A non-loopback (LAN)
    # client still gets the page, but without the embedded master credential.
    if _client_is_loopback(request):
        token_script = (
            "<script>"
            f"window.__TTS_API_TOKEN__ = {json.dumps(_API_TOKEN)};"
            "</script>"
        )
        if "</head>" in html:
            html = html.replace("</head>", token_script + "</head>", 1)
        else:
            html = token_script + html
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/")
async def serve_root(request: Request):
    """Serve the web UI or fall back to health check."""
    index = _static_dir / "index.html"
    if index.exists():
        return _serve_index_with_token(request)
    return await health()


if _static_dir.exists():
    @app.get("/static/index.html")
    async def serve_static_index(request: Request):
        return _serve_index_with_token(request)

    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ============================================================
# Server management
# ============================================================
def start_server(host: str = DEFAULT_API_HOST, port: int = DEFAULT_API_PORT):
    global _GATEWAY_PORT
    _GATEWAY_PORT = port
    # access_log=False: the access log records the full request line including
    # any `?token=` query string, and the gateway's stdout is captured to
    # tts_server_output.log (exposed back via /api/logs). Disabling access logs
    # keeps the master token out of a log retrievable through the API/UI.
    uvicorn.run(app, host=host, port=port, access_log=False)


if __name__ == "__main__":
    import argparse

    # Debug log for startup issues
    _debug_log = str(RUN_DIR / "tts_server_startup.log")
    def _startup_log(msg):
        line = f"[{__import__('datetime').datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(_debug_log, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    _startup_log("=" * 60)
    _startup_log("TTS API Server starting")
    _startup_log(f"Python: {sys.executable}")
    _startup_log(f"sys.path: {sys.path[:5]}")
    _startup_log(f"CWD: {os.getcwd()}")
    _startup_log(f"__file__: {__file__}")
    _startup_log(f"BASE_DIR: {BASE_DIR}")
    _startup_log(f"MODELS_DIR: {MODELS_DIR}")
    _startup_log(f"OUTPUT_DIR: {OUTPUT_DIR}")
    _startup_log(f"VOICE_DIR: {VOICE_DIR}")
    _startup_log(f"Static dir exists: {_static_dir.exists()}")
    _startup_log(f"Static index.html exists: {(_static_dir / 'index.html').exists()}")
    _startup_log("=" * 60)

    parser = argparse.ArgumentParser()
    # Bind loopback only by default. Opt in to LAN/non-loopback exposure via
    # --bind-lan (or the TTS_BIND_LAN env var); an explicit --host always wins.
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument(
        "--bind-lan",
        action="store_true",
        default=os.environ.get("TTS_BIND_LAN", "").lower() in ("1", "true", "yes", "on"),
        help="Bind to all interfaces (0.0.0.0) instead of loopback only.",
    )
    args = parser.parse_args()

    if args.host:
        bind_host = args.host
    elif args.bind_lan:
        bind_host = "0.0.0.0"
    else:
        bind_host = DEFAULT_API_HOST

    try:
        _startup_log(f"Starting uvicorn on {bind_host}:{args.port}")
        start_server(bind_host, args.port)
    except Exception as e:
        _startup_log(f"FATAL: {e}")
        _startup_log(traceback.format_exc())
        raise
