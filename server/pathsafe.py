# pathsafe.py
"""Shared path-containment primitive.

Single source of truth for the resolved "is this path inside that directory"
check used by the API server, workers, and job manager. Keeping one definition
prevents their path boundaries from silently diverging.
"""

from pathlib import Path


def is_within(path: Path, base: Path) -> bool:
    """Return True if ``path`` is the same as, or nested under, ``base``.

    Both operands are fully resolved (symlinks and ``..`` segments collapsed)
    before comparison, so this is safe to use as a path-traversal containment
    check.
    """
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False
