"""Rebind portable user-data paths after registration or a folder move."""
from pathlib import Path
import os
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "server"))
from portable_runtime import validate_app_binding


def configure(app_root: Path = root) -> None:
    validate_app_binding(app_root)
    state = Path("/opt/tts_server")
    values = {
        "APP_DIR": str(app_root), "MODELS_DIR": str(state / "data"),
        "VOICES_DIR": str(app_root / "voices"), "OUTPUT_DIR": str(app_root / "output"),
        "PROJECTS_DIR": str(app_root / "projects_output"), "CACHE_DIR": str(state / "cache"),
        "TMPDIR": str(state / "cache/tmp"), "RUN_DIR": str(app_root / "output/run"),
        "SECRETS_DIR": str(app_root / "secrets"), "DISCOVERY_DIR": str(app_root / "output/run/registry"),
        "HF_TOKEN_FILE": str(app_root / "secrets/hf_token"), "VENV_DIR": str(state / "venv"),
        "OVERRIDES_DIR": str(state / "overrides"), "REPOS_DIR": str(state / "repos"),
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Portable paths cannot contain newlines")
    state.mkdir(parents=True, exist_ok=True)
    link = state / "server"
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"Refusing to replace a real code directory at {link}")
    temporary = state / f"env.conf.{os.getpid()}.tmp"
    temporary.write_text("".join(f'{key}="{value}"\n' for key, value in values.items()), encoding="utf-8")
    temporary.replace(state / "env.conf")
    if link.is_symlink():
        link.unlink()
    link.symlink_to(app_root / "server", target_is_directory=True)


if __name__ == "__main__":
    configure()
