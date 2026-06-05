import subprocess
import os
import re
from pathlib import Path

def get_required_java_version(mc_version: str) -> int:
    """Определяет минимальную версию Java для данной версии Minecraft"""
    try:
        parts = mc_version.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2].split("-")[0]) if len(parts) > 2 else 0
    except ValueError:
        return 17

    if major == 1:
        if minor >= 21:
            return 21
        if minor == 20 and patch >= 5:
            return 21
        if minor >= 17:
            return 17
    return 8

def find_java(min_version: int = 8) -> str | None:
    """Ищет подходящую Java в системе и в .minecraft/runtime"""
    mc_dir = Path(__file__).parent.parent / ".minecraft"
    
    candidates = [
        "java",
        r"C:\Program Files\Eclipse Adoptium\jdk-21-hotspot\bin\java.exe",
        r"C:\Program Files\Eclipse Adoptium\jdk-17-hotspot\bin\java.exe",
        r"C:\Program Files\Eclipse Adoptium\jdk-8-hotspot\bin\java.exe",
        r"C:\Program Files\Java\jdk-21\bin\java.exe",
        r"C:\Program Files\Java\jdk-17\bin\java.exe",
        r"C:\Program Files\Java\jdk-1.8\bin\java.exe",
        "/usr/lib/jvm/java-21-openjdk/bin/java",
        "/usr/lib/jvm/java-17-openjdk/bin/java",
        "/usr/lib/jvm/java-8-openjdk/bin/java",
    ]
    
    runtime_dir = mc_dir / "runtime"
    if runtime_dir.exists():
        for jvm_dir in runtime_dir.rglob("bin/java.exe"):
            candidates.insert(1, str(jvm_dir))
        for jvm_dir in runtime_dir.rglob("bin/java"):
            candidates.insert(1, str(jvm_dir))
    
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            ver_out = subprocess.check_output([path, "-version"], stderr=subprocess.STDOUT).decode()
            match = re.search(r'version "?(\d+)', ver_out)
            if match:
                ver = int(match.group(1))
                if ver == 1:
                    match = re.search(r'version "1\.(\d+)', ver_out)
                    if match:
                        ver = int(match.group(1))
                if ver >= min_version:
                    return path
        except Exception:
            continue
    return None