#!/usr/bin/env python3
"""TTS Server CLI - full control surface over the local TTS server API.

Discovery order for the server URL and API token:
  1. CLI flags (--url, --token)
  2. Env vars (TTS_API_URL, TTS_API_TOKEN)
  3. Portable registry: <app_dir>/output/run/registry/tts_server.json
  4. Local token file: <app_dir>/output/run/api_token (URL defaults to bridge)
  5. Defaults: http://127.0.0.1:9300 (bridge) / http://127.0.0.1:8300 (gateway)

Run `tts <verb> --help` for verb-specific options.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# Force UTF-8 on Windows console so we can print non-ASCII text in JSON responses
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ---------------------------------------------------------------------------
# Discovery / configuration
# ---------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_BRIDGE_URL = "http://127.0.0.1:9300"
TOKEN_HEADER = "X-TTS-API-Token"


def _registry_path() -> Path | None:
    """Return this app's E:-local discovery file.

    Resolving relative to tts.py keeps the CLI paired with the server copy it
    ships beside and avoids reading or writing Windows LOCALAPPDATA. An explicit
    path remains available for controlled integration tests.
    """
    configured = os.environ.get("TTS_REGISTRY_PATH", "").strip()
    if configured:
        return Path(configured)
    return APP_ROOT / "output" / "run" / "registry" / "tts_server.json"


def _pid_alive(pid: Any) -> bool:
    """Best-effort liveness check for a registry-recorded pid (stdlib only).

    Returns True if the pid looks alive (or if we cannot tell), False only when
    we positively determine the process is gone. Unknown/missing pids return
    True so we never discard a registry entry we cannot verify.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (OSError, AttributeError):
            return True
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER (87) => no such process. Any other error
            # (e.g. access denied) means we can't tell, so assume alive.
            return ctypes.get_last_error() != 87
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_registry() -> dict | None:
    p = _registry_path()
    if p is None or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # A PID published by WSL belongs to its Linux PID namespace and
            # must not be tested with Windows OpenProcess (an unrelated or
            # absent Windows PID would produce a false stale result).
            extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
            pid_is_cross_namespace = sys.platform == "win32" and bool(extra.get("wsl_distro"))
            if "pid" in data and not pid_is_cross_namespace and not _pid_alive(data.get("pid")):
                return None
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _read_local_token() -> str | None:
    """Read the gateway's per-launch token file from the portable app tree."""
    token_file = APP_ROOT / "output" / "run" / "api_token"
    try:
        return token_file.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None


class Config:
    """Resolved server URL + token, plus the registry entry if available."""

    def __init__(self, url: str | None = None, token: str | None = None):
        registry = _read_registry()
        # URL discovery
        env_url = os.environ.get("TTS_API_URL")
        if url:
            self.url = url.rstrip("/")
            self.url_source = "flag"
        elif env_url:
            self.url = env_url.rstrip("/")
            self.url_source = "env"
        elif registry and registry.get("endpoints", {}).get("api"):
            self.url = registry["endpoints"]["api"].rstrip("/")
            self.url_source = "registry"
        else:
            self.url = DEFAULT_BRIDGE_URL
            self.url_source = "default"

        # Token discovery
        env_token = os.environ.get("TTS_API_TOKEN")
        if token is not None:
            self.token = token
            self.token_source = "flag"
        elif env_token:
            self.token = env_token
            self.token_source = "env"
        elif self.url_source in ("flag", "env") and self.url.rstrip("/") not in {
            DEFAULT_BRIDGE_URL, "http://localhost:9300",
            "http://127.0.0.1:8300", "http://localhost:8300",
            *((registry or {}).get("endpoints", {}).values()),
        }:
            self.token = ""
            self.token_source = "none"
        elif registry and registry.get("auth", {}).get("token"):
            self.token = registry["auth"]["token"]
            self.token_source = "registry"
        else:
            local_token = _read_local_token()
            if local_token:
                self.token = local_token
                self.token_source = "token_file"
            else:
                self.token = ""
                self.token_source = "none"

        self.registry = registry


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """HTTP-level API error, carries status + body."""

    def __init__(self, status: int, body: Any, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        if isinstance(body, dict):
            msg = body.get("detail") or body.get("error") or json.dumps(body)
        else:
            msg = str(body)
        super().__init__(f"HTTP {status} from {url}: {msg}")


def _build_request(cfg: Config, method: str, path: str, *,
                   query: dict | None = None,
                   json_body: Any = None,
                   raw_body: bytes | None = None,
                   content_type: str | None = None,
                   extra_headers: dict | None = None) -> urlrequest.Request:
    url = cfg.url + path
    if query:
        url += ("&" if "?" in url else "?") + urlparse.urlencode(
            {k: v for k, v in query.items() if v is not None}
        )
    headers: dict[str, str] = {}
    if cfg.token:
        headers[TOKEN_HEADER] = cfg.token
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        body = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    else:
        body = None
    if extra_headers:
        headers.update(extra_headers)
    req = urlrequest.Request(url, data=body, method=method, headers=headers)
    return req


class SameOriginRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlparse.urlsplit(req.full_url), urlparse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise urlerror.HTTPError(req.full_url, code, "Refusing cross-origin authenticated redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(cfg: Config, method: str, path: str, *,
            query: dict | None = None,
            json_body: Any = None,
            raw_body: bytes | None = None,
            content_type: str | None = None,
            extra_headers: dict | None = None,
            timeout: float = 60.0,
            stream: bool = False) -> Any:
    req = _build_request(cfg, method, path,
                         query=query, json_body=json_body,
                         raw_body=raw_body, content_type=content_type,
                         extra_headers=extra_headers)
    try:
        resp = urlrequest.build_opener(SameOriginRedirect()).open(req, timeout=timeout)
    except urlerror.HTTPError as e:
        body_bytes = e.read() if e.fp else b""
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = body_bytes.decode("utf-8", errors="replace")
        raise ApiError(e.code, body, url=req.full_url) from e
    except urlerror.URLError as e:
        raise ApiError(0, f"Cannot reach server: {e.reason}", url=req.full_url) from e

    if stream:
        return resp
    try:
        body_bytes = resp.read()
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            return json.loads(body_bytes.decode("utf-8")) if body_bytes else None
        if ctype.startswith("text/"):
            return body_bytes.decode("utf-8", errors="replace")
        return body_bytes
    finally:
        resp.close()


def _multipart_body(fields: dict[str, Any], files: dict[str, Path]) -> tuple[bytes, str]:
    """Encode a multipart/form-data body. files maps field name -> file path."""
    boundary = "----TTSBOUNDARY" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for name, path in files.items():
        if path is None:
            continue
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        filename = path.name
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _json_out(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _print_table(rows: list[list[str]], headers: list[str] | None = None) -> None:
    if not rows and not headers:
        return
    grid = ([headers] if headers else []) + [[str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in grid) for i in range(len(grid[0]))]
    for i, row in enumerate(grid):
        line = "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        print(line)
        if headers and i == 0:
            print("  ".join("-" * w for w in widths))


def _print_kv(d: dict, *, indent: int = 0) -> None:
    pad = "  " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            print(f"{pad}{k}:")
            _print_kv(v, indent=indent + 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"{pad}{k}:")
            for item in v:
                print(f"{pad}  -")
                _print_kv(item, indent=indent + 2)
        else:
            print(f"{pad}{k}: {v}")


def _abort(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _field(data: Any, key: str) -> Any:
    """Index a top-level field of an API response, with a clean error.

    Raises ApiError(0, ...) — caught by main() — instead of a raw KeyError/
    TypeError when the server returns an empty body or an unexpected shape.
    """
    if not isinstance(data, dict) or key not in data:
        raise ApiError(0, f"unexpected response shape: missing '{key}'")
    return data[key]


# ---------------------------------------------------------------------------
# Verb implementations
# ---------------------------------------------------------------------------

def cmd_health(cfg: Config, args) -> None:
    try:
        data = request(cfg, "GET", "/health", timeout=5)
    except ApiError as e:
        if args.json:
            _json_out({"status": "down", "error": str(e), "url": cfg.url})
            sys.exit(2)
        _abort(f"server unreachable at {cfg.url}: {e}", code=2)
    if args.json:
        _json_out(data)
        return
    print(f"status:  {data.get('status')}")
    print(f"url:     {cfg.url} (from {cfg.url_source})")
    print(f"workers: {data.get('worker_count')}")
    print(f"loaded:  {', '.join(data.get('loaded_models', [])) or '(none)'}")


def cmd_wait_ready(cfg: Config, args) -> None:
    """Block until server up (and model loaded if --model given)."""
    deadline = time.monotonic() + args.timeout
    server_up = False
    last_err: str | None = None
    while not server_up:
        try:
            request(cfg, "GET", "/health", timeout=2)
            server_up = True
        except ApiError as e:
            last_err = str(e)
            if time.monotonic() >= deadline:
                if args.json:
                    _json_out({"ready": False, "reason": "server_unreachable", "error": last_err})
                    sys.exit(3)
                _abort(f"server did not come up within {args.timeout}s: {last_err}", code=3)
            time.sleep(min(1.0, max(0.1, args.interval)))

    if not args.model:
        if args.json:
            _json_out({"ready": True, "url": cfg.url})
        else:
            print(f"ready: {cfg.url}")
        return

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            data = request(cfg, "GET", "/api/ready",
                           query={"model": args.model, "timeout": str(min(remaining, 600))},
                           timeout=min(remaining, 600) + 5)
            break
        except ApiError as e:
            if e.status == 503 and time.monotonic() < deadline:
                continue
            if args.json:
                _json_out({"ready": False, "model": args.model, "error": str(e)})
                sys.exit(3)
            _abort(str(e), code=3)
    if args.json:
        _json_out(data)
    else:
        print(f"ready: {args.model} ({len(data.get('workers', []))} worker(s))")


def cmd_config(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/config")
    if args.key:
        # support dotted lookup: a.b.c
        cur: Any = data
        for part in args.key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                _abort(f"key not found: {args.key}")
        if args.json or isinstance(cur, (dict, list)):
            _json_out(cur)
        else:
            print(cur)
        return
    if args.json:
        _json_out(data)
    else:
        print("Config keys:")
        for k in sorted(data):
            v = data[k]
            preview = "(dict)" if isinstance(v, dict) else "(list)" if isinstance(v, list) else repr(v)[:60]
            print(f"  {k:18s} {preview}")


def cmd_models(cfg: Config, args) -> None:
    listing = _field(request(cfg, "GET", "/api/models"), "models")
    setup = _field(request(cfg, "GET", "/api/setup/status"), "models")
    workers = _field(request(cfg, "GET", "/api/workers"), "workers")
    loaded_set = {w["model"] for w in workers if w["status"] in ("ready", "busy")}
    items = []
    for m in listing:
        mid = m["id"]
        st = setup.get(mid, {}).get("status", "?")
        loaded = mid in loaded_set
        if args.installed and st != "ready":
            continue
        if args.loaded and not loaded:
            continue
        items.append({
            "id": mid,
            "name": m["name"],
            "install_status": st,
            "loaded": loaded,
            "weights_size": m.get("weights_size"),
            "desc": m.get("desc"),
        })
    if args.json:
        _json_out({"models": items})
        return
    rows = [[it["id"], it["name"], it["install_status"],
             "yes" if it["loaded"] else "no", it["weights_size"] or "-"]
            for it in items]
    _print_table(rows, headers=["id", "name", "install_status", "loaded", "size"])


def cmd_devices(cfg: Config, args) -> None:
    data = _field(request(cfg, "GET", "/api/devices"), "devices")
    if args.json:
        _json_out(data)
        return
    rows = [[d["id"], d["name"],
             f"{d.get('vram_total_mb', 0)}MB",
             f"{d.get('vram_free_mb', 0)}MB",
             ",".join(d.get("workers", [])) or "-"]
            for d in data]
    _print_table(rows, headers=["device", "name", "total", "free", "workers"])


def cmd_status(cfg: Config, args) -> None:
    while True:
        try:
            health = request(cfg, "GET", "/health", timeout=3)
        except ApiError as e:
            print(f"server unreachable: {e}", file=sys.stderr)
            if not args.watch:
                sys.exit(2)
            time.sleep(args.interval)
            continue
        setup = _field(request(cfg, "GET", "/api/setup/status"), "models")
        workers = _field(request(cfg, "GET", "/api/workers"), "workers")
        by_model: dict[str, list] = {}
        for w in workers:
            by_model.setdefault(w["model"], []).append(w)

        if args.watch:
            os.system("cls" if os.name == "nt" else "clear")
        print(f"=== TTS Server @ {cfg.url} ===")
        print(f"workers: {len(workers)}    loaded: {', '.join(health.get('loaded_models', [])) or '(none)'}")
        print()
        rows = []
        for mid, info in sorted(setup.items()):
            ws = by_model.get(mid, [])
            ws_summary = ",".join(f"{w['worker_id']}({w['status']})" for w in ws) or "-"
            rows.append([
                mid,
                info.get("status", "?"),
                str(len(ws)),
                ws_summary,
            ])
        _print_table(rows, headers=["model", "install", "workers", "ids"])

        if not args.watch:
            return
        time.sleep(args.interval)


def cmd_install(cfg: Config, args) -> None:
    data = request(cfg, "POST", f"/api/setup/install/{args.model}")
    if data.get("status") == "busy":
        _abort(data.get("message", "Another installation is active"), code=4)
    if args.wait:
        deadline = time.monotonic() + args.timeout
        while True:
            snapshot = request(cfg, "GET", "/api/setup/status")
            active = snapshot.get("active_installs", {})
            running = active.get("all") if args.model == "all" else args.model in active.get("models", [])
            if not running:
                statuses = snapshot.get("models", {})
                requested = list(statuses) if args.model == "all" else [args.model]
                failures = {}
                for model in requested:
                    info = statuses.get(model, {})
                    if not (info.get("status") == "ready" or
                            (info.get("status") == "packages_only" and info.get("weights_on_demand"))):
                        failures[model] = info.get("status", "missing")
                outcome = active.get("recent", {}).get(args.model, {})
                for model in outcome.get("failed_models", []):
                    failures.setdefault(model, "installer_failed")
                status = outcome.get("status")
                data = {"status": status if status in ("failed", "cancelled") else
                        ("failed" if failures else "completed"), "failures": failures}
                break
            if time.monotonic() >= deadline:
                _abort("Timed out waiting for installation", code=4)
            time.sleep(5)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)
    if data.get("status") in ("failed", "cancelled"):
        sys.exit(4)


def cmd_remove(cfg: Config, args) -> None:
    data = request(cfg, "DELETE", f"/api/setup/{args.model}")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_token(cfg: Config, args) -> None:
    if not cfg.token:
        _abort("no token discovered (server may be down or registry missing)", code=2)
    if args.json:
        _json_out({
            "token": cfg.token,
            "header": TOKEN_HEADER,
            "url": cfg.url,
            "token_source": cfg.token_source,
            "url_source": cfg.url_source,
        })
    else:
        print(cfg.token)


def cmd_peers(cfg: Config, args) -> None:
    if args.local:
        # Read from filesystem directly (no need for a running server)
        from glob import glob
        reg = _registry_path()
        if reg is None:
            _abort("no registry path available on this OS", code=2)
        files = sorted(glob(str(reg.parent / "*.json")))
        peers = []
        for f in files:
            try:
                entry = json.loads(Path(f).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(entry, dict):
                continue
            cross_namespace = sys.platform == "win32" and bool((entry.get("extra") or {}).get("wsl_distro"))
            if "pid" in entry and not cross_namespace and not _pid_alive(entry.get("pid")):
                continue
            entry.pop("auth", None)
            peers.append(entry)
        if args.json:
            _json_out({"peers": peers, "registry_dir": str(reg.parent)})
            return
        _print_table(
            [[p.get("name", "?"), p.get("version", "?"),
              p.get("endpoints", {}).get("api", "?"),
              str(p.get("pid", "?"))]
             for p in peers],
            headers=["name", "version", "api", "pid"],
        )
        return

    data = request(cfg, "GET", "/api/peers")
    if args.json:
        _json_out(data)
        return
    _print_table(
        [[p.get("name", "?"), p.get("version", "?"),
          p.get("endpoints", {}).get("api", "?"),
          str(p.get("pid", "?"))]
         for p in _field(data, "peers")],
        headers=["name", "version", "api", "pid"],
    )


# --- Workers ---

def cmd_workers_list(cfg: Config, args) -> None:
    data = _field(request(cfg, "GET", "/api/workers"), "workers")
    if args.json:
        _json_out(data)
        return
    rows = [[w["worker_id"], w["model"], w["device"], w["status"],
             str(w.get("vram_used_mb", 0)) + "MB", str(w.get("port", "-"))]
            for w in data]
    _print_table(rows, headers=["id", "model", "device", "status", "vram", "port"])


def cmd_workers_spawn(cfg: Config, args) -> None:
    body: dict[str, Any] = {"model": args.model}
    if args.device:
        body["device"] = args.device
    if args.precision:
        body["precision"] = args.precision
    data = request(cfg, "POST", "/api/workers/spawn", json_body=body, timeout=300)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_workers_kill(cfg: Config, args) -> None:
    data = request(cfg, "DELETE", f"/api/workers/{args.worker_id}")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_workers_scale(cfg: Config, args) -> None:
    body: dict[str, Any] = {"count": args.count}
    if args.device:
        body["device"] = args.device
    data = request(cfg, "POST", f"/api/models/{args.model}/scale",
                   json_body=body, timeout=600)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_model_load(cfg: Config, args) -> None:
    q = {"device": args.device} if args.device else None
    data = request(cfg, "POST", f"/api/models/{args.model}/load",
                   query=q, timeout=600)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_model_unload(cfg: Config, args) -> None:
    data = request(cfg, "POST", f"/api/models/{args.model}/unload", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- Voices ---

def cmd_voices_list(cfg: Config, args) -> None:
    data = _field(request(cfg, "GET", "/api/voices"), "voices")
    if args.json:
        _json_out(data)
        return
    rows = [[v["name"], v["filename"], f"{v['size'] // 1024}KB",
             "yes" if v.get("transcription") else "no"]
            for v in data]
    _print_table(rows, headers=["name", "filename", "size", "transcribed"])


def cmd_voices_upload(cfg: Config, args) -> None:
    p = Path(args.file)
    if not p.exists():
        _abort(f"file not found: {p}")
    body, ctype = _multipart_body({}, {"file": p})
    data = request(cfg, "POST", "/api/voices/upload",
                   raw_body=body, content_type=ctype, timeout=120)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_voices_delete(cfg: Config, args) -> None:
    data = request(cfg, "DELETE", f"/api/voices/{urlparse.quote(args.name)}")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_voices_transcribe(cfg: Config, args) -> None:
    data = request(cfg, "POST",
                   f"/api/voices/{urlparse.quote(args.name)}/transcribe",
                   query={"size": args.size}, timeout=300)
    if args.json:
        _json_out(data)
    else:
        print(data.get("text", ""))


def cmd_voices_play(cfg: Config, args) -> None:
    """Download a voice file to local path."""
    raw = request(cfg, "GET", f"/api/voices/{urlparse.quote(args.name)}/audio",
                  timeout=60)
    if isinstance(raw, str):
        raw = raw.encode()
    out = Path(args.out) if args.out else Path(args.name)
    out.write_bytes(raw)
    print(str(out))


# --- Whisper ---

def cmd_whisper_info(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/whisper")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_whisper_load(cfg: Config, args) -> None:
    data = request(cfg, "POST", f"/api/whisper/{args.size}/load", timeout=600)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_whisper_unload(cfg: Config, args) -> None:
    data = request(cfg, "POST", f"/api/whisper/{args.size}/unload", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- TTS ---

_TTS_PARAM_FIELDS = (
    "voice", "reference_text", "language", "save_path", "device",
    "speed", "temperature", "repetition_penalty",
    "top_p", "top_k", "cfg_scale", "cfg_alpha", "exaggeration", "cfg_weight",
    "waveform_temperature", "seed", "nfe_step", "pitch", "volume",
    "speaker_idx", "voice_description",
    "de_reverb", "de_ess", "tolerance", "whisper_model",
    "verify_whisper", "skip_post_process", "auto_retry",
    "inter_pause_sec", "front_pad_sec", "padding_sec",
    "trim_db", "min_silence_ms", "front_protect_ms", "end_protect_ms",
    "clipping", "lufs",
)

# Numeric TTS params and their argparse type=, shared by the `tts` and `dryrun`
# verbs so both coerce e.g. --speed/--top-k to the same JSON number type.
_TTS_NUMERIC_TYPES: dict[str, type] = {
    "speed": float, "temperature": float, "repetition_penalty": float,
    "top_p": float, "top_k": int, "cfg_scale": float, "cfg_alpha": float,
    "exaggeration": float, "cfg_weight": float,
    "waveform_temperature": float, "seed": int, "nfe_step": int,
    "pitch": int, "volume": int, "speaker_idx": int,
    "de_reverb": float, "de_ess": float, "tolerance": float,
    "auto_retry": int,
    "inter_pause_sec": float, "front_pad_sec": float,
    "padding_sec": float, "trim_db": float,
    "min_silence_ms": int, "front_protect_ms": int,
    "end_protect_ms": int, "clipping": float, "lufs": float,
}


def _tts_build_body(args) -> dict[str, Any]:
    if args.text and args.text_file:
        _abort("specify --text OR --text-file, not both")
    if args.text:
        text = args.text
    elif args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        _abort("--text or --text-file required (or pipe text on stdin)")
    if not text.strip():
        _abort("--text or --text-file required (or pipe text on stdin)")
    body: dict[str, Any] = {"text": text}
    suffix = Path(getattr(args, "out", None) or "").suffix.lower().lstrip(".")
    if suffix and suffix not in {"wav", "mp3", "flac", "ogg", "m4a"}:
        _abort(f"Unsupported --out extension: {suffix}")
    if suffix and args.format and suffix != args.format:
        _abort("--out extension must match --format")
    if suffix or args.format:
        body["output_format"] = args.format or suffix
    for field in _TTS_PARAM_FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            body[field] = val
    if args.ref:
        ref_path = Path(args.ref)
        if not ref_path.exists():
            _abort(f"reference audio not found: {ref_path}")
        body["reference_audio"] = base64.b64encode(ref_path.read_bytes()).decode()
        body["reference_audio_name"] = ref_path.name
    return body


def cmd_tts(cfg: Config, args) -> None:
    body = _tts_build_body(args)

    # Pick infer timeout: long jobs need it
    timeout = args.request_timeout

    if args.async_submit:
        # The submit endpoint blocks only until the job is created (worker
        # spawn + chunking). Auto-install can take minutes if the model
        # isn't installed yet, so use a generous prep timeout.
        data = request(cfg, "POST", f"/api/tts/{args.model}/submit",
                       json_body=body, timeout=1800)
        if args.json:
            _json_out(data)
        else:
            print(_field(data, "job_id"))
        return

    data = request(cfg, "POST", f"/api/tts/{args.model}",
                   json_body=body, timeout=timeout)

    if data.get("status") != "completed":
        _abort(f"Synthesis ended with status: {data.get('status', 'unknown')}", code=5)
    # Resolve where to write. Priority:
    #   --out FILE        : caller-specified local path
    #   --save-path X     : server saved it; nothing to write locally
    #   else              : write to ./<filename> the server picked
    if args.out:
        out_path = Path(args.out)
    elif body.get("save_path"):
        out_path = None
    else:
        # No explicit destination — fall back to the server's filename in CWD
        # so a one-shot `tts kokoro --text ...` always produces a file.
        out_path = Path(data.get("filename") or f"{args.model}_output.wav")

    audio_b64 = data.get("audio_base64")
    if audio_b64 and out_path:
        out_path.write_bytes(base64.b64decode(audio_b64))
    elif out_path and data.get("output_url"):
        download(cfg, data["output_url"], out_path)
    elif out_path and not audio_b64:
        if data.get("job_id"):
            download(cfg, f"/api/jobs/{data['job_id']}/output", out_path)
        else:
            _abort("Server returned no downloadable audio", code=5)

    # Strip the bulky base64 from JSON output
    summary = {k: v for k, v in data.items() if k != "audio_base64"}
    if out_path:
        summary["written_to"] = str(out_path)
    if args.json:
        _json_out(summary)
    else:
        _print_kv(summary)


# --- Jobs ---

def cmd_jobs_list(cfg: Config, args) -> None:
    data = []
    wanted = args.limit or 0
    while True:
        page = request(cfg, "GET", "/api/jobs", query={
            "status": args.status, "limit": min(500, wanted - len(data)) if wanted else 500,
            "offset": len(data)})
        rows = page.get("jobs", [])
        data.extend(rows)
        if not rows or len(data) >= page.get("total", len(data)) or (wanted and len(data) >= wanted):
            break
    if args.json:
        _json_out(data)
        return
    rows = [[j.get("job_id", "?")[:8], j.get("model"), j.get("status"),
             f"{j.get('chunks_completed', 0)}/{j.get('total_chunks', 0)}",
             f"{j.get('duration_sec') if j.get('duration_sec') is not None else '-'}",
             (j.get("text_preview") or "")[:60]]
            for j in data]
    _print_table(rows, headers=["id", "model", "status", "chunks", "dur", "preview"])


def cmd_jobs_get(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/jobs/{args.id}")
    if args.json:
        _json_out(data)
    else:
        _print_kv({k: v for k, v in data.items() if k != "chunks"})
        chunks = data.get("chunks", [])
        if chunks:
            print()
            print(f"chunks: {len(chunks)}")
            rows = [[str(c["index"]),
                     "ok" if c.get("duration_sec") is not None else
                     ("err" if c.get("processing_error") else "pending"),
                     str(c.get("duration_sec") if c.get("duration_sec") is not None else "-"),
                     (c.get("text") or "")[:80]]
                    for c in chunks]
            _print_table(rows, headers=["#", "status", "dur", "text"])


def cmd_jobs_output(cfg: Config, args) -> None:
    # Fetch metadata only when we need a default filename (no --out given,
    # not piping to stdout). Cheap 404 fail-fast when the job doesn't exist.
    job: dict | None = None
    if not args.out:
        try:
            job = request(cfg, "GET", f"/api/jobs/{args.id}")
        except ApiError:
            job = None
    raw = request(cfg, "GET", f"/api/jobs/{args.id}/output", timeout=300)
    if isinstance(raw, str):
        raw = raw.encode()
    if args.out == "-":
        sys.stdout.buffer.write(raw)
        return
    final = (job or {}).get("final_file") or f"job_{args.id[:8]}.wav"
    out = Path(args.out) if args.out else Path(final)
    out.write_bytes(raw)
    print(str(out))


def cmd_jobs_cancel(cfg: Config, args) -> None:
    job = request(cfg, "GET", f"/api/jobs/{args.id}")
    model = job.get("model")
    data = request(cfg, "POST", f"/api/tts/{model}/cancel",
                   json_body={"job_id": args.id})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_jobs_recover(cfg: Config, args) -> None:
    data = request(cfg, "POST", f"/api/jobs/{args.id}/recover", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_jobs_delete(cfg: Config, args) -> None:
    data = request(cfg, "POST", "/api/jobs/delete",
                   json_body={"job_ids": args.ids})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_jobs_edit_chunk(cfg: Config, args) -> None:
    data = request(cfg, "PUT",
                   f"/api/jobs/{args.id}/chunks/{args.chunk_idx}",
                   json_body={"text": args.text})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_jobs_chunk_audio(cfg: Config, args) -> None:
    raw = request(cfg, "GET",
                  f"/api/jobs/{args.id}/chunks/{args.chunk_idx}/audio",
                  timeout=60)
    if isinstance(raw, str):
        raw = raw.encode()
    out = Path(args.out) if args.out else Path(f"chunk_{args.chunk_idx:03d}.wav")
    out.write_bytes(raw)
    print(str(out))


def cmd_jobs_wait(cfg: Config, args) -> None:
    """Poll a job until terminal state."""
    deadline = time.monotonic() + args.timeout
    last_status = None
    last_completed = -1
    while True:
        try:
            job = request(cfg, "GET", f"/api/jobs/{args.id}", timeout=10)
        except ApiError as e:
            _abort(str(e), code=2)
        status = job.get("status")
        completed = job.get("chunks_completed", 0)
        total = job.get("total_chunks", 0)
        if (status != last_status or completed != last_completed) and not args.json:
            print(f"  [{status}] chunks={completed}/{total}", file=sys.stderr)
            last_status = status
            last_completed = completed
        if status in ("completed", "failed", "cancelled", "incomplete"):
            if args.json:
                _json_out(job)
            else:
                _print_kv({k: v for k, v in job.items()
                           if k not in ("chunks", "input_text", "expected_files", "missing_files", "parameters")})
            sys.exit(0 if status == "completed" else 5)
        if time.monotonic() >= deadline:
            _abort(f"timeout after {args.timeout}s; last status={status}", code=4)
        time.sleep(args.interval)


def cmd_jobs_chunks(cfg: Config, args) -> None:
    """List chunks of a job."""
    job = request(cfg, "GET", f"/api/jobs/{args.id}")
    chunks = job.get("chunks", [])
    if args.json:
        _json_out(chunks)
        return
    rows = [[str(c["index"]),
             "ok" if c.get("duration_sec") is not None else ("err" if c.get("processing_error") else "pending"),
             str(c.get("duration_sec") if c.get("duration_sec") is not None else "-"),
             (c.get("text") or "")[:80]]
            for c in chunks]
    _print_table(rows, headers=["#", "status", "dur", "text"])


# --- Projects ---

def cmd_projects_list(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/projects")
    if args.json:
        _json_out(data)
        return
    print(f"root: {_field(data, 'root')}")
    rows = [[p["name"], str(p["job_count"]), p["modified"]]
            for p in _field(data, "projects")]
    _print_table(rows, headers=["name", "jobs", "modified"])


# --- Logs ---

def cmd_logs_follow(cfg: Config, args) -> None:
    pattern = re.compile(args.filter) if args.filter else None
    while True:
        resp = None
        try:
            try:
                resp = request(cfg, "GET", "/api/logs/stream",
                               timeout=600, stream=True)
            except ApiError as e:
                if not args.reconnect:
                    _abort(str(e), code=2)
                print(f"(reconnect) stream open failed: {e}", file=sys.stderr)
                time.sleep(2)
                continue
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                try:
                    entry = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message", "")
                if pattern and not pattern.search(msg):
                    continue
                ts = entry.get("timestamp", "")
                lvl = entry.get("level", "info").upper()[:4]
                print(f"{ts} {lvl} {msg}", flush=True)
            # Server closed the stream cleanly; reconnect or exit
            if not args.reconnect:
                return
            time.sleep(1)
        except KeyboardInterrupt:
            return
        except Exception as e:
            if not args.reconnect:
                _abort(f"stream error: {e}", code=2)
            print(f"(reconnect) {e}", file=sys.stderr)
            time.sleep(2)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass


# --- HF Token ---

def cmd_hf_token_get(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/setup/hf-token")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_hf_token_set(cfg: Config, args) -> None:
    if args.token == "-":
        token = sys.stdin.read().strip()
    elif args.token is None:
        token = ""  # delete
    else:
        token = args.token
    data = request(cfg, "POST", "/api/setup/hf-token", json_body={"token": token})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- Batch ---

def cmd_batch(cfg: Config, args) -> None:
    """Submit many TTS jobs from a JSON file.

    File format:
        {
          "model": "kokoro",                       # default if not per-job
          "wait": false,                           # block on each? default false
          "jobs": [
            {"text": "...", "voice": "...", "save_path": "..."},
            {"model": "xtts", "text": "..."}
          ]
        }
    """
    spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
    default_model = spec.get("model")
    wait = bool(spec.get("wait", False))
    results = []
    for j in spec.get("jobs", []):
        model = j.pop("model", None) or default_model
        if not model:
            _abort("each job needs a model (top-level or per-job)")
        endpoint = f"/api/tts/{model}" if wait else f"/api/tts/{model}/submit"
        try:
            data = request(cfg, "POST", endpoint, json_body=j,
                           timeout=args.request_timeout if wait else 1800)
            data = {k: v for k, v in data.items() if k not in ("audio_base64", "audio")}
            results.append({"model": model, "ok": data.get("status") in ("completed", "submitted"), "result": data})
        except ApiError as e:
            results.append({"model": model, "ok": False, "error": str(e)})
    if args.json:
        _json_out(results)
        if any(not r["ok"] for r in results):
            sys.exit(5)
        return
    for i, r in enumerate(results):
        ok = r["ok"]
        if ok:
            jr = r["result"]
            print(f"[{i}] {r['model']}: {jr.get('status')} job_id={jr.get('job_id')}")
        else:
            print(f"[{i}] {r['model']}: ERROR {r.get('error', 'Synthesis did not complete')}")


    if any(not r["ok"] for r in results):
        sys.exit(5)


# --- Version / about ---

def cmd_version(cfg: Config, args) -> None:
    """Print CLI + server version. CLI version is the script's __doc__ build."""
    cli_version = "1.1.0"  # bump when the CLI itself changes
    server_version: dict = {}
    try:
        server_version = request(cfg, "GET", "/api/version", timeout=5) or {}
    except ApiError as e:
        if not args.json:
            print(f"cli:    {cli_version}")
            print(f"server: unreachable ({e})", file=sys.stderr)
            return
        _json_out({"cli": cli_version, "server_error": str(e)})
        sys.exit(2)
    if args.json:
        _json_out({"cli": cli_version, "server": server_version})
    else:
        print(f"cli:        {cli_version}")
        print(f"server:     {server_version.get('version')}")
        print(f"api:        {server_version.get('api_version')}")
        print(f"python:     {server_version.get('python')}")
        print(f"platform:   {server_version.get('platform')}")
        if server_version.get("git_commit"):
            print(f"git:        {server_version['git_commit']}")


def cmd_about(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/about")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_openapi(cfg: Config, args) -> None:
    """Fetch the FastAPI OpenAPI document — full spec for code generators."""
    data = request(cfg, "GET", "/openapi.json", timeout=30)
    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(args.out)
    else:
        _json_out(data)


def cmd_schema(cfg: Config, args) -> None:
    """Per-model TTS schema: every accepted param with default + range."""
    if args.model:
        data = request(cfg, "GET", f"/api/schema/tts/{args.model}")
        if args.json:
            _json_out(data)
            return
        print(f"model: {data['id']}  ({data['name']})")
        print(f"  desc:     {data.get('desc') or '-'}")
        print(f"  weights:  {data.get('weights_repo') or '-'}  ({data.get('weights_size') or '-'})")
        caps = data.get("capabilities", {})
        print(f"  caps:     ref_audio={caps.get('ref_audio')} ref_text={caps.get('ref_text')} "
              f"style_tags={caps.get('style_tags')} builtin_voices={caps.get('builtin_voices')}")
        print(f"  formats:  {', '.join(data.get('output_formats', []))}")
        print(f"  timeout:  {data.get('infer_timeout_sec')}s")
        params = data.get("params", {})
        if params:
            print()
            rows = [[name, str(p.get("default")), f"{p.get('min')}–{p.get('max')}",
                     p.get("format", ""), "(pipeline)" if p.get("pipeline") else ""]
                    for name, p in sorted(params.items())]
            _print_table(rows, headers=["param", "default", "range", "fmt", "scope"])
        fields = data.get("fields", {})
        if fields:
            print()
            print("non-slider fields:")
            for n, f in fields.items():
                print(f"  {n:18s} {f.get('type'):8s} default={f.get('default')!r}")
        return
    data = request(cfg, "GET", "/api/schema/tts")
    if args.json:
        _json_out(data)
        return
    rows = [[m["id"], m["name"], len(m.get("params", {})),
             ",".join(k for k, v in m.get("capabilities", {}).items() if v) or "-"]
            for m in _field(data, "models").values()]
    _print_table(rows, headers=["id", "name", "params", "caps"])


def cmd_capabilities(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/tts/{args.model}/capabilities")
    if args.json:
        _json_out(data)
        return
    print(f"model:    {data['id']} ({data['name']})")
    caps = data.get("capabilities", {})
    print(f"caps:     {', '.join(k for k, v in caps.items() if v) or '(none)'}")
    print(f"params:   {', '.join(data.get('params', [])) or '(none)'}")
    print(f"fields:   {', '.join(data.get('fields', [])) or '(none)'}")
    print(f"voices:   {data.get('voice_count', 0)} voice(s)")
    print(f"formats:  {', '.join(data.get('output_formats', []))}")


# --- Diagnostics ---

def cmd_diagnose(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/diagnostics", timeout=60)
    if args.json:
        _json_out(data)
        return
    ok = data.get("ok")
    print(f"overall: {'OK' if ok else 'PROBLEMS DETECTED'}")
    print()
    print("binaries:")
    for name, b in data.get("binaries", {}).items():
        marker = "OK " if b.get("present") else "MISS"
        print(f"  [{marker}] {name:12s} {b.get('version') or '-'}")
    gpus = data.get("gpu", [])
    print()
    if gpus:
        print(f"gpu: {len(gpus)} device(s)")
        for g in gpus:
            print(f"  [{g['index']}] {g['name']}  {g['vram_used_mb']}/{g['vram_total_mb']}MB  util={g['utilization_pct']}%  drv={g['driver']}")
    else:
        print("gpu: none detected")
    print()
    print(f"hf_token: present={data.get('hf_token', {}).get('present')} legacy={data.get('hf_token', {}).get('legacy')}")
    print(f"disk:     free={data.get('disk', {}).get('free_gb')} GB / {data.get('disk', {}).get('total_gb')} GB")
    print()
    print("cache_in_opt:")
    for k, v in data.get("cache_in_opt", {}).items():
        print(f"  [{'OK' if v else 'NO'}] {k}")
    print()
    print("workers:")
    ws = data.get("workers", {})
    print(f"  total={ws.get('total')} ready={ws.get('ready')} loading={ws.get('loading')} "
          f"busy={ws.get('busy')} dead={ws.get('dead')}")
    print()
    print("models (install):")
    for mid, info in sorted(data.get("models", {}).items()):
        print(f"  {mid:12s} {info.get('status'):16s} pkgs={info.get('packages_installed')} "
              f"weights={info.get('weights_downloaded')}")


def cmd_disk(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/disk", timeout=120)
    if args.json:
        _json_out(data)
        return
    rows = []
    for name, info in data.get("directories", {}).items():
        size_gb = info.get("bytes", 0) / (1024**3)
        rows.append([name, "yes" if info.get("exists") else "no",
                     f"{size_gb:.2f} GB", str(info.get("files", 0)), info.get("path", "")])
    _print_table(rows, headers=["dir", "exists", "size", "files", "path"])
    print()
    print("mount free space:")
    for name, m in data.get("mounts", {}).items():
        if "error" in m:
            print(f"  {name}: error {m['error']}")
            continue
        free_gb = m.get("free", 0) / (1024**3)
        total_gb = m.get("total", 0) / (1024**3)
        print(f"  {name}: {free_gb:.1f} GB free of {total_gb:.1f} GB")


def cmd_env(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/env")
    if args.json:
        _json_out(data)
        return
    env = data.get("env", {})
    rows = [[k, v if v is not None else "(unset)"] for k, v in sorted(env.items())]
    _print_table(rows, headers=["var", "value"])


# --- Logs tail (different from logs follow) ---

def cmd_logs_tail(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/logs/tail",
                   query={"file": args.file, "lines": args.lines}, timeout=30)
    if args.json:
        _json_out(data)
        return
    if not data.get("exists"):
        print(f"(file does not exist: {data.get('path')})", file=sys.stderr)
        return
    for line in data.get("lines", []):
        print(line)


# --- Jobs extras ---

def cmd_jobs_manifest(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/jobs/{args.id}/manifest")
    if args.json:
        _json_out(data)
        return
    job = data.get("job", {})
    _print_kv({k: v for k, v in job.items()
               if k not in ("chunks", "input_text", "expected_files", "missing_files", "parameters")})
    files = data.get("files", [])
    if files:
        print()
        rows = [[f["name"], str(f["size"]), f["modified"], "dir" if f["is_dir"] else "file"]
                for f in files]
        _print_table(rows, headers=["name", "size", "modified", "kind"])


def download(cfg, endpoint, destination, timeout=300):
    response = request(cfg, "GET", endpoint, timeout=timeout, stream=True)
    try:
        if str(destination) == "-":
            shutil.copyfileobj(response, sys.stdout.buffer, length=65536)
        else:
            destination = Path(destination)
            temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
            try:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output, length=65536)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        response.close()


def cmd_jobs_zip(cfg: Config, args) -> None:
    out = args.out or f"job_{args.id[:8]}.zip"
    download(cfg, f"/api/jobs/{args.id}/zip", out)
    if out != "-":
        print(str(out))


def cmd_jobs_stream(cfg: Config, args) -> None:
    """SSE stream of job progress. Exits 0 when the job completes."""
    resp = None
    try:
        resp = request(cfg, "GET", f"/api/jobs/{args.id}/stream",
                       timeout=args.timeout, stream=True)
        last = None
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                entry = json.loads(payload)
            except json.JSONDecodeError:
                continue
            status = entry.get("status")
            if entry == last:
                continue
            last = entry
            if args.json:
                _json_out(entry)
            else:
                print(f"  [{status}] chunks={entry.get('chunks_completed')}/{entry.get('total_chunks')}")
            if status in ("completed", "failed", "cancelled", "incomplete", "gone", "stream_timeout"):
                if status != "completed":
                    sys.exit(5)
                return
        _abort("Job stream closed before completion", code=5)
    except ApiError as e:
        _abort(str(e), code=2)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def cmd_jobs_rerun_chunk(cfg: Config, args) -> None:
    data = request(cfg, "POST",
                   f"/api/jobs/{args.id}/chunks/{args.chunk_idx}/rerun", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- Projects extras ---

def cmd_projects_get(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/projects/{urlparse.quote(args.name)}")
    if args.json:
        _json_out(data)
        return
    print(f"project: {data['name']}")
    print(f"path:    {data['path']}")
    jobs = data.get("jobs", [])
    if jobs:
        print()
        rows = [[(j.get("job_id") or "?")[:8], j.get("model"), j.get("status"),
                 f"{j.get('chunks_completed', 0)}/{j.get('total_chunks', 0)}",
                 j.get("final_file") or "-"]
                for j in jobs]
        _print_table(rows, headers=["id", "model", "status", "chunks", "final"])
    files = data.get("files", [])
    if files:
        print()
        rows = [[f["name"], str(f["size"]), f["modified"]] for f in files]
        _print_table(rows, headers=["file", "size", "modified"])


def cmd_projects_delete(cfg: Config, args) -> None:
    if not args.yes:
        _abort(f"refusing to delete project '{args.name}' without --yes", code=1)
    data = request(cfg, "DELETE", f"/api/projects/{urlparse.quote(args.name)}")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_projects_zip(cfg: Config, args) -> None:
    out = args.out or f"{args.name}.zip"
    download(cfg, f"/api/projects/{urlparse.quote(args.name, safe='')}/zip", out)
    if out != "-":
        print(str(out))


def cmd_voices_info(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/voices/{urlparse.quote(args.name)}/info")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_voices_rename(cfg: Config, args) -> None:
    data = request(cfg, "POST",
                   f"/api/voices/{urlparse.quote(args.old)}/rename",
                   json_body={"new_name": args.new})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- Maintenance ---

def cmd_maint_gc_jobs(cfg: Config, args) -> None:
    data = request(cfg, "POST", "/api/maintenance/gc-jobs",
                   json_body={"older_than_hours": args.older_than})
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_maint_clear_cache(cfg: Config, args) -> None:
    kinds = args.kind if args.kind else ["tmp"]
    data = request(cfg, "POST", "/api/maintenance/clear-cache",
                   json_body={"kinds": kinds}, timeout=120)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_maint_kill_stale(cfg: Config, args) -> None:
    data = request(cfg, "POST", "/api/maintenance/kill-stale-workers", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_maint_restart_workers(cfg: Config, args) -> None:
    q = {"model": args.model} if args.model else None
    data = request(cfg, "POST", "/api/maintenance/restart-workers",
                   query=q, timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


def cmd_maint_cleanup_temp(cfg: Config, args) -> None:
    data = request(cfg, "POST", "/api/maintenance/cleanup-temp", timeout=60)
    if args.json:
        _json_out(data)
    else:
        _print_kv(data)


# --- Dryrun + Preview (one-shot generation helpers) ---

def cmd_dryrun(cfg: Config, args) -> None:
    body = _tts_build_body(args)
    data = request(cfg, "POST", f"/api/tts/{args.model}/dryrun",
                   json_body=body, timeout=30)
    if args.json:
        _json_out(data)
        return
    print(f"model:           {data['model']}")
    print(f"text_chars:      {data['text_chars']}")
    print(f"chunk_count:     {data['chunk_count']}")
    print(f"too_many_chunks: {data['too_many_chunks']}")
    print(f"max_chunks:      {data['max_chunks']}")
    print(f"est_runtime_sec: {data['estimated_runtime_sec']}")
    sp = data.get("save_path", {})
    if sp.get("input"):
        print(f"save_path:       input={sp['input']!r} -> dir={sp.get('resolved_dir')!r} stem={sp.get('resolved_stem')!r}")
        if sp.get("error"):
            print(f"save_path_error: {sp['error']}")
    print()
    rows = [[str(c["index"]), str(c["chars"]), c["text"][:80]]
            for c in data.get("chunks", [])]
    _print_table(rows, headers=["#", "chars", "text"])


def cmd_preview(cfg: Config, args) -> None:
    """Quick canned-text generate, save to ./preview_<model>.wav by default."""
    text = args.text or f"This is a quick {args.model} preview test."
    body: dict[str, Any] = {"text": text}
    if args.voice:
        body["voice"] = args.voice
    out = Path(args.out) if args.out else Path(f"preview_{args.model}.wav")
    data = request(cfg, "POST", f"/api/tts/{args.model}",
                   json_body=body, timeout=args.timeout)
    def _emit_written() -> None:
        if args.json:
            summary = {"written_to": str(out), "model": args.model}
            for k in ("duration_sec", "voice", "sample_rate"):
                if k in data:
                    summary[k] = data[k]
            _json_out(summary)
        else:
            print(str(out))

    audio_b64 = data.get("audio_base64")
    if audio_b64:
        out.write_bytes(base64.b64decode(audio_b64))
        _emit_written()
    elif data.get("output_url"):
        raw = request(cfg, "GET", data["output_url"], timeout=120)
        if isinstance(raw, str):
            raw = raw.encode()
        out.write_bytes(raw)
        _emit_written()
    else:
        if args.json:
            _json_out(data)
        else:
            _print_kv(data)


# ---------------------------------------------------------------------------
# Audio editor commands (effects, render, edits library)
# ---------------------------------------------------------------------------

def _to_server_path(path: str) -> str:
    """Translate a Windows path to a WSL ``/mnt/<drive>/`` path.

    The server runs inside WSL2, so paths the orchestrator sees as
    ``E:\\foo\\bar.wav`` need to be sent as ``/mnt/e/foo/bar.wav``. If the
    input is already a POSIX path it passes through unchanged.
    """
    if not isinstance(path, str) or len(path) < 2:
        return path
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _parse_audio_source_spec(spec: str) -> dict:
    """Parse a CLI source spec into the API JSON shape.

    Forms:
        final:JOB              -> kind="final"
        chunk:JOB:N            -> kind="chunk", index=N
        edit:JOB:NAME          -> kind="edit", name=NAME
        path:/abs/file.wav     -> kind="path", path=...
        path:E:\\abs\\file.wav   -> auto-translated to /mnt/e/abs/file.wav
    """
    if not isinstance(spec, str) or ":" not in spec:
        _abort(f"Invalid source spec: {spec!r}. Expected one of "
               "final:JOB, chunk:JOB:N, edit:JOB:NAME, path:/abs/file.wav")
    kind, _, rest = spec.partition(":")
    if kind == "final":
        if not rest:
            _abort("final source needs a job id: final:JOB")
        return {"kind": "final", "job_id": rest}
    if kind == "chunk":
        job, _, idx = rest.partition(":")
        if not job or not idx:
            _abort("chunk source needs job id and integer index: chunk:JOB:N")
        try:
            idx_int = int(idx)
        except ValueError:
            _abort(f"chunk index must be an integer (got {idx!r})")
        return {"kind": "chunk", "job_id": job, "index": idx_int}
    if kind == "edit":
        job, _, name = rest.partition(":")
        if not job or not name:
            _abort("edit source needs job id and filename: edit:JOB:NAME")
        return {"kind": "edit", "job_id": job, "name": name}
    if kind == "path":
        if not rest:
            _abort("path source needs an absolute path: path:/abs/file.wav")
        return {"kind": "path", "path": _to_server_path(rest)}
    _abort(f"Unknown source kind: {kind!r}. Use final, chunk, edit, or path.")


def _coerce_param_value(v: str) -> Any:
    """Coerce a 'k=v' value to int/float/bool when it looks numeric/boolean."""
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    try:
        if "." in v or "e" in v.lower():
            return float(v)
        return int(v)
    except ValueError:
        return v


def _parse_effect_spec(spec: str) -> dict:
    """Parse 'TYPE[:k=v,k=v,...]' into {type, params}."""
    name, _, kvs = spec.partition(":")
    name = name.strip()
    if not name:
        _abort(f"Effect spec missing type: {spec!r}")
    params: dict = {}
    if kvs:
        for kv in kvs.split(","):
            kv = kv.strip()
            if not kv:
                continue
            if "=" not in kv:
                _abort(f"Bad effect param '{kv}' in {spec!r}. Use k=v")
            k, _, v = kv.partition("=")
            params[k.strip()] = _coerce_param_value(v.strip())
    return {"type": name, "params": params}


def cmd_audio_effects(cfg: Config, args) -> None:
    data = request(cfg, "GET", "/api/audio/effects")
    effects = (data or {}).get("effects", {})
    if args.json:
        _json_out(data)
        return
    if not effects:
        print("(no effects)")
        return
    rows = []
    for name, defaults in effects.items():
        params_str = ", ".join(f"{k}={v}" for k, v in defaults.items())
        rows.append([name, params_str])
    _print_table(rows, headers=["effect", "default params"])


def cmd_audio_render(cfg: Config, args) -> None:
    source = _parse_audio_source_spec(args.source)
    chain: list = []
    if args.chain_file:
        try:
            loaded = json.loads(Path(args.chain_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _abort(f"--chain-file: {e}")
        if not isinstance(loaded, list):
            _abort("--chain-file must contain a JSON list of {type, params}")
        chain.extend(loaded)
    for spec in args.effect or []:
        chain.append(_parse_effect_spec(spec))
    body: dict[str, Any] = {
        "source": source,
        "edits": chain,
        "output_format": args.format,
        "overwrite": bool(args.overwrite),
    }
    if args.output_name:
        body["output_name"] = args.output_name
    if args.output_path:
        body["output_path"] = _to_server_path(args.output_path)
    data = request(cfg, "POST", "/api/audio/render",
                   json_body=body, timeout=args.timeout)
    if args.json:
        _json_out(data)
        return
    print(f"saved:        {data.get('output_path') or data.get('edit_path')}")
    print(f"name:         {data.get('edit_name')}")
    print(f"duration:     {data.get('duration_sec')}s")
    print(f"sample_rate:  {data.get('sample_rate')} Hz")
    print(f"size:         {data.get('size_bytes')} bytes")
    if data.get("job_id"):
        print(f"job_id:       {data.get('job_id')}")


def cmd_audio_edits(cfg: Config, args) -> None:
    data = request(cfg, "GET", f"/api/audio/edits/{args.job_id}")
    edits = (data or {}).get("edits", [])
    if args.json:
        _json_out(data)
        return
    if not edits:
        print("(no edits)")
        return
    rows = [[e.get("name", ""), e.get("format", ""),
             str(e.get("size_bytes", "-")), e.get("modified", "-")]
            for e in edits]
    _print_table(rows, headers=["name", "fmt", "size", "modified"])


def cmd_audio_edit_get(cfg: Config, args) -> None:
    safe_name = urlparse.quote(args.name, safe="")
    raw = request(cfg, "GET",
                  f"/api/audio/edits/{args.job_id}/{safe_name}",
                  timeout=300)
    if isinstance(raw, str):
        raw = raw.encode()
    if args.out == "-":
        sys.stdout.buffer.write(raw)
        return
    out = Path(args.out) if args.out else Path(args.name)
    out.write_bytes(raw)
    print(str(out))


def cmd_audio_edit_delete(cfg: Config, args) -> None:
    safe_name = urlparse.quote(args.name, safe="")
    data = request(cfg, "DELETE",
                   f"/api/audio/edits/{args.job_id}/{safe_name}")
    if args.json:
        _json_out(data)
    else:
        _print_kv(data or {"status": "deleted", "name": args.name})


def cmd_audio_peaks(cfg: Config, args) -> None:
    source = _parse_audio_source_spec(args.source)
    body = {"source": source, "buckets": int(args.buckets)}
    data = request(cfg, "POST", "/api/audio/peaks",
                   json_body=body, timeout=60)
    if args.json:
        _json_out(data)
        return
    print(f"duration:    {data.get('duration_sec')}s")
    print(f"sample_rate: {data.get('sample_rate')} Hz")
    print(f"buckets:     {data.get('buckets')}")
    print("(peaks/rms data omitted in human mode — use --json for full output)")


# --- Open Explorer (Windows convenience) ---

def _wsl_distro_name(registry: dict | None) -> str | None:
    """Best-effort: dig the WSL distro name out of the discovery registry.

    Used to render \\\\wsl$\\<distro>\\... UNC paths so Windows Explorer can
    open files that live inside the Linux ext4 filesystem (under /opt).
    """
    if not registry:
        return None
    extra = registry.get("extra") or {}
    for key in ("wsl_distro", "distro", "wsl"):
        if extra.get(key):
            return extra[key]
    return None


def cmd_open(cfg: Config, args) -> None:
    """Open a TTS-related directory in Windows Explorer.

    /mnt/<x>/... paths translate to <X>:\\..., /opt/... paths translate to
    \\\\wsl$\\<distro>\\opt\\... so Explorer can reach into the WSL ext4
    filesystem.
    """
    data = request(cfg, "GET", "/api/about")
    paths = data.get("paths", {})
    pmap = {
        "app": paths.get("app_dir"),
        "voices": paths.get("voices_dir"),
        "output": paths.get("output_dir"),
        "projects": paths.get("projects_dir"),
        "models": paths.get("models_dir"),
        "logs": str(Path(paths.get("output_dir", "")) / "logs") if paths.get("output_dir") else None,
        "run": paths.get("run_dir"),
    }
    target = pmap.get(args.kind)
    if not target:
        _abort(f"unknown kind '{args.kind}'. choose from: {sorted(pmap)}")

    win_path: str | None = None
    if target.startswith("/mnt/") and len(target) > 6:
        drive = target[5].upper()
        rest = target[6:].replace("/", "\\")
        win_path = f"{drive}:\\{rest}"
    elif target.startswith("/"):
        # Linux-side path — try to translate via the discovery registry
        distro = _wsl_distro_name(cfg.registry)
        if distro:
            rest = target.lstrip("/").replace("/", "\\")
            win_path = f"\\\\wsl$\\{distro}\\{rest}"
        else:
            _abort(f"path '{target}' is inside WSL but no distro name discovered "
                   f"(registry missing 'extra.wsl_distro'). Open manually.", code=2)
    else:
        win_path = target.replace("/", "\\")

    if sys.platform == "win32":
        # This verb only ever opens a directory in Explorer. The path comes from
        # the (trusted, but overridable) server/registry, so refuse anything that
        # isn't an existing directory — os.startfile performs a ShellExecute
        # "open" that would otherwise launch an executable / dangerous handler.
        if not Path(win_path).is_dir():
            _abort(f"not a directory (refusing to open): {win_path}", code=1)
        try:
            os.startfile(win_path)  # noqa: S606
        except OSError as e:
            _abort(f"open failed for {win_path}: {e}", code=1)
        print(f"opened: {win_path}")
    else:
        print(win_path)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tts",
        description="TTS Server CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url", help="Override server URL (default: discovery -> bridge :9300)")
    p.add_argument("--token", help="Override API token (default: discovery -> registry)")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    sub = p.add_subparsers(dest="cmd", required=True)

    # health / wait-ready / config
    sp = sub.add_parser("health", help="Check server health")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("wait-ready", help="Block until server (and optionally a model) is ready")
    sp.add_argument("--model")
    sp.add_argument("--timeout", type=float, default=60.0)
    sp.add_argument("--interval", type=float, default=0.5)
    sp.set_defaults(func=cmd_wait_ready)

    sp = sub.add_parser("config", help="Show server configuration (or one key)")
    sp.add_argument("key", nargs="?", help="Dotted key path (e.g. models_dir, defaults.kokoro)")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("models", help="List models with install + load status")
    sp.add_argument("--installed", action="store_true")
    sp.add_argument("--loaded", action="store_true")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("devices", help="List GPUs / CPU and active workers")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("status", help="Combined install + worker status")
    sp.add_argument("--watch", action="store_true", help="Loop forever, refresh every interval")
    sp.add_argument("--interval", type=float, default=3.0)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("install", help="Install a model (or 'all')")
    sp.add_argument("model")
    sp.add_argument("--wait", action="store_true", help="Block until install finishes")
    sp.add_argument("--timeout", type=float, default=3600.0)
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("remove", help="Remove a model's weights and overrides")
    sp.add_argument("model")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("token", help="Print the API token (auto-discovered)")
    sp.set_defaults(func=cmd_token)

    sp = sub.add_parser("peers", help="List discovery registry peers")
    sp.add_argument("--local", action="store_true",
                    help="Read registry from filesystem directly (no server roundtrip)")
    sp.set_defaults(func=cmd_peers)

    # Workers
    pw = sub.add_parser("workers", help="Worker management")
    pwsub = pw.add_subparsers(dest="action", required=True)
    pwl = pwsub.add_parser("list"); pwl.set_defaults(func=cmd_workers_list)
    pws = pwsub.add_parser("spawn")
    pws.add_argument("model")
    pws.add_argument("--device")
    pws.add_argument("--precision", choices=["fp32", "fp16", "bf16"])
    pws.set_defaults(func=cmd_workers_spawn)
    pwk = pwsub.add_parser("kill"); pwk.add_argument("worker_id"); pwk.set_defaults(func=cmd_workers_kill)
    pwc = pwsub.add_parser("scale")
    pwc.add_argument("model"); pwc.add_argument("count", type=int); pwc.add_argument("--device")
    pwc.set_defaults(func=cmd_workers_scale)

    # Model load/unload (alias of workers spawn for one)
    pm = sub.add_parser("model", help="Model load/unload (convenience)")
    pmsub = pm.add_subparsers(dest="action", required=True)
    pml = pmsub.add_parser("load"); pml.add_argument("model"); pml.add_argument("--device")
    pml.set_defaults(func=cmd_model_load)
    pmu = pmsub.add_parser("unload"); pmu.add_argument("model"); pmu.set_defaults(func=cmd_model_unload)

    # Voices
    pv = sub.add_parser("voices", help="Reference voice files")
    pvsub = pv.add_subparsers(dest="action", required=True)
    pvl = pvsub.add_parser("list"); pvl.set_defaults(func=cmd_voices_list)
    pvu = pvsub.add_parser("upload"); pvu.add_argument("file"); pvu.set_defaults(func=cmd_voices_upload)
    pvd = pvsub.add_parser("delete"); pvd.add_argument("name"); pvd.set_defaults(func=cmd_voices_delete)
    pvt = pvsub.add_parser("transcribe")
    pvt.add_argument("name"); pvt.add_argument("--size", default="base")
    pvt.set_defaults(func=cmd_voices_transcribe)
    pvp = pvsub.add_parser("download"); pvp.add_argument("name"); pvp.add_argument("--out")
    pvp.set_defaults(func=cmd_voices_play)

    # Whisper
    pwh = sub.add_parser("whisper", help="Whisper verification model management")
    pwhsub = pwh.add_subparsers(dest="action", required=True)
    pwhi = pwhsub.add_parser("info"); pwhi.set_defaults(func=cmd_whisper_info)
    pwhl = pwhsub.add_parser("load"); pwhl.add_argument("size"); pwhl.set_defaults(func=cmd_whisper_load)
    pwhu = pwhsub.add_parser("unload"); pwhu.add_argument("size"); pwhu.set_defaults(func=cmd_whisper_unload)

    # TTS - biggest verb
    pt = sub.add_parser("tts", help="Generate TTS audio (sync by default)")
    pt.add_argument("model")
    pt.add_argument("--text")
    pt.add_argument("--text-file")
    pt.add_argument("--out", help="Save audio to this path (otherwise inline base64 returned)")
    pt.add_argument("--format", choices=["wav", "mp3", "ogg", "flac", "m4a"], default=None)
    pt.add_argument("--ref", help="Reference audio file path")
    pt.add_argument("--save-path", dest="save_path",
                    help="Server-side relative path under projects_output/")
    pt.add_argument("--async", dest="async_submit", action="store_true",
                    help="Submit and return job_id immediately, don't wait")
    pt.add_argument("--request-timeout", type=float, default=1800.0)
    # Per-param flags, registered with explicit types so --speed etc. send
    # JSON numbers (not strings) and so --help documents them.
    for name, kind in _TTS_NUMERIC_TYPES.items():
        pt.add_argument(f"--{name.replace('_', '-')}", dest=name, type=kind, default=None)
    pt.add_argument("--voice", default=None)
    pt.add_argument("--reference-text", dest="reference_text", default=None)
    pt.add_argument("--language", default=None)
    pt.add_argument("--device", default=None)
    pt.add_argument("--whisper-model", dest="whisper_model", default=None)
    pt.add_argument("--voice-description", dest="voice_description", default=None)
    pt.add_argument("--verify-whisper", dest="verify_whisper",
                    action="store_const", const=True, default=None)
    pt.add_argument("--no-postprocess", dest="skip_post_process",
                    action="store_const", const=True, default=None)
    pt.set_defaults(func=cmd_tts)

    # Jobs
    pj = sub.add_parser("jobs", help="Job lifecycle commands")
    pjsub = pj.add_subparsers(dest="action", required=True)
    pjl = pjsub.add_parser("list")
    pjl.add_argument("--status"); pjl.add_argument("--limit", type=int)
    pjl.set_defaults(func=cmd_jobs_list)
    pjg = pjsub.add_parser("get"); pjg.add_argument("id"); pjg.set_defaults(func=cmd_jobs_get)
    pjo = pjsub.add_parser("output"); pjo.add_argument("id"); pjo.add_argument("--out")
    pjo.set_defaults(func=cmd_jobs_output)
    pjc = pjsub.add_parser("cancel"); pjc.add_argument("id"); pjc.set_defaults(func=cmd_jobs_cancel)
    pjr = pjsub.add_parser("recover"); pjr.add_argument("id"); pjr.set_defaults(func=cmd_jobs_recover)
    pjd = pjsub.add_parser("delete"); pjd.add_argument("ids", nargs="+"); pjd.set_defaults(func=cmd_jobs_delete)
    pje = pjsub.add_parser("edit-chunk")
    pje.add_argument("id"); pje.add_argument("chunk_idx", type=int); pje.add_argument("--text", required=True)
    pje.set_defaults(func=cmd_jobs_edit_chunk)
    pjca = pjsub.add_parser("chunk-audio")
    pjca.add_argument("id"); pjca.add_argument("chunk_idx", type=int); pjca.add_argument("--out")
    pjca.set_defaults(func=cmd_jobs_chunk_audio)
    pjch = pjsub.add_parser("chunks"); pjch.add_argument("id"); pjch.set_defaults(func=cmd_jobs_chunks)
    pjw = pjsub.add_parser("wait")
    pjw.add_argument("id"); pjw.add_argument("--timeout", type=float, default=1800.0)
    pjw.add_argument("--interval", type=float, default=2.0)
    pjw.set_defaults(func=cmd_jobs_wait)

    # Projects
    pp = sub.add_parser("projects", help="List project directories")
    pp.set_defaults(func=cmd_projects_list)

    # Logs
    pl = sub.add_parser("logs", help="Stream server logs")
    plsub = pl.add_subparsers(dest="action", required=True)
    plf = plsub.add_parser("follow")
    plf.add_argument("--filter", help="Regex to filter log messages")
    plf.add_argument("--reconnect", action="store_true", help="Reconnect on disconnect")
    plf.set_defaults(func=cmd_logs_follow)

    # HF Token
    phf = sub.add_parser("hf-token", help="Manage HuggingFace token")
    phfs = phf.add_subparsers(dest="action", required=True)
    phfg = phfs.add_parser("get"); phfg.set_defaults(func=cmd_hf_token_get)
    phfse = phfs.add_parser("set")
    phfse.add_argument("token", nargs="?", help="Token, '-' for stdin, omit to remove")
    phfse.set_defaults(func=cmd_hf_token_set)

    # Batch
    pb = sub.add_parser("batch", help="Submit many TTS jobs from a JSON spec")
    pb.add_argument("file")
    pb.add_argument("--request-timeout", type=float, default=1800.0)
    pb.set_defaults(func=cmd_batch)

    # Audio editor (post-process effects)
    pa = sub.add_parser("audio",
        help="Audio editor — apply effects to TTS output (gain, EQ, pitch, etc.)")
    pasub = pa.add_subparsers(dest="action", required=True)

    pae = pasub.add_parser("effects", help="List available effects and their defaults")
    pae.set_defaults(func=cmd_audio_effects)

    par = pasub.add_parser("render",
        help="Apply an effect chain to a source and save the result")
    par.add_argument("source",
        help="Source: final:JOB | chunk:JOB:N | edit:JOB:NAME | path:/abs/file.wav")
    par.add_argument("--effect", action="append", metavar="TYPE[:k=v,...]",
        help="Effect spec, repeatable. e.g. --effect gain:db=3 --effect pitch:semitones=-2")
    par.add_argument("--chain-file", metavar="PATH",
        help="JSON file with a list of {type, params} entries (concatenated with --effect)")
    par.add_argument("--output-name", metavar="NAME",
        help="Output filename without extension (saved under <job_dir>/edits/)")
    par.add_argument("--output-path", metavar="PATH",
        help="Absolute output path (under VOICE_DIR / OUTPUT_DIR / PROJECTS_OUTPUT)")
    par.add_argument("--format", default="wav",
        choices=["wav", "flac", "ogg", "mp3"],
        help="Output format when --output-path is not given (default: wav)")
    par.add_argument("--overwrite", action="store_true",
        help="Overwrite existing output (otherwise the render fails with 409)")
    par.add_argument("--timeout", type=float, default=600.0,
        help="HTTP timeout in seconds (default: 600)")
    par.set_defaults(func=cmd_audio_render)

    pal = pasub.add_parser("edits", help="List edits saved for a job")
    pal.add_argument("job_id")
    pal.set_defaults(func=cmd_audio_edits)

    pag = pasub.add_parser("edit-get", help="Download an edit file")
    pag.add_argument("job_id")
    pag.add_argument("name")
    pag.add_argument("--out", help="Output path or '-' for stdout (default: ./<name>)")
    pag.set_defaults(func=cmd_audio_edit_get)

    pad = pasub.add_parser("edit-delete", help="Delete an edit file")
    pad.add_argument("job_id")
    pad.add_argument("name")
    pad.set_defaults(func=cmd_audio_edit_delete)

    pap = pasub.add_parser("peaks",
        help="Get waveform peaks for a source (use --json for full payload)")
    pap.add_argument("source",
        help="Source: final:JOB | chunk:JOB:N | edit:JOB:NAME | path:/abs/file.wav")
    pap.add_argument("--buckets", type=int, default=512,
        help="Number of envelope buckets (64-4096, default 512)")
    pap.set_defaults(func=cmd_audio_peaks)

    # Version / about / OpenAPI / schema / capabilities
    sp = sub.add_parser("version", help="Print CLI + server version")
    sp.set_defaults(func=cmd_version)

    sp = sub.add_parser("about", help="Server summary: paths, ports, uptime, GPU")
    sp.set_defaults(func=cmd_about)

    sp = sub.add_parser("openapi", help="Fetch the FastAPI OpenAPI document")
    sp.add_argument("--out", help="Write to this file instead of stdout")
    sp.set_defaults(func=cmd_openapi)

    sp = sub.add_parser("schema",
                        help="Show TTS model schema (params, defaults, ranges)")
    sp.add_argument("model", nargs="?",
                    help="Model id; omit for a summary table of all models")
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("capabilities",
                        aliases=["caps"],
                        help="Compact capability summary for one model")
    sp.add_argument("model")
    sp.set_defaults(func=cmd_capabilities)

    # Diagnostics
    sp = sub.add_parser("diagnose",
                        help="Run server self-check (binaries, GPU, HF token, models)")
    sp.set_defaults(func=cmd_diagnose)

    sp = sub.add_parser("disk", help="Disk usage by directory + mount free space")
    sp.set_defaults(func=cmd_disk)

    sp = sub.add_parser("env", help="Show TTS-relevant environment variables")
    sp.set_defaults(func=cmd_env)

    # Logs tail (in addition to logs follow) — extend the existing 'logs' verb
    logs_parser = sub.choices.get("logs")
    if logs_parser is not None:
        logs_sub = next(
            (a for a in logs_parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if logs_sub is not None:
            plt = logs_sub.add_parser("tail", help="Read last N lines from a persistent log file")
            plt.add_argument("--lines", type=int, default=200)
            plt.add_argument("--file", default="server",
                             choices=["server", "bridge", "setup", "startup"])
            plt.set_defaults(func=cmd_logs_tail)

    # Jobs extras (manifest, zip, stream, rerun-chunk)
    jobs_parser = sub.choices.get("jobs")
    if jobs_parser is not None:
        jobs_sub = next(
            (a for a in jobs_parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if jobs_sub is not None:
            jm = jobs_sub.add_parser("manifest", help="Show full job.json + file listing")
            jm.add_argument("id")
            jm.set_defaults(func=cmd_jobs_manifest)

            jz = jobs_sub.add_parser("zip", help="Download whole job dir as .zip")
            jz.add_argument("id")
            jz.add_argument("--out")
            jz.set_defaults(func=cmd_jobs_zip)

            js = jobs_sub.add_parser("stream", help="SSE stream of job progress")
            js.add_argument("id")
            js.add_argument("--timeout", type=float, default=1800.0)
            js.set_defaults(func=cmd_jobs_stream)

            jr = jobs_sub.add_parser("rerun-chunk",
                                      help="Re-render a single chunk (resets and triggers recovery)")
            jr.add_argument("id")
            jr.add_argument("chunk_idx", type=int)
            jr.set_defaults(func=cmd_jobs_rerun_chunk)

    # Keep the original plural listing verb; singular `project` owns operations
    # on one named project.
    pproj = sub.add_parser("project",
                           help="Project ops: get details, zip, delete one project")
    pproj_sub = pproj.add_subparsers(dest="action", required=True)
    pp_get = pproj_sub.add_parser("get",
                                  help="Show project files + jobs")
    pp_get.add_argument("name")
    pp_get.set_defaults(func=cmd_projects_get)

    pp_zip = pproj_sub.add_parser("zip", help="Download project as .zip")
    pp_zip.add_argument("name")
    pp_zip.add_argument("--out")
    pp_zip.set_defaults(func=cmd_projects_zip)

    pp_del = pproj_sub.add_parser("delete", help="Delete a project (irreversible)")
    pp_del.add_argument("name")
    pp_del.add_argument("--yes", action="store_true",
                        help="Required confirmation flag")
    pp_del.set_defaults(func=cmd_projects_delete)

    # Voices extras (info, rename) — add to existing voices subparsers
    voices_parser = sub.choices.get("voices")
    if voices_parser is not None:
        voices_sub = next(
            (a for a in voices_parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if voices_sub is not None:
            vi = voices_sub.add_parser("info", help="Show voice metadata + transcription")
            vi.add_argument("name")
            vi.set_defaults(func=cmd_voices_info)

            vr = voices_sub.add_parser("rename", help="Rename a voice in place")
            vr.add_argument("old")
            vr.add_argument("new")
            vr.set_defaults(func=cmd_voices_rename)

    # Maintenance
    pm = sub.add_parser("maintenance", aliases=["maint"],
                        help="Server-side maintenance: GC jobs, clear caches, restart workers")
    pm_sub = pm.add_subparsers(dest="action", required=True)

    pm_gc = pm_sub.add_parser("gc-jobs",
                              help="Delete jobs older than N hours")
    pm_gc.add_argument("--older-than", dest="older_than", type=int, default=72)
    pm_gc.set_defaults(func=cmd_maint_gc_jobs)

    pm_cc = pm_sub.add_parser("clear-cache",
                              help="Wipe a cache tree (does NOT touch model weights or venv)")
    pm_cc.add_argument("--kind", action="append",
                       choices=["tmp", "pip", "hub", "datasets", "torch",
                                "xdg", "modules", "logs", "all"],
                       help="Repeat to clear multiple kinds; default is 'tmp'")
    pm_cc.set_defaults(func=cmd_maint_clear_cache)

    pm_ks = pm_sub.add_parser("kill-stale",
                              help="Kill workers not in (ready, busy)")
    pm_ks.set_defaults(func=cmd_maint_kill_stale)

    pm_rw = pm_sub.add_parser("restart-workers",
                              help="Kill all workers (next request will respawn them)")
    pm_rw.add_argument("--model", help="Only restart workers for this model")
    pm_rw.set_defaults(func=cmd_maint_restart_workers)

    pm_ct = pm_sub.add_parser("cleanup-temp",
                              help="Remove orphaned raw_*/assembled_*/ref_* temp files")
    pm_ct.set_defaults(func=cmd_maint_cleanup_temp)

    # Dryrun + preview (one-shot helpers)
    pd = sub.add_parser("dryrun",
                        help="Preview chunking + estimate without inferring")
    pd.add_argument("model")
    pd.add_argument("--text")
    pd.add_argument("--text-file")
    pd.add_argument("--ref")
    pd.add_argument("--format", choices=["wav", "mp3", "ogg", "flac", "m4a"], default=None)
    # Add every TTS param flag generically — _tts_build_body uses getattr,
    # so missing flags would break the body builder. Numeric params reuse the
    # shared type table so dryrun serializes them as JSON numbers, identically
    # to the `tts` verb.
    for name in _TTS_PARAM_FIELDS:
        pd.add_argument(f"--{name.replace('_', '-')}", dest=name,
                        type=_TTS_NUMERIC_TYPES.get(name), default=None)
    pd.set_defaults(func=cmd_dryrun)

    pp = sub.add_parser("preview",
                        help="Quick test generation with canned text")
    pp.add_argument("model")
    pp.add_argument("--text", help="Text override (default: short canned phrase)")
    pp.add_argument("--voice")
    pp.add_argument("--out", help="Output file (default: preview_<model>.wav)")
    pp.add_argument("--timeout", type=float, default=300.0)
    pp.set_defaults(func=cmd_preview)

    # Open Explorer (Windows convenience)
    po = sub.add_parser("open",
                        help="Open a TTS directory in Explorer (Windows only)")
    po.add_argument("kind",
                    choices=["app", "voices", "output", "projects", "models",
                             "logs", "run"],
                    help="Which directory to open")
    po.set_defaults(func=cmd_open)

    return p


_GLOBAL_FLAGS = {"--json"}
_GLOBAL_VALUE_FLAGS = {"--url", "--token"}


def _collect_value_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """All long-option strings (across every subparser) that consume a value.

    Used by the hoister so a value that merely happens to equal --json/--url/
    --token is not mistaken for a global flag.
    """
    found: set[str] = set()
    seen: set[int] = set()

    def walk(p: argparse.ArgumentParser) -> None:
        if id(p) in seen:
            return
        seen.add(id(p))
        for act in p._actions:
            takes_value = not isinstance(
                act,
                (argparse._StoreTrueAction, argparse._StoreFalseAction,
                 argparse._StoreConstAction, argparse._HelpAction,
                 argparse._VersionAction, argparse._CountAction),
            ) and getattr(act, "nargs", None) != 0
            if takes_value:
                for opt in act.option_strings:
                    if opt.startswith("--"):
                        found.add(opt)
            if isinstance(act, argparse._SubParsersAction):
                for sub in act.choices.values():
                    walk(sub)

    walk(parser)
    return found


def _hoist_global_flags(argv: list[str], value_options: set[str]) -> list[str]:
    """Move --json/--url/--token to the front so they work after subcommands.

    Stops scanning at the first ``--`` sentinel, so anything after is treated
    as positional and never hoisted (lets users pass literal ``--json`` etc.
    as values: ``tts kokoro -- --text "--json starts the file"``).

    A token that is the VALUE of a preceding value-consuming option (e.g.
    ``--text --url`` or ``--filter --json``) is left in place rather than
    mistaken for a global flag. ``value_options`` is the set of every long
    option (global or per-verb) that consumes an argument.
    """
    head: list[str] = []
    tail: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            tail.extend(argv[i:])
            break
        if a in _GLOBAL_FLAGS:
            head.append(a)
            i += 1
        elif a in _GLOBAL_VALUE_FLAGS:
            head.append(a)
            if i + 1 < n:
                head.append(argv[i + 1])
                i += 2
            else:
                i += 1
        elif "=" in a and a.split("=", 1)[0] in (_GLOBAL_FLAGS | _GLOBAL_VALUE_FLAGS):
            head.append(a)
            i += 1
        elif a in value_options and "=" not in a:
            # A value-consuming option (any verb): keep it AND its value in
            # place so the value is never re-read as a global flag.
            tail.append(a)
            if i + 1 < n:
                tail.append(argv[i + 1])
                i += 2
            else:
                i += 1
        else:
            tail.append(a)
            i += 1
    return head + tail


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    argv = _hoist_global_flags(argv, _collect_value_option_strings(parser))
    args = parser.parse_args(argv)
    cfg = Config(url=args.url, token=args.token)
    try:
        args.func(cfg, args)
    except ApiError as e:
        if args.json:
            _json_out({"error": str(e), "status": e.status})
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2 if e.status == 0 else 1
    except (KeyError, TypeError, ValueError) as e:
        # Residual shape mismatch from an unexpected response payload — emit a
        # clean error + stable exit code rather than a raw traceback.
        if args.json:
            _json_out({"error": str(e), "status": 0})
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("(interrupted)", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
