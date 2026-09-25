# job_manager.py
"""Job tracking, recovery, retry, and cancellation for TTS inference."""

import json
import os
import re
import shutil
import tempfile
import time
import uuid
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

from pathsafe import is_within

logger = logging.getLogger(__name__)


class JobManager:
    """Manages TTS job lifecycle: creation, progress tracking, cancellation, and recovery.

    Job directory structure:
        {jobs_dir}/{job_id}/
            job.json
            chunk_000.wav, chunk_001.wav, ...
            {stem}_final.{format}
    """

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._cancel_flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._job_locks: dict[str, threading.Lock] = {}  # per-job write lock
        self._job_index: dict[str, Path] = {}  # job_id -> job_file path cache
        self._index_built = False
        self._index_lock = threading.Lock()  # protects _job_index dict

    def _job_lock(self, job_id: str) -> threading.Lock:
        """Get or create a per-job write lock for thread-safe job.json updates."""
        with self._lock:
            if job_id not in self._job_locks:
                self._job_locks[job_id] = threading.Lock()
            return self._job_locks[job_id]

    def create_job(self, model: str, text: str, chunks: list[str],
                   params: dict, output_format: str = "wav",
                   sample_rate: int = 24000, stem: str | None = None,
                   job_dir: Path | None = None) -> dict:
        """Create a new job with tracking metadata.

        Args:
            model: Model identifier (xtts, fish, kokoro).
            text: Full original input text.
            chunks: List of text chunks after splitting.
            params: Request parameters dict (saved for recovery).
            output_format: Target audio format.
            sample_rate: Audio sample rate.
            stem: Base name for the final output file.
            job_dir: Override job directory (for save_path jobs).

        Returns:
            The job payload dict.
        """
        job_id = str(uuid.uuid4())

        if job_dir is None:
            job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        if stem is None:
            stem = f"{model}_{int(datetime.now(timezone.utc).timestamp())}"

        job_payload = {
            "job_id": job_id,
            "model": model,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "running",
            "progress": {
                "stage": "queued",
                "message": "Job queued",
                "current": 0,
                "total": len(chunks),
            },
            "input_text": text,
            "total_chunks": len(chunks),
            "chunks_completed": 0,
            "total_duration_sec": None,
            "sample_rate": sample_rate,
            "output_format": output_format,
            "final_file": None,
            "expected_files": [f"chunk_{i:03d}.wav" for i in range(len(chunks))]
                              + [f"{stem}_final.{output_format}"],
            "missing_files": [f"chunk_{i:03d}.wav" for i in range(len(chunks))]
                             + [f"{stem}_final.{output_format}"],
            "chunks": [
                {
                    "index": i,
                    "text": c,
                    "char_length": len(c),
                    "duration_sec": None,
                    "file": f"chunk_{i:03d}.wav",
                    "verification_passed": None,
                    "whisper_transcript": None,
                    "whisper_similarity": None,
                    "processing_error": None,
                }
                for i, c in enumerate(chunks)
            ],
            "parameters": params,
            "stem": stem,
            "failure_reason": None,
            "job_dir": str(job_dir),
        }

        job_file = job_dir / "job.json"
        self._write_job(job_file, job_payload)
        with self._index_lock:
            self._job_index[job_id] = job_file

        # Set up cancellation flag
        with self._lock:
            self._cancel_flags[job_id] = threading.Event()

        logger.info("Created job %s (%s, %d chunks)", job_id, model, len(chunks))
        return job_payload

    def update_progress(
        self,
        job_id: str,
        stage: str,
        message: str | None = None,
        current: int | float | None = None,
        total: int | float | None = None,
    ) -> None:
        """Update coarse job progress for long non-chunk stages."""
        job_file = self._find_job_file(job_id)
        if not job_file:
            return

        with self._job_lock(job_id):
            try:
                job = self._read_job(job_file)
                progress = dict(job.get("progress") or {})
                progress["stage"] = stage
                if message is not None:
                    progress["message"] = message
                if current is not None:
                    progress["current"] = current
                if total is not None:
                    progress["total"] = total
                progress["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                job["progress"] = progress
                self._write_job(job_file, job)
            except Exception as e:
                logger.warning("Failed to update progress for job %s: %s", job_id, e)

    def update_chunk(self, job_id: str, chunk_idx: int, status: str = "success",
                     duration: float | None = None, audio_file: str | None = None,
                     error: str | None = None,
                     whisper_transcript: str | None = None,
                     whisper_similarity: float | None = None,
                     verification_passed: bool | None = None) -> None:
        """Update a single chunk's status in job.json.

        Args:
            job_id: The job UUID.
            chunk_idx: Zero-based chunk index.
            status: 'success' or 'failed'.
            duration: Audio duration in seconds.
            audio_file: Filename of the chunk WAV.
            error: Error message if failed.
            whisper_transcript: Whisper transcription result.
            whisper_similarity: Similarity ratio.
            verification_passed: Whether Whisper verification passed.
        """
        job_file = self._find_job_file(job_id)
        if not job_file:
            return

        with self._job_lock(job_id):
            try:
                job = self._read_job(job_file)
                chunk = job["chunks"][chunk_idx]

                if status == "success":
                    chunk["duration_sec"] = round(duration, 3) if duration else None
                    if verification_passed is not None or chunk.get("whisper_transcript") is None:
                        chunk["verification_passed"] = verification_passed
                    chunk["processing_error"] = None
                    # Store the completed prefix, not merely a success count.
                    # Parallel workers can finish out of order; recovery resumes
                    # from this prefix and must not skip earlier failed chunks.
                    job["chunks_completed"] = self._completed_prefix(job["chunks"])
                    chunk_file_name = f"chunk_{chunk_idx:03d}.wav"
                    if chunk_file_name in job["missing_files"]:
                        job["missing_files"].remove(chunk_file_name)
                    job["progress"] = {
                        "stage": "synthesizing",
                        "message": f"Synthesized chunk {chunk_idx + 1} of {len(job['chunks'])}",
                        "current": job["chunks_completed"],
                        "total": len(job["chunks"]),
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                else:
                    chunk["processing_error"] = error
                    if verification_passed is not None or chunk.get("whisper_transcript") is None:
                        chunk["verification_passed"] = verification_passed
                    job["chunks_completed"] = self._completed_prefix(job["chunks"])

                if whisper_transcript is not None:
                    chunk["whisper_transcript"] = whisper_transcript
                if whisper_similarity is not None:
                    chunk["whisper_similarity"] = round(whisper_similarity, 4)

                self._write_job(job_file, job)
            except Exception as e:
                logger.warning("Failed to update chunk %d of job %s: %s", chunk_idx, job_id, e)

    def complete_job(
        self,
        job_id: str,
        output_file: str,
        total_duration: float,
        srt_info: dict | None = None,
    ) -> bool:
        """Mark a job as completed."""
        job_file = self._find_job_file(job_id)
        if not job_file:
            return False
        output_file = Path(output_file).name
        with self._job_lock(job_id):
            try:
                job = self._read_job(job_file)
                if self.is_cancelled(job_id):
                    self.invalidate_output(job)
                    job["status"] = "cancelled"
                    job["failure_reason"] = "Cancelled by user"
                    self._write_job(job_file, job)
                    return False
                job["status"] = "completed"
                job["progress"] = {
                    "stage": "completed",
                    "message": "Job completed",
                    "current": job.get("total_chunks"),
                    "total": job.get("total_chunks"),
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                job["final_file"] = output_file
                job["output_format"] = Path(output_file).suffix.lstrip(".")
                job["expected_files"] = [c["file"] for c in job["chunks"]] + [output_file]
                job["total_duration_sec"] = round(total_duration, 3)
                if srt_info is not None:
                    job["srt"] = srt_info
                    if srt_info.get("srt_path"):
                        job["srt_path"] = srt_info.get("srt_path")
                    if srt_info.get("timing_path"):
                        job["srt_timing_path"] = srt_info.get("timing_path")
                job["missing_files"] = []
                self._write_job(job_file, job)
                logger.info("Job %s completed: %s (%.1fs)", job_id, output_file, total_duration)
                return True
            except Exception as e:
                logger.warning("Failed to complete job %s: %s", job_id, e)

        return False

    def fail_job(self, job_id: str, reason: str) -> None:
        """Mark a job as failed."""
        job_file = self._find_job_file(job_id)
        if not job_file:
            return
        with self._job_lock(job_id):
            try:
                job = self._read_job(job_file)
                job["status"] = "failed"
                job["progress"] = {
                    "stage": "failed",
                    "message": reason,
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                job["failure_reason"] = reason
                self._write_job(job_file, job)
                logger.warning("Job %s failed: %s", job_id, reason)
            except Exception as e:
                logger.warning("Failed to mark job %s as failed: %s", job_id, e)

    def cancel_job(self, job_id: str, reason: str = "Cancelled by user") -> None:
        """Mark a job as cancelled."""
        job_file = self._find_job_file(job_id)
        if not job_file:
            return
        with self._job_lock(job_id):
            try:
                job = self._read_job(job_file)
                job["status"] = "cancelled"
                job["failure_reason"] = reason
                self._write_job(job_file, job)
                logger.info("Job %s cancelled: %s", job_id, reason)
            except Exception as e:
                logger.warning("Failed to mark job %s as cancelled: %s", job_id, e)

    def recover_job(self, job_dir: str | Path) -> dict | None:
        """Load a failed/incomplete job for re-processing.

        Args:
            job_dir: Path to the job directory containing job.json.

        Returns:
            The job dict with status reset to 'running', or None if not recoverable.
        """
        job_dir = self._validate_job_dir(job_dir)
        if job_dir is None:
            return None
        job_file = job_dir / "job.json"
        if not job_file.exists():
            logger.warning("No job.json found in %s", job_dir)
            return None

        try:
            job = self._read_job(job_file)
        except Exception as e:
            logger.error("job.json corrupted in %s: %s", job_dir, e)
            return None

        start_from = self._first_recovery_chunk(job, job_dir)
        final_file = self._safe_file_name(job.get("final_file"))
        if job.get("final_file") and not final_file:
            logger.warning("Rejected unsafe final_file in %s", job_file)
            job["final_file"] = None
        final_exists = bool(final_file and (job_dir / final_file).exists())
        if start_from >= job.get("total_chunks", 0) and final_exists:
            logger.info("Job in %s is already finished", job_dir)
            return None

        # Reset status for re-processing
        job["status"] = "running"
        job["failure_reason"] = None
        self.invalidate_output(job)
        job["status"] = "running"
        job["chunks_completed"] = start_from

        # Reset every chunk that will be regenerated. This keeps progress and
        # UI state consistent even when parallel jobs completed later chunks
        # before an earlier chunk failed.
        expected_missing = []
        for chunk in job["chunks"][start_from:]:
            chunk["duration_sec"] = None
            chunk["processing_error"] = None
            chunk["verification_passed"] = None
            chunk["whisper_transcript"] = None
            chunk["whisper_similarity"] = None
            safe_chunk_file = self._safe_file_name(chunk.get("file"))
            if safe_chunk_file:
                expected_missing.append(safe_chunk_file)

        stem = job.get("stem") or job_dir.name
        output_format = job.get("output_format", "wav")
        expected_missing.append(f"{stem}_final.{output_format}")
        job["missing_files"] = expected_missing

        self._write_job(job_file, job)

        # Set up cancellation flag
        job_id = job["job_id"]
        with self._lock:
            self._cancel_flags[job_id] = threading.Event()

        logger.info("Recovering job %s from chunk %d/%d",
                    job_id, start_from, job["total_chunks"])
        return job

    def is_cancelled(self, job_id: str) -> bool:
        """Thread-safe check if a job has been cancelled."""
        with self._lock:
            event = self._cancel_flags.get(job_id)
        if event is None:
            return False
        return event.is_set()

    def request_cancel(self, job_id: str) -> bool:
        """Set the cancel flag for a running job.

        Returns:
            True if the job was found and cancel was set, False otherwise.
        """
        with self._job_lock(job_id):
            job = self.get_job(job_id)
            if job is None or job.get("status") != "running":
                return False
            with self._lock:
                event = self._cancel_flags.get(job_id)
            if event is None:
                return False
            event.set()
        logger.info("Cancel requested for job %s", job_id)
        return True

    def get_job(self, job_id: str) -> dict | None:
        """Load job data by ID."""
        job_file = self._find_job_file(job_id)
        if job_file:
            return self._read_job(job_file)
        return None

    def get_job_dir(self, job_id: str) -> Path | None:
        """Return the validated directory containing a job's job.json."""
        job_file = self._find_job_file(job_id)
        if not job_file:
            return None
        return self._validate_job_dir(job_file.parent)

    def list_jobs(self, limit: int | None = 50) -> list[dict]:
        """List recent jobs with summary info.

        Uses the in-memory index cache to avoid full directory scans on every
        poll (frontend polls every 6s).  Falls back to scanning only for the
        first call or after cache invalidation.
        """
        # Rebuild index if empty (first call or after cleanup)
        with self._index_lock:
            if not self._index_built:
                self._rebuild_index()
            # Snapshot the index under lock to avoid iteration-during-mutation
            index_snapshot = list(self._job_index.items())

        # Build summaries from the snapshot (no lock held during I/O)
        jobs = []
        stale = []
        for job_id, job_file in index_snapshot:
            if not job_file.exists():
                stale.append(job_id)
                continue
            try:
                job = self._read_job(job_file)
                jobs.append(self._job_summary(job, str(job_file.parent)))
            except Exception:
                stale.append(job_id)
        if stale:
            with self._index_lock:
                for jid in stale:
                    self._job_index.pop(jid, None)

        # Sort by timestamp descending, limit
        jobs.sort(key=lambda j: j.get("timestamp", ""), reverse=True)
        return jobs[:limit]

    def _rebuild_index(self):
        """Scan job directories once and populate the in-memory index."""
        self._index_built = True
        roots = [self.jobs_dir]
        # Saved project jobs are first-class jobs and must remain addressable
        # by job_id after a gateway restart. Large installations can opt out,
        # but recovery-safe behavior is the default for this local app.
        if os.environ.get("TTS_INDEX_PROJECT_JOBS", "1") == "1":
            try:
                from config import PROJECTS_OUTPUT
                if PROJECTS_OUTPUT.exists():
                    roots.append(PROJECTS_OUTPUT)
            except ImportError:
                pass

        for root in roots:
            if not root.exists():
                continue
            for jf in root.rglob("job.json"):
                if not jf.is_file():
                    continue
                try:
                    data = self._read_job(jf)
                    jid = data.get("job_id")
                    if jid:
                        self._job_index[jid] = jf
                except Exception:
                    pass

    @staticmethod
    def _job_summary(job: dict, job_dir: str = "") -> dict:
        """Extract a summary dict from a full job payload."""
        text = job.get("input_text", "")
        return {
            "job_id": job.get("job_id"),
            "model": job.get("model"),
            "status": job.get("status"),
            "total_chunks": job.get("total_chunks"),
            "chunks_completed": job.get("chunks_completed"),
            "timestamp": job.get("timestamp"),
            "failure_reason": job.get("failure_reason"),
            "text_preview": text[:120] + ("..." if len(text) > 120 else ""),
            "duration_sec": job.get("total_duration_sec"),
            "final_file": job.get("final_file"),
            "output_format": job.get("output_format", "wav"),
            "sample_rate": job.get("sample_rate"),
            "job_dir": job_dir or job.get("job_dir", ""),
        }

    def delete_job(self, job_id: str, force: bool = False) -> bool:
        """Delete a job directory and all its files. Returns True if found and deleted.

        Refuses to delete running jobs unless force=True.
        """
        job_file = self._find_job_file(job_id)
        if not job_file:
            return False
        # Refuse to delete active jobs — cancellation should be used instead
        if not force:
            try:
                job = self._read_job(job_file)
                if job.get("status") == "running":
                    logger.warning("Refusing to delete running job %s — cancel it first", job_id)
                    return False
            except Exception:
                return False
        job_dir = job_file.parent
        try:
            shutil.rmtree(job_dir)
            with self._index_lock:
                self._job_index.pop(job_id, None)
            with self._lock:
                self._cancel_flags.pop(job_id, None)
                self._job_locks.pop(job_id, None)
            logger.info("Deleted job %s (%s)", job_id, job_dir)
            return True
        except Exception as e:
            logger.warning("Failed to delete job %s: %s", job_id, e)
            return False

    def cleanup_cancel_flag(self, job_id: str) -> None:
        """Remove the cancel flag and per-job lock for a completed/failed job."""
        with self._lock:
            self._cancel_flags.pop(job_id, None)
        with self._index_lock:
            # Keep the index entry (it's just a Path, cheap to hold)
            pass

    def _evict_stale_index_entries(self) -> None:
        """Remove index/lock entries for jobs whose directories no longer exist."""
        with self._index_lock:
            stale = [jid for jid, jf in self._job_index.items() if not jf.exists()]
            for jid in stale:
                del self._job_index[jid]
        with self._lock:
            for jid in stale:
                self._cancel_flags.pop(jid, None)
                self._job_locks.pop(jid, None)

    # --- Internal helpers ---

    # Delegate to the shared path-containment primitive.
    _path_is_relative_to = staticmethod(is_within)

    @staticmethod
    def _safe_file_name(value: str | None) -> str | None:
        if not value or "\x00" in value:
            return None
        p = Path(value)
        if p.name != value or p.is_absolute() or "/" in value or "\\" in value:
            return None
        return value

    def _validate_job_dir(self, job_dir: str | Path) -> Path | None:
        try:
            resolved = Path(job_dir).expanduser().resolve()
        except (OSError, ValueError):
            return None
        roots = [self.jobs_dir.resolve()]
        try:
            from config import OUTPUT_DIR, PROJECTS_OUTPUT
            roots.extend([OUTPUT_DIR.resolve(), PROJECTS_OUTPUT.resolve()])
        except ImportError:
            pass
        if any(resolved == root or self._path_is_relative_to(resolved, root) for root in roots):
            return resolved
        logger.warning("Rejected job directory outside allowed roots: %s", job_dir)
        return None

    @staticmethod
    def _chunk_succeeded(chunk: dict) -> bool:
        return (
            chunk.get("duration_sec") is not None
            and chunk.get("processing_error") is None
            and chunk.get("verification_passed") is not False
        )

    @classmethod
    def _completed_prefix(cls, chunks: list[dict]) -> int:
        completed = 0
        for chunk in chunks:
            if not cls._chunk_succeeded(chunk):
                break
            completed += 1
        return completed

    @classmethod
    def _first_recovery_chunk(cls, job: dict, job_dir: Path) -> int:
        chunks = job.get("chunks", [])
        for i, chunk in enumerate(chunks):
            chunk_name = cls._safe_file_name(chunk.get("file")) or f"chunk_{i:03d}.wav"
            chunk_file = job_dir / chunk_name
            if not cls._chunk_succeeded(chunk) or not chunk_file.exists():
                return i
        return len(chunks)

    # Job IDs are server-generated tokens (timestamp + uuid hex / slug). Reject
    # anything outside this character set so a crafted job_id (e.g. containing
    # '..', a path separator, or NUL) cannot escape jobs_dir or poison the index.
    _JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

    def _find_job_file(self, job_id: str) -> Path | None:
        """Find job.json by job_id, using index cache then searching directories."""
        # Validate the job_id format up front (defense-in-depth path confinement).
        if not job_id or not self._JOB_ID_RE.match(job_id) or job_id in (".", ".."):
            logger.warning("Rejected malformed job_id: %r", job_id)
            return None

        # Check index cache first
        with self._index_lock:
            cached = self._job_index.get(job_id)
        if cached and cached.exists():
            return cached

        # Direct path by ID. Confine to jobs_dir before trusting it, so even a
        # job_id that slipped past validation cannot resolve outside the root.
        direct = self.jobs_dir / job_id / "job.json"
        if direct.exists() and self._path_is_relative_to(direct, self.jobs_dir):
            with self._index_lock:
                self._job_index[job_id] = direct
            return direct

        with self._index_lock:
            if not self._index_built:
                self._rebuild_index()
            return self._job_index.get(job_id)

    @staticmethod
    def invalidate_output(job: dict) -> None:
        """Stop exposing outputs and captions that describe an earlier revision."""
        for key in ("final_file", "srt", "srt_path", "srt_timing_path", "total_duration_sec"):
            job.pop(key, None)
        job["status"] = "incomplete"


    @staticmethod
    def _read_job(job_file: Path) -> dict:
        with open(job_file, "r", encoding="utf-8") as f:
            job = json.load(f)
        # The manifest travels with its directory. Saved reference snapshots
        # must follow that directory even when the original copy still exists.
        old_dir = job.get("job_dir")
        current_dir = job_file.parent
        if isinstance(old_dir, str) and old_dir and Path(old_dir) != current_dir:
            mappings = [(Path(old_dir), current_dir)]
            # Older manifests can reference the shared voices folder instead
            # of a job-local snapshot. Infer the former app root only when the
            # job's complete relative location has stayed the same.
            try:
                from config import APP_DIR
                relative_job = current_dir.relative_to(APP_DIR)
                count = len(relative_job.parts)
                old_path = Path(old_dir)
                if count and old_path.parts[-count:] == relative_job.parts:
                    old_root = old_path.parents[count - 1]
                    for name in ("voices", "output", "projects_output"):
                        mappings.append((old_root / name, APP_DIR / name))
            except (ImportError, ValueError, IndexError):
                pass
            def relocate(value):
                if not isinstance(value, str):
                    return value
                for before, after in mappings:
                    try:
                        relative = Path(value).relative_to(before)
                    except ValueError:
                        continue
                    candidate = after / relative
                    if not is_within(candidate, after):
                        raise ValueError("Saved reference escapes its relocated job directory")
                    return str(candidate)
                return value  # Built-in voice IDs and external exports stay literal.
            params = job.get("parameters")
            if isinstance(params, dict):
                for key in ("voice", "reference_audio"):
                    if key in params:
                        params[key] = relocate(params[key])
                if isinstance(params.get("reference_audios"), list):
                    params["reference_audios"] = [relocate(value) for value in params["reference_audios"]]
        job["job_dir"] = str(current_dir)
        return job

    @staticmethod
    def _write_job(job_file: Path, data: dict) -> None:
        """Write job data atomically via temp file + rename."""
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(job_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                # Force the temp file's bytes to disk before the rename so a
                # crash/power loss cannot leave job.json pointing at a
                # zero-length or partially-flushed file.
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(job_file))
            # On POSIX, also fsync the parent directory so the rename itself is
            # durable. Guarded for platforms (e.g. Windows) that cannot open or
            # fsync a directory.
            try:
                dir_fd = os.open(str(job_file.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (OSError, AttributeError):
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def recover_stale_running_jobs(self) -> int:
        """Mark any jobs stuck in 'running' as 'failed'.

        Called on gateway startup — if a job is still 'running', the previous
        gateway process died mid-pipeline and the job will never complete.
        Scans both temporary and saved project jobs by default. Deep project
        recovery can be disabled for unusually large libraries with
        ``TTS_RECOVER_PROJECT_JOBS_ON_STARTUP=0``.
        """
        roots = [self.jobs_dir]
        if os.environ.get("TTS_RECOVER_PROJECT_JOBS_ON_STARTUP", "1") == "1":
            try:
                from config import PROJECTS_OUTPUT
                if PROJECTS_OUTPUT.exists():
                    roots.append(PROJECTS_OUTPUT)
            except ImportError:
                pass

        recovered = 0
        for root in roots:
            if not root.exists():
                continue
            for jf in root.rglob("job.json"):
                if not jf.is_file():
                    continue
                try:
                    job = self._read_job(jf)
                    if job.get("status") == "running":
                        job["status"] = "failed"
                        job["failure_reason"] = "Gateway restarted — job was interrupted"
                        self._write_job(jf, job)
                        logger.warning("Marked stale job %s as failed (was running at shutdown)",
                                       job.get("job_id", jf.parent.name))
                        recovered += 1
                except Exception:
                    pass
        if recovered:
            logger.info("Recovered %d stale running job(s) on startup", recovered)
        return recovered

    def cleanup_old_jobs(self, max_age_hours: int = 72) -> int:
        """Remove auto-generated temp job directories older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0
        if not self.jobs_dir.exists():
            return removed
        for d in list(self.jobs_dir.iterdir()):
            if not d.is_dir() or not d.name.startswith("temp_"):
                continue  # Only clean up auto-generated temp jobs
            try:
                job = self._read_job(d / "job.json")
                if job.get("status") == "running":
                    continue
                if d.stat().st_mtime < cutoff and self.delete_job(job["job_id"]):
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info("Cleaned up %d old temp jobs (>%dh)", removed, max_age_hours)
        # Evict stale index/lock entries for deleted job dirs
        self._evict_stale_index_entries()
        return removed
