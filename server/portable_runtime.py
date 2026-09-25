"""Validate a movable app folder against its own WSL backing disk."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess

DISTRO_PATTERN = re.compile(r"(?:linbox-TTS_Server|TTS-Server-V2-[a-f0-9]{12})\Z")


def distro_name() -> str:
    value = os.environ.get("WSL_DISTRO_NAME", "")
    if not DISTRO_PATTERN.fullmatch(value):
        raise RuntimeError(f"Not a dedicated TTS Server distribution: {value or 'unknown'}")
    return value


def windows_path(path: Path) -> str:
    result = subprocess.run(["/usr/bin/wslpath", "-w", str(path.resolve())],
                            capture_output=True, text=True, timeout=10, check=True)
    value = result.stdout.strip()
    if not re.match(r"^[A-Za-z]:\\", value):
        raise RuntimeError("The portable app must be on a local Windows drive")
    return value


def normalize_windows_path(value: str) -> str:
    value = value.strip().lstrip("\ufeff")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def registered_base_path() -> str:
    name = distro_name()
    powershell = (os.environ.get("TTS_WINDOWS_POWERSHELL")
                  or shutil.which("powershell.exe")
                  or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not Path(powershell).is_file():
        raise RuntimeError("Windows PowerShell interop is unavailable")
    command = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
        "$e=Get-ChildItem -LiteralPath 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss'|"
        "ForEach-Object{Get-ItemProperty -LiteralPath $_.PSPath}|"
        f"Where-Object{{$_.DistributionName -eq '{name}'}}|Select-Object -First 1;"
        "if($null -eq $e){exit 3};[Console]::Write($e.BasePath)"
    )
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"Cannot verify WSL registration for {name}")
    return result.stdout.strip()


def validate_app_binding(app_root: Path) -> None:
    root = app_root.resolve()
    if not (root / "app.json").is_file() or not (root / "server/tts_api_server.py").is_file():
        raise RuntimeError(f"Incomplete portable app folder: {root}")
    actual = normalize_windows_path(registered_base_path())
    expected = normalize_windows_path(windows_path(root / "wsl"))
    if actual != expected:
        raise RuntimeError(f"The selected TTS distro is registered at {actual!r}; expected {expected!r}")
