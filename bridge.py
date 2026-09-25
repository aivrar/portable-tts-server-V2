"""Proxy the Windows WebView and CLI to the TTS gateway inside this WSL distro.

The native launcher starts this bridge in WSL and opens its URL in WebView2.
The bridge forwards requests to the gateway on localhost:8300 in the same
distro. The bridge starts and supervises the gateway for desktop and headless use.
"""

import atexit
import re
import secrets
import http.server
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

LINUX_STATE_ROOT = "/opt/tts_server"

try:
    IN_WSL = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or "microsoft" in os.uname().release.lower()
except AttributeError:
    IN_WSL = False  # os.uname() unavailable on native Windows


def _refuse_unsafe_launch(message):
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(78)

PORT = int(os.environ.get("BRIDGE_PORT", 9300))
TTS_PORT = int(os.environ.get("TTS_PORT", 8300))
TTS_URL = f"http://localhost:{TTS_PORT}"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
EXPECTED_APP_DIR = APP_DIR

if IN_WSL:
    from pathlib import Path
    from server.portable_runtime import validate_app_binding
    try:
        validate_app_binding(Path(APP_DIR))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        _refuse_unsafe_launch(f"Unsafe TTS bridge launch refused: {exc}")

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(APP_DIR, "output"))
RUN_DIR = os.environ.get("RUN_DIR", os.path.join(OUTPUT_DIR, "run"))
# Cache lives inside the WSL ext4 filesystem to match setup.sh / config.py.
# Falls back to the local /tmp-style path on bare Windows where /opt doesn't
# exist (bridge can run standalone for development).
_DEFAULT_CACHE = "/opt/tts_server/cache" if os.path.isdir("/opt/tts_server") else os.path.join(APP_DIR, "cache")
CACHE_DIR = os.environ.get("CACHE_DIR", _DEFAULT_CACHE)
PORTABLE_EXEC_PATH = ":".join((
    "/opt/tts_server/venv/bin", "/usr/local/cuda/bin", "/usr/lib/wsl/lib",
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
    "/sbin", "/bin",
))

if IN_WSL:
    def _within(path, root):
        try:
            return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
        except (OSError, ValueError):
            return False

    for _name, _path, _root in (
        ("OUTPUT_DIR", OUTPUT_DIR, EXPECTED_APP_DIR),
        ("RUN_DIR", RUN_DIR, EXPECTED_APP_DIR),
        ("CACHE_DIR", CACHE_DIR, LINUX_STATE_ROOT),
    ):
        if not _within(_path, _root):
            _refuse_unsafe_launch(
                f"Unsafe TTS bridge launch refused: {_name} escapes {_root}: {_path}"
            )

    # Do not let WSL's Windows-appended PATH resolve missing dependencies from
    # C: or another app. Native Windows development runs keep their own PATH.
    os.environ["PATH"] = PORTABLE_EXEC_PATH

os.makedirs(RUN_DIR, exist_ok=True)
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    for _cache_child in (
        "home", "xdg-cache", "xdg-data", "xdg-config", "xdg-state", "tmp",
        "pip", "numba", "triton", "cuda", "torchinductor", "matplotlib",
    ):
        os.makedirs(os.path.join(CACHE_DIR, _cache_child), exist_ok=True)
except OSError:
    # Bridge isn't authoritative for cache layout â€” gateway will fix on start.
    pass

# The Windows launcher reaches this process through WSL's localhost forwarding.
# In WSL, binding only to 127.0.0.1 keeps the proxy inside the VM and makes the
# launcher think the UI server failed to start. Bind broadly in WSL by default,
# while native Windows runs stay loopback-only. Operators can still override the
# address with TTS_BRIDGE_BIND_ADDR, for example 127.0.0.1 or 0.0.0.0.
_BIND_OVERRIDE = os.environ.get("TTS_BRIDGE_BIND_ADDR", "").strip()
BIND_ADDR = _BIND_OVERRIDE if _BIND_OVERRIDE else ("0.0.0.0" if IN_WSL else "127.0.0.1")

# ---- Debug log ----
LOG_FILE = os.path.join(RUN_DIR, "tts_server_debug.log")
_LOG_MAX_BYTES = max(
    1024 * 1024,
    int(os.environ.get("TTS_LOG_MAX_BYTES", 10 * 1024 * 1024)),
)
_LOG_BACKUP_COUNT = max(
    1,
    min(10, int(os.environ.get("TTS_LOG_BACKUP_COUNT", 3))),
)

_log_fh = None
_log_lock = threading.RLock()


def _rotate_log_file(path):
    """Rotate a persistent log before opening it when it exceeds the cap."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < _LOG_MAX_BYTES:
            return
        oldest = f"{path}.{_LOG_BACKUP_COUNT}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(_LOG_BACKUP_COUNT - 1, 0, -1):
            src = f"{path}.{index}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{index + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        # Diagnostics must never prevent the local app from starting.
        pass


def log(msg):
    with _log_lock:
        _write_log(msg)


def _write_log(msg):
    global _log_fh
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        if _log_fh is not None and _log_fh.tell() >= _LOG_MAX_BYTES:
            _close_log()
        if _log_fh is None:
            _rotate_log_file(LOG_FILE)
            _log_fh = open(LOG_FILE, "a")
        _log_fh.write(line + "\n")
        _log_fh.flush()
    except Exception:
        pass


def _close_log():
    """Close the debug log file handle."""
    global _log_fh
    if _log_fh:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None


def _check_tts_alive(timeout: float = 2.0) -> bool:
    """Return True if the TTS server is responding on its port."""
    try:
        r = urllib.request.urlopen(f"{TTS_URL}/health", timeout=timeout)
        try:
            return r.status == 200
        finally:
            r.close()
    except Exception:
        return False


# ============================================================
# Proxy handler
# ============================================================
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Proxies requests to the TTS server running on localhost."""

    # Max request body size: 100 MB
    _MAX_BODY = 100 * 1024 * 1024

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    # Allowed path prefixes for proxying
    _ALLOWED_PREFIXES = ("/api/", "/health", "/static/", "/docs", "/openapi.json")
    _ALLOWED_ORIGINS = {
        f"http://localhost:{PORT}",
        f"http://127.0.0.1:{PORT}",
    }

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin in self._ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_GET(self):
        if self.path == "/bridge-health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"bridge":true}')
            return
        if self.path == "/api/status":
            alive = _check_tts_alive()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok" if alive else "degraded",
                "tts_server": "connected" if alive else "unreachable",
            }).encode())
            return
        if self.path.split("?", 1)[0] == "/" and not _check_tts_alive(timeout=0.25):
            _send_backend_starting_page(self)
            return
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-TTS-API-Token")
        self.end_headers()

    def _read_chunked_body(self):
        """De-chunk a Transfer-Encoding: chunked request body.

        Returns the assembled bytes (possibly b"" for an empty body), or None
        after already sending an error response (400 on malformed framing, 413
        if the body exceeds _MAX_BODY). None is reserved for the error sentinel.
        """
        chunks = []
        total = 0
        try:
            while True:
                size_line = self.rfile.readline(65536)
                if not size_line:
                    raise ValueError("Premature EOF")
                # Chunk size is hex; strip any ";ext" chunk extensions.
                size_str = size_line.split(b";", 1)[0].strip()
                if not re.fullmatch(rb"[0-9a-fA-F]+", size_str) or not size_line.endswith(b"\r\n"):
                    raise ValueError("Invalid chunk size")
                chunk_size = int(size_str, 16)
                if chunk_size == 0:
                    # Consume trailers up to the terminating blank line.
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(65536)
                        trailer_bytes += len(trailer)
                        if not trailer or trailer_bytes > 65536:
                            raise ValueError("Invalid trailers")
                        if trailer == b"\r\n":
                            break
                    break
                total += chunk_size
                if total > self._MAX_BODY:
                    self.send_response(413)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Request body too large"}).encode())
                    return None
                chunk = self.rfile.read(chunk_size)
                if len(chunk) != chunk_size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("Truncated chunk")
                chunks.append(chunk)
        except (ValueError, OSError):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Malformed chunked body"}).encode())
            return None
        return b"".join(chunks)

    def _proxy(self):
        """Forward the request to the TTS server and relay the response."""
        # Security: block path traversal (check both raw and decoded paths)
        decoded_path = urllib.parse.unquote(self.path)
        if (".." in self.path or "\x00" in self.path
                or ".." in decoded_path or "\x00" in decoded_path):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid path"}).encode())
            return
        # Extract path without query string for prefix matching
        path_only = self.path.split("?", 1)[0]
        is_app_shutdown = self.command == "POST" and path_only == "/api/shutdown"
        if path_only != "/" and not any(path_only.startswith(p) for p in self._ALLOWED_PREFIXES):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Forbidden path"}).encode())
            return

        if path_only.startswith("/api/"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            supplied = self.headers.get("X-TTS-API-Token") or query.get("token", [""])[0]
            auth = self.headers.get("Authorization", "")
            if not supplied and auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            try:
                with open(os.path.join(RUN_DIR, "api_token"), encoding="utf-8") as token_file:
                    expected = token_file.read().strip()
            except OSError:
                expected = ""
            if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
                self.send_error(401, "Missing or invalid API token")
                self.close_connection = True
                return

        target_url = f"{TTS_URL}{self.path}"
        is_sse = path_only.endswith("/stream")

        try:
            transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
            if "chunked" in transfer_encoding:
                # http.server does not de-chunk request bodies; without this a
                # chunked upload (no Content-Length) would forward an empty body.
                body = self._read_chunked_body()
                if body is None:
                    return  # error response already sent
            else:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length < 0 or content_length > self._MAX_BODY:
                    self.send_response(413)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Request body too large"}).encode())
                    return
                body = self.rfile.read(content_length) if content_length > 0 else None
                if content_length and len(body) != content_length:
                    self.send_error(400, "Truncated request body")
                    return

            req = urllib.request.Request(target_url, data=body, method=self.command)
            req.add_header("X-TTS-Client-IP", self.client_address[0])
            for header in ["Content-Type", "Accept", "Authorization", "X-TTS-API-Token"]:
                val = self.headers.get(header)
                if val:
                    req.add_header(header, val)
            # Forward Last-Event-ID for SSE reconnection
            last_event_id = self.headers.get("Last-Event-ID")
            if last_event_id:
                req.add_header("Last-Event-ID", last_event_id)

            resp = urllib.request.urlopen(req, timeout=600)
            response_status = resp.status
            try:
                self.send_response(resp.status)
                # Filter hop-by-hop and potentially misleading headers
                _skip_headers = {
                    "transfer-encoding", "connection",
                    "access-control-allow-origin",
                    "content-encoding", "content-length",
                    "keep-alive", "proxy-authenticate",
                    "proxy-authorization", "te", "trailers", "upgrade",
                }
                for key, val in resp.getheaders():
                    if key.lower() not in _skip_headers:
                        self.send_header(key, val)
                self._send_cors_headers()
                self.end_headers()

                if is_sse:
                    try:
                        while True:
                            line = resp.readline()
                            if not line:
                                break
                            self.wfile.write(line)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                resp.close()
            if is_app_shutdown and 200 <= response_status < 300:
                _schedule_bridge_shutdown(self.server)

        except urllib.error.HTTPError as e:
            try:
                body = e.read()
                e.close()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        except Exception as e:
            log(f"Proxy error: {path_only} -> {type(e).__name__}")
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "TTS server unreachable",
                }).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        pass  # Suppress per-request logs


# ============================================================
# Diagnostics (runs in background thread)
# ============================================================
def diagnose():
    """Run diagnostics and log everything useful."""
    log("=" * 60)
    log("TTS Server Bridge â€” Diagnostics")
    log("=" * 60)
    log(f"Python: {sys.executable} ({sys.version})")
    log(f"CWD: {os.getcwd()}")
    log(f"USER: {os.environ.get('USER', 'unknown')}")

    log("")
    log("--- /opt/tts_server check ---")
    for path in [
        "/opt/tts_server", "/opt/tts_server/env.conf",
        "/opt/tts_server/venv", "/opt/tts_server/venv/bin/python3",
        "/opt/tts_server/server", "/opt/tts_server/server/tts_api_server.py",
    ]:
        exists = os.path.exists(path)
        log(f"  {'OK' if exists else 'MISSING'}: {path}")

    log("")
    log("--- GPU ---")
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        log(f"  {r.stdout.strip()}")
    except Exception as e:
        log(f"  {e}")

    log("=" * 60)


# ============================================================
# TTS server management (standalone mode only)
# ============================================================
_tts_process = None
_tts_log_fh = None
_shutdown_in_progress = False
_shutdown_lock = threading.Lock()
_tts_start_started = threading.Event()
_tts_start_done = threading.Event()
_tts_start_ok = False
_tts_manager_stop = threading.Event()
_bridge_shutdown_scheduled = threading.Event()


def _schedule_bridge_shutdown(http_server, delay: float = 3.5):
    """Stop backend supervision now, then retire the bridge after response flush.

    Without this, an intentional gateway shutdown looks like a crash to the
    supervisor and the bridge immediately starts a replacement gateway.
    """
    if _bridge_shutdown_scheduled.is_set():
        return None
    _bridge_shutdown_scheduled.set()
    _tts_manager_stop.set()

    def _finish():
        if delay > 0:
            time.sleep(delay)
        log("Intentional TTS app shutdown — stopping bridge")
        http_server.shutdown()

    thread = threading.Thread(
        target=_finish,
        name="tts-bridge-shutdown",
        daemon=True,
    )
    thread.start()
    return thread


def _setup_is_running():
    """Return True while this app's setup process owns its advisory lock."""
    lock_path = os.path.join(RUN_DIR, "tts_setup.lock")
    if not os.path.exists(lock_path):
        return False
    try:
        probe = subprocess.run(
            ["flock", "-n", lock_path, "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return probe.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        # A failed lock probe must not race a possibly active setup.
        return True


def _start_tts_server_background():
    """Start and supervise the backend without blocking the UI bridge.

    Setup and the bridge can overlap after an interrupted WSL session.  The old
    one-shot startup permanently left the UI at 502 if the venv was not ready
    on that first attempt.  Keep one lightweight supervisor instead: wait for
    this app's setup lock, start the gateway, and retry after a bounded delay if
    setup finishes later or the gateway exits.
    """
    global _tts_start_ok
    if _tts_start_started.is_set():
        return
    _tts_start_started.set()

    def _runner():
        global _tts_start_ok
        setup_wait_logged = False
        while not _tts_manager_stop.is_set():
            if os.path.exists(os.path.join(RUN_DIR, "shutdown.requested")):
                _tts_manager_stop.set()
                break
            if _check_tts_alive(timeout=0.5):
                _tts_start_ok = True
                _tts_start_done.set()
                _tts_manager_stop.wait(5)
                continue

            _tts_start_ok = False
            if _setup_is_running():
                _tts_start_done.clear()
                if not setup_wait_logged:
                    log("TTS setup is still running; backend start will retry automatically")
                    setup_wait_logged = True
                _tts_manager_stop.wait(5)
                continue

            setup_wait_logged = False
            try:
                _tts_start_ok = bool(start_tts_server())
                _tts_start_done.set()
                if not _tts_start_ok:
                    log("WARNING: TTS server failed to start; retrying in 10 seconds")
            except Exception as e:
                _tts_start_ok = False
                _tts_start_done.set()
                log(f"ERROR: TTS server background startup failed: {type(e).__name__}: {e}")
                try:
                    log(traceback.format_exc())
                except Exception:
                    pass
            if not _tts_start_ok:
                _tts_manager_stop.wait(10)

    threading.Thread(target=_runner, name="tts-backend-start", daemon=True).start()


def _send_backend_starting_page(handler):
    """Serve a small holding page while the backend is still warming up."""
    started = _tts_start_started.is_set()
    done = _tts_start_done.is_set()
    state = "starting"
    detail = "The TTS backend is warming up. This can take 15-60 seconds on a cold start."
    if not started:
        state = "queued"
        detail = "The TTS backend start has not been queued yet."
    elif done and not _tts_start_ok:
        state = "offline"
        detail = f"The TTS backend did not start. Check {LOG_FILE} and tts_server_output.log."
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="2">
  <title>TTS Server Starting</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111827; color: #e5e7eb; font-family: Segoe UI, Arial, sans-serif; }}
    main {{ max-width: 620px; padding: 28px; }}
    h1 {{ margin: 0 0 12px; font-size: 22px; }}
    p {{ color: #9ca3af; line-height: 1.5; }}
    code {{ color: #d1d5db; background: #1f2937; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <main>
    <h1>TTS Server {state}</h1>
    <p>{detail}</p>
    <p>This page refreshes automatically. API status is available at <code>/api/status</code>.</p>
  </main>
</body>
</html>"""
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler._send_cors_headers()
    handler.end_headers()
    handler.wfile.write(body)


def start_tts_server():
    """Launch the TTS server ONLY if it's not already running."""
    global _tts_process, _tts_log_fh, _tts_started_at

    # Check if already running (started by template's service manager)
    if _check_tts_alive():
        log("TTS server already running â€” skipping subprocess launch")
        return True

    # A graceful uvicorn shutdown can stop accepting requests before all
    # WebView/SSE connections have drained. Never overwrite the Popen handle
    # and strand that old child as a zombie. Give it a short chance to recover
    # or exit, then retire and reap it before launching one replacement.
    if _tts_process is not None:
        if _tts_process.poll() is None and time.monotonic() - _tts_started_at < 600:
            return False
        if _tts_process.poll() is None:
            for _ in range(3):
                time.sleep(1)
                if _check_tts_alive(timeout=0.5):
                    return True
                if _tts_process.poll() is not None:
                    break
        if _tts_process.poll() is None:
            log(f"Retiring unresponsive TTS server (PID {_tts_process.pid})")
            _tts_process.terminate()
            try:
                _tts_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _tts_process.kill()
                try:
                    _tts_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        else:
            try:
                _tts_process.wait(timeout=0)
            except (subprocess.TimeoutExpired, OSError):
                pass
        _tts_process = None
        if _tts_log_fh is not None:
            try:
                _tts_log_fh.close()
            except Exception:
                pass
            _tts_log_fh = None

    venv_py = "/opt/tts_server/venv/bin/python3"
    server_script = "/opt/tts_server/server/tts_api_server.py"

    if not os.path.exists(venv_py):
        log(f"ERROR: venv python not found at {venv_py}")
        return False
    if not os.path.exists(server_script):
        log(f"ERROR: server script not found at {server_script}")
        return False

    log(f"Starting TTS server: {venv_py} {server_script} --host 0.0.0.0 --port {TTS_PORT}")

    try:
        # Append (not truncate) so a prior crash's output survives a restart
        # and is still available for diagnosis on the next launch attempt.
        tts_log_path = os.path.join(RUN_DIR, "tts_server_output.log")
        _rotate_log_file(tts_log_path)
        _tts_log_fh = open(tts_log_path, "a")
    except Exception as e:
        log(f"ERROR: Failed to open TTS log file: {e}")
        return False
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.update({
        "HOME": os.path.join(CACHE_DIR, "home"),
        "XDG_CACHE_HOME": os.path.join(CACHE_DIR, "xdg-cache"),
        "XDG_DATA_HOME": os.path.join(CACHE_DIR, "xdg-data"),
        "XDG_CONFIG_HOME": os.path.join(CACHE_DIR, "xdg-config"),
        "XDG_STATE_HOME": os.path.join(CACHE_DIR, "xdg-state"),
        "PATH": PORTABLE_EXEC_PATH,
        "PYTHONNOUSERSITE": "1",
        "PIP_CACHE_DIR": os.path.join(CACHE_DIR, "pip"),
        "NUMBA_CACHE_DIR": os.path.join(CACHE_DIR, "numba"),
        "TRITON_CACHE_DIR": os.path.join(CACHE_DIR, "triton"),
        "CUDA_CACHE_PATH": os.path.join(CACHE_DIR, "cuda"),
        "TORCHINDUCTOR_CACHE_DIR": os.path.join(CACHE_DIR, "torchinductor"),
        "MPLCONFIGDIR": os.path.join(CACHE_DIR, "matplotlib"),
        "TTS_SERVER_APP_DIR": APP_DIR,
        "TTS_SERVER_RUN_DIR": RUN_DIR,
    })

    try:
        _tts_started_at = time.monotonic()
        _tts_process = subprocess.Popen(
            [venv_py, server_script, "--host", "0.0.0.0", "--port", str(TTS_PORT)],
            stdout=_tts_log_fh, stderr=subprocess.STDOUT,
            env=env, cwd="/opt/tts_server/server",
        )
    except Exception as e:
        log(f"ERROR: Failed to start TTS server: {e}")
        _tts_log_fh.close()
        _tts_log_fh = None
        return False
    log(f"TTS server launched (PID {_tts_process.pid})")

    for i in range(60):
        time.sleep(1)
        if _tts_process.poll() is not None:
            log(f"TTS server exited with code {_tts_process.returncode}!")
            try:
                with open(os.path.join(RUN_DIR, "tts_server_output.log")) as f:
                    log(f"Server output:\n{f.read()[-2000:]}")
            except Exception:
                pass
            return False
        if _check_tts_alive():
            log(f"TTS server ready after {i+1}s")
            return True
        if i % 10 == 9:
            log(f"Waiting for TTS server... ({i+1}s)")

    log("TTS server failed to start within 60s")
    return False


def stop_tts_server():
    """Stop the TTS server subprocess (with reentrancy guard)."""
    global _tts_process, _tts_log_fh, _shutdown_in_progress
    _tts_manager_stop.set()
    with _shutdown_lock:
        if _shutdown_in_progress:
            return
        _shutdown_in_progress = True

    if _tts_log_fh:
        try:
            _tts_log_fh.close()
        except Exception:
            pass
        _tts_log_fh = None
    if not _tts_process:
        _shutdown_in_progress = False
        return
    if _tts_process.poll() is not None:
        log(f"TTS server already exited (code {_tts_process.returncode})")
        _tts_process = None
        _shutdown_in_progress = False
        return
    log(f"Stopping TTS server (PID {_tts_process.pid})...")
    _tts_process.terminate()
    try:
        _tts_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _tts_process.kill()
        try:
            _tts_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    log("TTS server stopped")
    _tts_process = None
    with _shutdown_lock:
        _shutdown_in_progress = False


# ============================================================
# Main
# ============================================================
def main():
    # Custom ports permit independent copies, but one folder must own only one
    # bridge/gateway session. Keep this descriptor alive for the whole process.
    instance_lock = None
    if IN_WSL:
        import fcntl
        instance_lock = open(os.path.join(RUN_DIR, "tts_bridge.lock"), "a")
        try:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            instance_lock.close()
            log("This portable folder already has a running TTS session.")
            _refuse_unsafe_launch("This portable folder already has a running TTS session.")
    log(f"Bridge starting: port {PORT} -> proxy to localhost:{TTS_PORT}")

    # Run diagnostics in background (don't block startup)
    threading.Thread(target=diagnose, daemon=True).start()

    # Cleanup on exit
    atexit.register(_close_log)
    atexit.register(stop_tts_server)

    def _sig_handler(signum, frame):
        log(f"Bridge received signal {signum} â€” cleaning up")
        stop_tts_server()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    # Check bridge-port availability before starting the gateway. A duplicate
    # bridge start must fail without launching and then killing a fresh TTS
    # server during a later bind failure.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _bind_probe:
        _bind_probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _bind_probe.bind((BIND_ADDR, PORT))

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        _slots = threading.BoundedSemaphore(16)

        def process_request(self, request, client_address):
            if not self._slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

    server = ThreadedHTTPServer((BIND_ADDR, PORT), ProxyHandler)
    try:
        os.unlink(os.path.join(RUN_DIR, "shutdown.requested"))
    except FileNotFoundError:
        pass
    log(f"Bridge proxy listening on {BIND_ADDR}:{PORT}")
    _start_tts_server_background()
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_tts_server()
        server.server_close()


if __name__ == "__main__":
    main()
