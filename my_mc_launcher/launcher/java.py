import subprocess
import os
from pathlib import Path

def find_java(min_version: int = 8) -> str | None:
    """Ищет подходящую Java в системе"""
    candidates = [
        "java",
        r"C:\Program Files\Eclipse Adoptium\jdk-17-hotspot\bin\java.exe",
        r"C:\Program Files\Java\jdk-17\bin\java.exe",
        "/usr/lib/jvm/java-17-openjdk/bin/java",
    ]
    
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            ver_out = subprocess.check_output([path, "-version"], stderr=subprocess.STDOUT).decode()
            import re
            match = re.search(r'version "(\d+)', ver_out)
            if match and int(match.group(1)) >= min_version:
                return path
        except Exception:
            continue
    return None
