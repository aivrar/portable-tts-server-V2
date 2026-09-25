"""Cross-app local discovery registry.

TTS Server publishes a JSON file describing its endpoints, auth, and PID into
an explicitly configured, application-owned registry. It sets
APPHUB_REGISTRY_DIR to E:/tts_server/output/run/registry so starting the WSL
service never writes discovery metadata or tokens to C:. If that location is
not writable, callers may supply another app-owned E: fallback directory.

Public API:
    publish(name, *, endpoints, auth=None, app_dir=None, version=None,
            extra=None, fallback_dir=None) -> Path
    unpublish(name, *, fallback_dir=None, expected_pid=None,
              expected_token=None) -> bool
    read(name, *, fallback_dir=None, check_alive=True, gc=False) -> dict | None
    list_peers(*, fallback_dir=None, check_alive=True, gc=False) -> list[dict]
    registry_dir(*, fallback_dir=None) -> Path

A registry entry is considered stale when its PID is no longer running.
Stale entries are filtered out of read()/list_peers() results by default, but
their files are left on disk unless gc=True is passed (so a plain read never
deletes another app's entry). The owning process removes its own entry via
unpublish().

The schema written to each file:
    {
      "name": str,                  # app identifier (filename stem)
      "version": str | None,
      "pid": int,
      "started_at": str,            # ISO 8601 UTC
      "app_dir": str | None,        # absolute path to the app install dir
      "endpoints": dict[str, str],  # named URLs (e.g. "api", "gateway")
      "auth": {                     # optional, omit for unauthenticated apps
        "header": str,              # HTTP header name to send
        "token": str
      } | null,
      "extra": dict | None          # any app-specific extension fields
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

__all__ = [
    "publish",
    "unpublish",
    "read",
    "list_peers",
    "wait_for_peer",
    "registry_dir",
    "RegistryError",
]

# Allowed app names: alphanumeric + underscore + hyphen + dot, must start
# with alphanum or underscore, length 1-64. Rejects path separators, control
# chars, leading dots, and Windows reserved patterns.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class RegistryError(Exception):
    """Raised when the registry cannot be read or written."""


# One-time guard so the DrvFs-token warning is emitted at most once per process.
_DRVFS_TOKEN_WARNED = False


def _path_on_drvfs(path: Path) -> bool:
    """Best-effort check that `path` is on a Windows-mounted /mnt/<drive> path.

    On those DrvFs/9p mounts POSIX mode bits (chmod 0o600) are not enforced by
    the underlying NTFS, so a secret-bearing file there is effectively readable
    by any local user. Falls back to a simple /mnt/ prefix test.
    """
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    return resolved.startswith("/mnt/")


def _warn_token_drvfs_once(path: Path) -> None:
    global _DRVFS_TOKEN_WARNED
    if _DRVFS_TOKEN_WARNED:
        return
    _DRVFS_TOKEN_WARNED = True
    _log.warning(
        "Registry entry %s contains an auth token and lives on a Windows DrvFs "
        "mount where chmod 0600 is NOT enforced; the token is effectively "
        "readable by any local user with access to that directory.", path,
    )


def _is_wsl() -> bool:
    """Return True when running inside a WSL/WSL2 Linux distro."""
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except (OSError, UnicodeDecodeError):
        return False


def _system_registry_dir() -> Path | None:
    """Return only the explicitly configured portable registry directory.

    This TTS build fails closed: it never falls back to LOCALAPPDATA or another
    OS profile directory. Callers without APPHUB_REGISTRY_DIR must supply their
    app-owned E: fallback directory to registry_dir().
    """
    configured = os.environ.get("APPHUB_REGISTRY_DIR", "").strip()
    if not configured:
        return None
    path = Path(configured)

    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return None


def registry_dir(*, fallback_dir: str | os.PathLike | None = None) -> Path:
    """Return the active registry directory.

    Tries the OS-standard location first; falls back to fallback_dir if the
    standard path is unavailable. Raises RegistryError if neither works.
    """
    sysdir = _system_registry_dir()
    if sysdir is not None:
        return sysdir
    if fallback_dir is None:
        raise RegistryError("No writable registry dir; pass fallback_dir.")
    fb = Path(fallback_dir)
    try:
        fb.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RegistryError(f"Cannot create fallback registry dir {fb}: {e}") from e
    return fb


def _safe_name(name: str) -> str:
    """Reject names that would escape the registry dir or collide on Windows."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RegistryError(
            f"Invalid registry name: {name!r} "
            "(use alphanumeric, underscore, hyphen, dot; 1-64 chars; "
            "must start with alphanum or underscore)"
        )
    if name.upper() in _WINDOWS_RESERVED:
        raise RegistryError(f"Name conflicts with Windows reserved device: {name!r}")
    return name


def _registry_file(name: str, fallback_dir: str | os.PathLike | None) -> Path:
    return registry_dir(fallback_dir=fallback_dir) / f"{_safe_name(name)}.json"


def _sweep_stale_tmp(directory: Path) -> None:
    """Best-effort cleanup of orphan .tmp files from interrupted writes."""
    try:
        cutoff = time.time() - 3600  # 1h old
        for f in directory.glob("*.tmp"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid: int) -> bool:
    """Return True if PID is currently a live process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            ERROR_ACCESS_DENIED = 5
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                err = ctypes.get_last_error() or kernel32.GetLastError()
                # ACCESS_DENIED means the PID exists but is owned by another
                # user / privileged process — treat as alive.
                if err == ERROR_ACCESS_DENIED:
                    return True
                # INVALID_PARAMETER (or other) generally means the PID is gone.
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True  # query failed but process exists
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # if we can't tell, don't drop the entry
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists, we just can't signal it
        except OSError:
            return True


def publish(
    name: str,
    *,
    endpoints: dict[str, str],
    auth: dict | None = None,
    app_dir: str | os.PathLike | None = None,
    version: str | None = None,
    extra: dict | None = None,
    fallback_dir: str | os.PathLike | None = None,
) -> Path:
    """Publish this app's registry entry. Returns the file path.

    Args:
        name: Stable app identifier (e.g. "tts_server", "orchestrator").
        endpoints: Named URLs the app exposes, e.g. {"api": "http://...", "gateway": "..."}.
        auth: Optional {"header": "...", "token": "..."} for token auth.
        app_dir: Filesystem path where the app is installed.
        version: App version string.
        extra: Any extra app-specific fields.
        fallback_dir: App-owned E: directory used when no override is configured.
    """
    if not isinstance(endpoints, dict) or not endpoints:
        raise RegistryError("endpoints must be a non-empty dict")

    payload = {
        "name": _safe_name(name),
        "version": version,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_dir": str(Path(app_dir).resolve()) if app_dir else None,
        "endpoints": {str(k): str(v) for k, v in endpoints.items()},
        "auth": dict(auth) if auth else None,
        "extra": dict(extra) if extra else None,
    }
    path = _registry_file(name, fallback_dir)
    _sweep_stale_tmp(path.parent)
    _atomic_write_json(path, payload)
    try:
        if sys.platform != "win32":
            os.chmod(path, 0o600)  # token in file — don't expose to other users
            # Under WSL the portable registry lives on E: via a /mnt/<drive>
            # DrvFs/9p mount where POSIX mode bits are NOT
            # enforced by NTFS — the 0o600 above is effectively a no-op and the
            # file (including auth.token) inherits the parent NTFS ACL, so it is
            # readable by any local user with access to this app tree. This is
            # the same trust boundary as output/run/api_token; emit a one-time
            # warning so operators do not mistake chmod for an NTFS ACL.
            if auth and auth.get("token") and _is_wsl() and _path_on_drvfs(path):
                _warn_token_drvfs_once(path)
    except OSError:
        pass
    return path


def unpublish(
    name: str,
    *,
    fallback_dir: str | os.PathLike | None = None,
    expected_pid: int | None = None,
    expected_token: str | None = None,
) -> bool:
    """Remove this app's registry entry. Returns True if a file was removed.

    `expected_pid` / `expected_token` protect against a stale instance erasing
    the registry entry that a newer instance already published: when either is
    provided, the on-disk entry is only unlinked if its stored pid (resp.
    auth.token) matches. Defaults of None preserve the previous unconditional
    behavior.
    """
    try:
        path = _registry_file(name, fallback_dir)
    except RegistryError:
        return False
    try:
        if expected_pid is not None or expected_token is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return False
            if expected_pid is not None and data.get("pid") != expected_pid:
                return False
            if expected_token is not None:
                auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
                if auth.get("token") != expected_token:
                    return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _read_one(path: Path, *, check_alive: bool, gc: bool = False) -> dict | None:
    """Read one registry file. Returns None if missing, corrupt, or stale.

    When `check_alive` is set, an entry whose pid is not alive is filtered out
    (returns None). The file is only unlinked when `gc=True` — a plain read must
    never delete another app's published entry from the shared registry (a
    transient liveness misfire would otherwise permanently remove a healthy
    peer). Stale-entry deletion is reserved for an explicit GC pass / the owning
    unpublish.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if check_alive and isinstance(pid, int) and not _pid_alive(pid):
        if gc:
            # Explicit GC opt-in only — clean up the stale entry, best-effort.
            try:
                path.unlink()
            except OSError:
                pass
        return None
    # Annotate with the file path so callers can locate it
    data["_file"] = str(path)
    return data


def read(
    name: str,
    *,
    fallback_dir: str | os.PathLike | None = None,
    check_alive: bool = True,
    gc: bool = False,
) -> dict | None:
    """Read a single peer's registry entry by name.

    Returns the entry dict (with extra "_file" key for the source path), or
    None if the peer is not running / not registered. By default this is a pure
    read: a stale entry is filtered out but its file is left on disk. Pass
    `gc=True` to also unlink stale entries (opt-in garbage collection).
    """
    try:
        path = _registry_file(name, fallback_dir)
    except RegistryError:
        return None
    return _read_one(path, check_alive=check_alive, gc=gc)


def list_peers(
    *,
    fallback_dir: str | os.PathLike | None = None,
    check_alive: bool = True,
    gc: bool = False,
) -> list[dict]:
    """List every live peer in the registry.

    By default this is a pure read: dead entries are filtered from the returned
    list but their files are not deleted (so a transient liveness misfire never
    removes another app's published entry from the shared registry). Pass
    `gc=True` to also reap stale entries.
    """
    try:
        d = registry_dir(fallback_dir=fallback_dir)
    except RegistryError:
        return []
    # Reap orphan *.tmp files (from a write interrupted between mkstemp and
    # os.replace) here too, so read-mostly peers (CLI, viewers) that never call
    # publish() don't let them accumulate. The 1h mtime cutoff inside guards
    # against racing a concurrent in-progress write.
    _sweep_stale_tmp(d)
    peers: list[dict] = []
    for p in sorted(d.glob("*.json")):
        entry = _read_one(p, check_alive=check_alive, gc=gc)
        if entry:
            peers.append(entry)
    return peers


# Convenience: a wait helper (kept tiny, no asyncio dependency)
def wait_for_peer(
    name: str,
    timeout: float = 30.0,
    interval: float = 0.5,
    *,
    fallback_dir: str | os.PathLike | None = None,
) -> dict | None:
    """Block until a peer with the given name appears or timeout elapses."""
    deadline = time.monotonic() + timeout
    while True:
        entry = read(name, fallback_dir=fallback_dir)
        if entry is not None:
            return entry
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
