from __future__ import annotations

from pathlib import Path
from typing import Iterable


def recorded_sidecar_path(
    job: dict,
    job_dir: Path,
    recorded_keys: Iterable[tuple[str, str | None]],
    fallback_name: str,
) -> Path:
    """Resolve a recorded sidecar by basename inside one job directory.

    Recorded values may be absolute WSL paths. Only their basename is trusted,
    and symlinks or paths resolving outside the job directory are ignored.
    """
    base = Path(job_dir).resolve()
    values: list[object] = []
    for outer, inner in recorded_keys:
        value = job.get(outer)
        if inner is not None:
            value = value.get(inner) if isinstance(value, dict) else None
        if value:
            values.append(value)
    for value in values:
        text = str(value)
        if not text or "\x00" in text:
            continue
        name = Path(text).name
        if name in {"", ".", ".."}:
            continue
        candidate = base / name
        try:
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(base):
            return resolved

    fallback = Path(fallback_name)
    if fallback.name != fallback_name or fallback_name in {"", ".", ".."}:
        raise ValueError("fallback_name must be one safe filename")
    return base / fallback_name
