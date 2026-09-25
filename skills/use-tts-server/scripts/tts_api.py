#!/usr/bin/env python3
"""Small authenticated client for the local portable TTS Server API."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
import sys
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


APP_ROOT = Path(__file__).resolve().parents[3]
REGISTRY = APP_ROOT / "output" / "run" / "registry" / "tts_server.json"


def discover() -> tuple[str, str, str]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    url = data["endpoints"]["api"].rstrip("/")
    auth = data["auth"]
    return url, auth["header"], auth["token"]


def load_body(value: str | None) -> bytes | None:
    if value is None:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    parsed = json.loads(value)
    return json.dumps(parsed).encode("utf-8")


def safe_output(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = APP_ROOT / path
    path = path.resolve()
    path.relative_to(APP_ROOT.resolve())
    return path


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise urllib.error.HTTPError(req.full_url, code, "Refusing cross-origin redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call TTS Server without exposing its API token."
    )
    parser.add_argument("method", choices=("GET", "POST", "PUT", "DELETE"))
    parser.add_argument("path", help="API path beginning with /api/")
    parser.add_argument("--data", help="JSON string or @path-to-json")
    parser.add_argument("--output", help="Save a binary response inside the app")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if not args.path.startswith("/api/"):
        parser.error("path must begin with /api/")
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be between 1 and 900 seconds")

    base, header, token = discover()
    body = load_body(args.data)
    headers = {header: token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + args.path,
        data=body,
        headers=headers,
        method=args.method,
    )
    try:
        with urllib.request.build_opener(SameOriginRedirect()).open(request, timeout=args.timeout) as response:
            content_type = response.headers.get_content_type()
            if args.output:
                output = safe_output(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".part")
                try:
                    with temporary.open("wb") as stream:
                        shutil.copyfileobj(response, stream, length=65536)
                    temporary.replace(output)
                finally:
                    temporary.unlink(missing_ok=True)
                print(json.dumps({"saved": str(output), "bytes": output.stat().st_size}))
                return 0
            if content_type != "application/json":
                print(f"Binary response ({content_type}). Use --output.")
                return 0
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"API request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(json.loads(payload), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
