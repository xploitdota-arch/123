"""
Auto-download Java 21 (Adoptium Temurin) — works without VPN.
"""

import os
import sys
import zipfile
import tarfile
import urllib.request
import tempfile
import shutil
from pathlib import Path

# Adoptium API — не заблокирован
ADOPTIUM_API = "https://api.adoptium.net/v3/binary/latest/{java_major}/ga/{os}/{arch}/jre/hotspot/normal/eclipse"


def _detect_platform():
    """Detect OS and arch for Adoptium API."""
    if sys.platform == "win32":
        os_name = "windows"
    elif sys.platform == "darwin":
        os_name = "mac"
    else:
        os_name = "linux"

    import platform
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = "x64"

    return os_name, arch


def get_java_dir(mc_dir: Path, java_major: int = 21) -> Path:
    """Path where we install Java."""
    return mc_dir / "java" / f"jre-{java_major}"


def find_java_executable(mc_dir: Path, java_major: int = 21) -> str | None:
    """Find installed Java executable."""
    java_dir = get_java_dir(mc_dir, java_major)
    if not java_dir.exists():
        return None

    # Search for java/java.exe in the extracted directory
    if sys.platform == "win32":
        candidates = list(java_dir.rglob("java.exe"))
    else:
        candidates = list(java_dir.rglob("java"))

    for c in candidates:
        if "bin" in str(c):
            return str(c)

    return None


def is_java_installed(mc_dir: Path, java_major: int = 21) -> bool:
    """Check if Java is already installed."""
    return find_java_executable(mc_dir, java_major) is not None


def download_java(mc_dir: Path, java_major: int = 21, log_fn=None):
    """Download and extract specified Java JRE from Adoptium (default Java 21)."""
    if log_fn is None:
        log_fn = print

    java_dir = get_java_dir(mc_dir, java_major)

    if is_java_installed(mc_dir, java_major):
        exe = find_java_executable(mc_dir, java_major)
        log_fn(f"☕ Java {java_major} уже установлена: {exe}")
        return exe

    os_name, arch = _detect_platform()
    url = ADOPTIUM_API.format(java_major=java_major, os=os_name, arch=arch)

    log_fn(f"☕ Скачивание Java {java_major} (Adoptium)...")

    # Download
    tmp_dir = Path(tempfile.mkdtemp(prefix="java_"))
    if os_name == "windows":
        archive_name = f"java{java_major}.zip"
    else:
        archive_name = f"java{java_major}.tar.gz"
    archive_path = tmp_dir / archive_name

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
    with urllib.request.urlopen(req, timeout=180) as resp:
        archive_path.write_bytes(resp.read())

    log_fn(f"☕ Распаковка Java {java_major}...")

    # Extract
    java_dir.mkdir(parents=True, exist_ok=True)

    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(str(archive_path), 'r') as zf:
            zf.extractall(str(java_dir))
    else:
        with tarfile.open(str(archive_path), 'r:gz') as tf:
            tf.extractall(str(java_dir))

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)

    exe = find_java_executable(mc_dir, java_major)
    if exe:
        log_fn(f"☕ Java {java_major} установлена: {Path(exe).name}")
        return exe
    else:
        log_fn("❌ Не удалось найти java после распаковки")
        return None
