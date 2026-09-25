"""Worker Manager - Spawns, monitors, and kills worker subprocesses."""

import asyncio
import ctypes
import logging
import os
import subprocess
import time
import threading
import re
from pathlib import Path

import httpx

from config import (
    APP_DIR, BASE_DIR, PYTHON_PATH, WORKER_PORT_MIN, WORKER_PORT_MAX,
    WORKER_HEALTH_INTERVAL, WORKER_STARTUP_TIMEOUT,
    WORKER_MAX_HEALTH_FAILURES, WORKER_LOG_DIR, WORKER_DEFAULT_DEVICE,
    LINUX_STATE_ROOT, MODELS_DIR, OVERRIDES_DIR, MODEL_OVERRIDE_MAP,
    MODEL_SETUP, MODEL_VRAM_ESTIMATE_GB, app_environment,
)
from worker_registry import WorkerRegistry, WorkerInfo

logger = logging.getLogger(__name__)


class WorkerManager:
    """Manages the lifecycle of TTS worker subprocesses."""

    _DEVICE_CACHE_TTL = 30  # seconds

    def __init__(self, registry: WorkerRegistry | None = None):
        self.registry = registry or WorkerRegistry(WORKER_PORT_MIN, WORKER_PORT_MAX)
        self._health_task: asyncio.Task | None = None
        self._device_cache: list[dict] | None = None
        self._device_cache_time: float = 0.0
        self._device_load_locks: dict[str, threading.Lock] = {}
        self._last_reclaim: dict[str, dict] = {}
        WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)

    async def spawn_worker(self, model: str, device: str | None = None,
                           precision: str | None = None) -> WorkerInfo:
        """Capacity-check and serialize model loads on each GPU."""
        device = str(device or WORKER_DEFAULT_DEVICE)
        device = self.normalize_device(device)
        lock = self._device_load_locks.setdefault(device, threading.Lock())
        # Nonblocking acquisition works across the request loop and executor loops.
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.05)
        try:
            await asyncio.to_thread(self._ensure_device_capacity, model, device)
            return await self._spawn_worker_unlocked(model, device, precision)
        finally:
            lock.release()

    @staticmethod
    def normalize_device(device: str) -> str:
        device = str(device).strip().lower()
        if device == "cuda":
            device = "cuda:0"
        if device != "cpu" and not re.fullmatch(r"cuda:\d+", device):
            raise ValueError("device must be cpu, cuda, or cuda:N")
        return device

    def _ensure_device_capacity(self, model: str, device: str) -> None:
        """Reject a CUDA load that cannot leave the configured safety reserve."""
        if not device.startswith("cuda:"):
            return
        # Force a fresh nvidia-smi read. The public device endpoint is cached,
        # but another local application may have loaded or unloaded a model in
        # the last few seconds.
        devices = self.detect_devices(force_refresh=True)
        current = next((item for item in devices if item.get("id") == device), None)
        if current is None:
            raise RuntimeError(f"CUDA device {device} is not available")
        estimate_gb = 1.0 if model == "whisper" else float(MODEL_VRAM_ESTIMATE_GB.get(model, 0) or 0)
        reserve_gb = max(0.0, float(os.environ.get("TTS_SERVER_GPU_RESERVE_GB", "1.5")))
        free_mb = int(current.get("vram_free_mb") or 0)
        required_mb = int((estimate_gb + reserve_gb) * 1024)
        if required_mb and free_mb < required_mb:
            raise RuntimeError(
                f"Refusing to load {model} on {device}: {free_mb / 1024:.1f} GiB "
                f"VRAM is free, but the model estimate plus safety reserve is "
                f"{required_mb / 1024:.1f} GiB. Unload another model, choose a "
                f"different device, or explicitly lower TTS_SERVER_GPU_RESERVE_GB."
            )

    async def _spawn_worker_unlocked(self, model: str, device: str | None = None,
                                     precision: str | None = None) -> WorkerInfo:
        """Spawn a new worker subprocess for the given model.

        Args:
            model: Model identifier (kokoro, xtts, higgs, etc.)
            device: CUDA device string (cuda:0, cuda:1, cpu). Defaults to config.
            precision: Weight precision — "fp32", "fp16", "bf16", or None (auto).

        Returns:
            WorkerInfo for the spawned worker.

        Raises:
            RuntimeError: If worker fails to start or become ready.
        """
        device = str(device or WORKER_DEFAULT_DEVICE)
        port = self.registry.allocate_port()
        worker_id = self.registry.next_worker_id(model)

        worker_script = str(BASE_DIR / "tts_worker.py")
        python_exe = str(PYTHON_PATH)

        # Set up environment for the subprocess
        env = os.environ.copy()
        env.update(app_environment())
        env["TTS_SERVER_WORKER_SCRIPT"] = str((BASE_DIR / "tts_worker.py").resolve())
        env["PYTHONUNBUFFERED"] = "1"  # flush worker logs immediately

        # Ensure CUDA and nvidia-smi use the same GPU ordering (PCI bus ID).
        # Without this, WSL2 can enumerate GPUs in a different order than
        # nvidia-smi, causing the wrong GPU to be selected.
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        # Reduce CUDA memory fragmentation — expandable segments let PyTorch
        # grow allocations instead of requiring contiguous blocks up front.
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

        # Restrict GPU visibility to the assigned device.
        # CUDA_VISIBLE_DEVICES causes PyTorch to renumber the visible GPU as cuda:0,
        # so we must also remap the --device arg to cuda:0 for the worker.
        worker_device = device
        if device.startswith("cuda:"):
            env["CUDA_VISIBLE_DEVICES"] = device.split(":")[1]
            worker_device = "cuda:0"

        cmd = [
            python_exe, worker_script,
            "--model", model,
            "--port", str(port),
            "--device", worker_device,
        ]
        if precision:
            cmd.extend(["--precision", precision])

        log_file = WORKER_LOG_DIR / f"worker_{model}_{port}.log"
        logger.info("Spawning worker %s: model=%s port=%d device=%s "
                    "(CUDA_VISIBLE_DEVICES=%s, worker sees %s)",
                    worker_id, model, port, device,
                    env.get("CUDA_VISIBLE_DEVICES", "all"), worker_device)

        try:
            log_fh = open(log_file, "w", encoding="utf-8", buffering=1)  # line-buffered
            process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(BASE_DIR),
                start_new_session=True,  # own process group — killpg catches children
            )
        except Exception as e:
            self.registry.release_port(port)
            if 'log_fh' in locals():
                log_fh.close()
            raise RuntimeError(f"Failed to launch worker process: {e}")

        # Capture the process-group id now, while the main pid is guaranteed
        # alive. start_new_session=True makes the worker its own group leader
        # (pgid == pid). Saving it lets _force_kill reap the whole group even if
        # the main pid later exits before its GPU-holding children.
        try:
            pgid = os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            pgid = process.pid

        worker = WorkerInfo(
            worker_id=worker_id,
            model=model,
            port=port,
            device=device,
            process=process,
            pgid=pgid,
            log_fh=log_fh,
            status="starting",
        )
        self.registry.register(worker)

        # Wait for worker FastAPI to be up, then load the model
        try:
            await self._wait_for_healthy(worker)
            await self._load_model(worker)
        except BaseException as e:
            # asyncio.CancelledError inherits BaseException on current Python.
            # Treat cancellation as a normal teardown path so cancelling a
            # pending API load cannot orphan its process, registry entry, or
            # allocated port.
            if not isinstance(e, asyncio.CancelledError):
                try:
                    log_tail = log_file.read_text(
                        encoding="utf-8", errors="replace"
                    )[-2000:]
                    logger.error("Worker %s failed. Log tail:\n%s", worker_id, log_tail)
                except Exception:
                    pass
            await self._force_kill(worker)
            removed = self.registry.unregister(worker_id)
            if removed is not None:
                self.registry.release_port(removed.port)
            if isinstance(e, asyncio.CancelledError):
                raise
            raise RuntimeError(f"Worker {worker_id} failed: {e}") from e

        return worker

    async def _wait_for_healthy(self, worker: WorkerInfo) -> None:
        """Poll worker /health until FastAPI responds (model may not be loaded yet)."""
        url = f"http://127.0.0.1:{worker.port}/health"
        deadline = time.time() + WORKER_STARTUP_TIMEOUT

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.time() < deadline:
                if worker.process and worker.process.poll() is not None:
                    raise RuntimeError(
                        f"Worker {worker.worker_id} process exited with code "
                        f"{worker.process.returncode} during startup"
                    )

                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except Exception:
                            data = {}
                        expected_pid = worker.process.pid if worker.process else 0
                        actual_pid = int(data.get("pid") or 0)
                        if data.get("model") == worker.model and actual_pid == expected_pid:
                            logger.info("Worker %s FastAPI is up (pid=%d)",
                                        worker.worker_id, expected_pid)
                            return
                        logger.warning(
                            "Ignoring non-matching service on worker port %d "
                            "(expected model=%s pid=%d, got model=%s pid=%s)",
                            worker.port, worker.model, expected_pid,
                            data.get("model"), data.get("pid"),
                        )
                    if resp.status_code >= 500:
                        raise RuntimeError(
                            f"Worker {worker.worker_id} returned {resp.status_code} during startup"
                        )
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass

                await asyncio.sleep(1.0)

        raise RuntimeError(
            f"Worker {worker.worker_id} FastAPI did not start within "
            f"{WORKER_STARTUP_TIMEOUT}s"
        )

    async def _load_model(self, worker: WorkerInfo) -> None:
        """Call POST /load on the worker to load the model into memory."""
        load_url = f"http://127.0.0.1:{worker.port}/load"
        health_url = f"http://127.0.0.1:{worker.port}/health"

        self.registry.mark_loading(worker.worker_id)
        logger.info("Loading model on worker %s ...", worker.worker_id)

        # /load blocks until the model is fully loaded; use a long timeout
        async with httpx.AsyncClient(timeout=900.0) as client:
            try:
                resp = await client.post(load_url)
                if resp.status_code != 200:
                    detail = resp.text[:500]
                    logger.error("Worker %s /load failed (%d): %s",
                                 worker.worker_id, resp.status_code, detail)
                    raise RuntimeError(
                        f"Worker {worker.worker_id} /load returned "
                        f"{resp.status_code}: {detail}"
                    )
            except httpx.TimeoutException:
                raise RuntimeError(
                    f"Worker {worker.worker_id} model load timed out (900s)"
                )

            # Confirm health reports ready
            try:
                resp = await client.get(health_url)
                if resp.status_code == 200:
                    data = resp.json()
                    worker.vram_used_mb = data.get("vram_used_mb", 0)
                    worker.vram_total_mb = data.get("vram_total_mb", 0)
            except Exception:
                pass

        self.registry.mark_ready(worker.worker_id)
        self.registry.update_health(worker.worker_id,
                                     vram_used_mb=worker.vram_used_mb,
                                     vram_total_mb=worker.vram_total_mb)
        logger.info("Worker %s model loaded and ready", worker.worker_id)

    async def kill_worker(self, worker_id: str) -> bool:
        """Gracefully stop and remove a worker.

        Returns True if worker was found and killed.
        """
        worker = self.registry.get(worker_id)
        if not worker:
            return False

        logger.info("Killing worker %s (port=%d)", worker_id, worker.port)

        # Try graceful unload first
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"http://127.0.0.1:{worker.port}/unload")
        except Exception:
            pass

        mapped_app_files = self._mapped_app_files(
            worker.process.pid if worker.process else None
        )
        await self._force_kill(worker)
        # Release the port only if this call removed the worker, so a concurrent
        # cleanup path cannot double-release and re-add a port a new worker owns.
        removed = self.registry.unregister(worker_id)
        if removed is not None:
            self.registry.release_port(removed.port)
        self._last_reclaim[worker.model] = await asyncio.to_thread(
            self.reclaim_model_file_cache, worker.model, mapped_app_files
        )
        self._reclaim_memory()
        return True

    @staticmethod
    def _mapped_app_files(pid: int | None) -> set[Path]:
        """Return app-owned files mapped by a worker before it exits."""
        if not pid:
            return set()
        try:
            lines = Path(f"/proc/{pid}/maps").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return set()
        result: set[Path] = set()
        app_root = LINUX_STATE_ROOT.resolve(strict=False)
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) < 6:
                continue
            raw = fields[5].removesuffix(" (deleted)")
            if not raw.startswith("/"):
                continue
            path = Path(raw)
            try:
                path.resolve(strict=False).relative_to(app_root)
                if path.is_file():
                    result.add(path)
            except (OSError, ValueError):
                continue
        return result

    @staticmethod
    def _model_cache_roots(model: str) -> list[Path]:
        """Return only storage trees owned by one TTS model."""
        roots: list[Path] = []
        info = MODEL_SETUP.get(model, {})
        weights_dir = info.get("weights_dir")
        if weights_dir:
            roots.append(MODELS_DIR / weights_dir)
        for extra_dir in info.get("extra_weights_dirs", ()):
            roots.append(MODELS_DIR / str(extra_dir))
        weights_repo = info.get("weights_repo")
        if weights_repo:
            roots.append(
                MODELS_DIR / "hub" /
                f"models--{str(weights_repo).replace('/', '--')}"
            )
        override = MODEL_OVERRIDE_MAP.get(model)
        if override:
            roots.append(OVERRIDES_DIR / override)
        if model == "whisper":
            roots.append(MODELS_DIR / "whisper")
        return roots

    def reclaim_model_file_cache(
        self,
        model: str,
        mapped_files: set[Path] | None = None,
    ) -> dict:
        """Advise Linux to evict clean file cache owned by one model.

        This deliberately avoids /proc/sys/vm/drop_caches, which is VM-wide
        under WSL and can evict cache belonging to unrelated Linbox apps.
        Only app-owned files that were mapped by the worker, or files of at
        least 1 MiB under this model's own storage roots, are considered.
        """
        candidates: set[Path] = set(mapped_files or ())
        roots = self._model_cache_roots(model)
        max_files = 4096
        min_tree_file_bytes = 1024 * 1024
        for root in roots:
            if len(candidates) >= max_files:
                break
            try:
                if root.is_file():
                    candidates.add(root)
                    continue
                if not root.is_dir():
                    continue
                for base, _dirs, files in os.walk(root):
                    for name in files:
                        path = Path(base) / name
                        try:
                            if path.stat().st_size >= min_tree_file_bytes:
                                candidates.add(path)
                        except OSError:
                            continue
                        if len(candidates) >= max_files:
                            break
                    if len(candidates) >= max_files:
                        break
            except OSError:
                continue

        advised = 0
        advised_bytes = 0
        errors = 0
        synced = 0
        sync_errors = 0
        advise = getattr(os, "posix_fadvise", None)
        advice = getattr(os, "POSIX_FADV_DONTNEED", None)
        if advise is not None and advice is not None:
            for path in candidates:
                try:
                    size = path.stat().st_size
                    close_on_exec = getattr(os, "O_CLOEXEC", 0)
                    # Fresh model downloads can still have dirty pages after
                    # the writer closes. DONTNEED is only effective for clean
                    # pages, so flush this app-owned file first. Opening it
                    # read/write is required by fsync on Linux; fall back to a
                    # read-only descriptor when permissions do not allow that.
                    can_sync = True
                    try:
                        fd = os.open(path, os.O_RDWR | close_on_exec)
                    except OSError:
                        can_sync = False
                        fd = os.open(path, os.O_RDONLY | close_on_exec)
                    try:
                        if can_sync:
                            try:
                                os.fsync(fd)
                                synced += 1
                            except OSError:
                                sync_errors += 1
                        advise(fd, 0, 0, advice)
                    finally:
                        os.close(fd)
                    advised += 1
                    advised_bytes += size
                except OSError:
                    errors += 1

        result = {
            "strategy": "app_file_fadvise",
            "model": model,
            "files_advised": advised,
            "bytes_advised": advised_bytes,
            "errors": errors,
            "files_synced": synced,
            "sync_errors": sync_errors,
            "vm_wide_drop_caches": False,
        }
        logger.info("Targeted file-cache reclaim for %s: %s", model, result)
        return result

    def last_reclaim(self, model: str) -> dict | None:
        value = self._last_reclaim.get(model)
        return dict(value) if value else None

    async def _force_kill(self, worker: WorkerInfo) -> None:
        """Terminate the worker subprocess and all its children. No orphans."""
        pid = worker.process.pid if worker.process else None
        # Prefer the pgid captured at spawn time so a dead main pid can never
        # prevent group termination. Fall back to a live getpgid, then pid.
        saved_pgid = worker.pgid
        if worker.process:
            try:
                # Kill entire process group (catches child processes such as
                # vLLM/voxtral engine subprocesses that outlive the main pid).
                killed_group = False
                if saved_pgid is not None:
                    try:
                        os.killpg(saved_pgid, 9)
                        killed_group = True
                    except (OSError, ProcessLookupError):
                        pass
                if not killed_group:
                    try:
                        os.killpg(os.getpgid(pid), 9)
                        killed_group = True
                    except (OSError, ProcessLookupError):
                        pass
                if not killed_group:
                    # Last resort: only reaches the main pid, not the group.
                    worker.process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(worker.process.wait), timeout=5
                    )
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    pass  # already sent SIGKILL above
            except Exception as e:
                logger.warning("Error killing worker %s (pid=%s): %s",
                               worker.worker_id, pid, e)
            worker.process = None
        # Close the log file handle
        if worker.log_fh:
            try:
                worker.log_fh.close()
            except Exception:
                pass
            worker.log_fh = None
        # Verify the whole group is gone, not just the main pid. killpg(pgid, 0)
        # raises ProcessLookupError when no group members remain.
        if saved_pgid is not None:
            try:
                os.killpg(saved_pgid, 0)
                logger.warning("Worker %s group %d still has members after kill — "
                               "re-sending SIGKILL", worker.worker_id, saved_pgid)
                os.killpg(saved_pgid, 9)
            except (OSError, ProcessLookupError):
                pass  # confirmed empty
        elif pid:
            try:
                os.kill(pid, 0)  # signal 0 = check if alive
                logger.warning("Worker pid %d still alive after kill — sending SIGKILL", pid)
                os.kill(pid, 9)
            except (OSError, ProcessLookupError):
                pass  # confirmed dead

    async def kill_all_workers(self) -> int:
        """Kill all active workers in parallel. Returns count killed."""
        workers = self.registry.all_workers()
        if not workers:
            return 0
        results = await asyncio.gather(
            *(self.kill_worker(w.worker_id) for w in workers),
            return_exceptions=True,
        )
        killed = sum(1 for r in results if r is True)
        if killed:
            self._reclaim_memory()
        return killed

    @staticmethod
    def _reclaim_memory():
        """Release arenas owned by the long-lived gateway process."""
        import gc as _gc
        _gc.collect()
        try:
            # Return free arenas held by the long-lived gateway allocator.
            libc = ctypes.CDLL(None)
            trim = getattr(libc, "malloc_trim", None)
            if trim is not None:
                trim(0)
        except (OSError, AttributeError):
            pass

    @staticmethod
    def _pid_matches_this_app_worker(pid: int) -> bool:
        """Match this app's workers or engine children carrying its marker."""
        expected_script = (BASE_DIR / "tts_worker.py").resolve()

        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
            marker = b"TTS_SERVER_WORKER_SCRIPT="
            for item in environ.split(b"\0"):
                if item.startswith(marker):
                    value = item[len(marker):].decode(errors="ignore")
                    return Path(value).resolve() == expected_script
        except (FileNotFoundError, OSError):
            pass

        try:
            raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            args = [a.decode(errors="ignore") for a in raw_cmdline.split(b"\0") if a]
        except (FileNotFoundError, OSError):
            return False

        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            cwd = BASE_DIR

        for arg in args:
            if not arg.endswith("tts_worker.py"):
                continue
            candidate = Path(arg)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                if candidate.resolve() == expected_script:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def kill_orphan_workers():
        """Find and kill worker processes left over from a previous crash.

        Only kills processes whose parent is init (pid 1, indicating they were
        orphaned when the previous gateway died) or our own pid. Leaves workers
        belonging to other gateway instances alone.
        """
        try:
            my_pid = os.getpid()
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == my_pid:
                    continue
                # Only kill orphans (parent=1) or our own children
                try:
                    with open(f"/proc/{pid}/status") as f:
                        ppid = None
                        for status_line in f:
                            if status_line.startswith("PPid:"):
                                ppid = int(status_line.split()[1])
                                break
                        if ppid is None or ppid not in (1, my_pid):
                            continue  # unknown parent or belongs to another gateway
                except (FileNotFoundError, ValueError, OSError):
                    continue  # process already gone or unreadable
                if not WorkerManager._pid_matches_this_app_worker(pid):
                    continue
                logger.warning("Killing orphan worker process pid=%d (ppid=%s)", pid, ppid)
                try:
                    pgid = os.getpgid(pid)
                    # Spawned workers own a session; engine children inherit it.
                    # Match the process's worker marker before killing its group.
                    if pgid != os.getpgrp() and os.getsid(pid) == pgid:
                        os.killpg(pgid, 9)
                    else:
                        os.kill(pid, 9)
                except (OSError, ProcessLookupError):
                    pass
        except FileNotFoundError:
            logger.warning("kill_orphan_workers: /proc not available — "
                           "skipping orphan reap")
        except Exception as e:
            logger.warning("kill_orphan_workers failed: %s", e, exc_info=True)

    async def scale_model(self, model: str, count: int,
                          device: str | None = None) -> list[WorkerInfo]:
        """Ensure exactly `count` workers exist for this model on the specified device.

        Args:
            model: Model identifier.
            count: Desired number of workers.
            device: GPU device. If None, uses default.

        Returns:
            List of all workers for this model on this device after scaling.
        """
        device = str(device or WORKER_DEFAULT_DEVICE)

        # Get current workers for this model on this device
        current = [w for w in self.registry.workers_for_model(model)
                   if w.device == device and w.status != "dead"]
        current_count = len(current)

        if current_count < count:
            # Spawn more
            for _ in range(count - current_count):
                await self.spawn_worker(model, device)
        elif current_count > count:
            # Kill excess (kill most recent first)
            to_kill = current[count:]
            for w in to_kill:
                await self.kill_worker(w.worker_id)

        return [w for w in self.registry.workers_for_model(model)
                if w.device == device and w.status != "dead"]

    async def _cleanup_dead_worker(self, worker: WorkerInfo) -> None:
        """Clean up a dead worker: terminate process, release port, unregister."""
        logger.info("Cleaning up dead worker %s (port=%d)", worker.worker_id, worker.port)
        await self._force_kill(worker)
        # Release the port ONLY if this call is the one that removed the worker,
        # so a concurrent cleanup path (e.g. _finalize_worker) cannot release the
        # same port twice and re-add a port a new worker may already own.
        removed = self.registry.unregister(worker.worker_id)
        if removed is not None:
            self.registry.release_port(removed.port)

    async def health_check_loop(self) -> None:
        """Periodic health check for all workers. Runs as an asyncio task."""
        logger.info("Worker health check loop started (interval=%ds)",
                    WORKER_HEALTH_INTERVAL)

        consecutive_failures = 0
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                try:
                    await asyncio.sleep(WORKER_HEALTH_INTERVAL)
                    workers = self.registry.all_workers()
                    to_cleanup = []

                    for w in workers:
                        if w.status == "dead":
                            to_cleanup.append(w)
                            continue
                        if w.status in ("starting", "loading"):
                            continue
                        if w.status == "busy":
                            # Still check if process crashed while busy
                            if w.process and w.process.poll() is not None:
                                logger.warning(
                                    "Worker %s process died while busy (exit code %d)",
                                    w.worker_id, w.process.returncode)
                                to_cleanup.append(w)
                            continue

                        # Check if process is still alive
                        if w.process and w.process.poll() is not None:
                            logger.warning("Worker %s process died (exit code %d)",
                                          w.worker_id, w.process.returncode)
                            to_cleanup.append(w)
                            continue

                        try:
                            resp = await client.get(
                                f"http://127.0.0.1:{w.port}/health"
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                self.registry.update_health(
                                    w.worker_id,
                                    vram_used_mb=data.get("vram_used_mb", 0),
                                    vram_total_mb=data.get("vram_total_mb", 0),
                                    status=data.get("status"),
                                )
                            else:
                                failures = self.registry.record_health_failure(w.worker_id)
                                if failures >= WORKER_MAX_HEALTH_FAILURES:
                                    logger.warning(
                                        "Worker %s failed %d health checks, marking dead",
                                        w.worker_id, failures)
                                    to_cleanup.append(w)
                        except Exception:
                            failures = self.registry.record_health_failure(w.worker_id)
                            if failures >= WORKER_MAX_HEALTH_FAILURES:
                                logger.warning(
                                    "Worker %s unreachable %d times, marking dead",
                                    w.worker_id, failures)
                                to_cleanup.append(w)

                    for w in to_cleanup:
                        # Isolate each cleanup so one failing worker does not
                        # abort cleanup of the others in this cycle.
                        try:
                            await self._cleanup_dead_worker(w)
                        except Exception as e:
                            logger.error("Failed to clean up dead worker %s: %s",
                                         w.worker_id, e, exc_info=True)

                    consecutive_failures = 0  # clean iteration

                except asyncio.CancelledError:
                    logger.info("Health check loop cancelled")
                    break
                except Exception as e:
                    # Unexpected fault in registry/loop code. Log with a stack
                    # trace and apply short bounded backoff that escalates once
                    # the fault persists, so it stays visible instead of
                    # silently looping every interval.
                    consecutive_failures += 1
                    logger.error("Health check loop error (#%d): %s",
                                 consecutive_failures, e, exc_info=True)
                    if consecutive_failures >= 3:
                        logger.critical(
                            "Health check loop has failed %d consecutive times — "
                            "dead workers may not be reaped", consecutive_failures)
                    backoff = min(WORKER_HEALTH_INTERVAL, 2 ** min(consecutive_failures, 5))
                    await asyncio.sleep(backoff)

    def start_health_checks(self) -> None:
        """Start the health check background task."""
        if self._health_task is None or self._health_task.done():
            loop = asyncio.get_running_loop()
            self._health_task = loop.create_task(self.health_check_loop())

    def stop_health_checks(self) -> None:
        """Stop the health check background task."""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()

    async def detect_devices_async(self) -> list[dict]:
        """Async wrapper around detect_devices to avoid blocking the event loop."""
        return await asyncio.to_thread(self.detect_devices)

    def detect_devices(self, force_refresh: bool = False) -> list[dict]:
        """Detect available GPU devices using nvidia-smi (cached for 30s).

        Returns list of device dicts with id, name, vram_total_mb, vram_free_mb,
        and list of worker_ids currently on each device.
        """
        now = time.time()
        if (not force_refresh and self._device_cache is not None
                and (now - self._device_cache_time) < self._DEVICE_CACHE_TTL):
            # Refresh worker assignments from cache
            for dev in self._device_cache:
                dev["workers"] = [w.worker_id
                                  for w in self.registry.workers_on_device(dev["id"])]
            return self._device_cache

        devices = []

        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        idx = parts[0]
                        device_id = f"cuda:{idx}"
                        workers_on = [w.worker_id
                                      for w in self.registry.workers_on_device(device_id)]
                        devices.append({
                            "id": device_id,
                            "name": parts[1],
                            "vram_total_mb": int(float(parts[2])),
                            "vram_free_mb": int(float(parts[3])),
                            "workers": workers_on,
                        })
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("nvidia-smi not available: %s", e)

        # Always include CPU
        cpu_workers = [w.worker_id for w in self.registry.workers_on_device("cpu")]
        devices.append({
            "id": "cpu",
            "name": "CPU",
            "vram_total_mb": 0,
            "vram_free_mb": 0,
            "workers": cpu_workers,
        })

        self._device_cache = devices
        self._device_cache_time = now
        return devices
