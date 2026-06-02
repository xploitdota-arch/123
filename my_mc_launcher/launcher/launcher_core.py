import subprocess
import os
from pathlib import Path
from launcher.api import get_version_info
from launcher.auth import get_offline_uuid

def build_classpath(libs_dir: Path, libraries: list) -> str:
    """Собирает classpath из скачанных библиотек"""
    cp = []
    for lib in libraries:
        if "natives" in lib or "rules" in lib:
            continue
        if "version" not in lib: continue
        name = lib["name"]
        version = lib["version"]
        jar_path = libs_dir / f"{name.replace('.', '/')}/{version}/{name}-{version}.jar"
        if jar_path.exists():
            cp.append(str(jar_path))
    return os.pathsep.join(cp)

def launch_minecraft(version_id: str, username: str, java_path: str, mc_dir: Path):
    """Запускает Minecraft"""
    ver_json = get_version_info(version_id)
    if not ver_json:
        print("❌ Не удалось загрузить version.json")
        return

    libs_dir = mc_dir / "libraries"
    classpath = build_classpath(libs_dir, ver_json["libraries"])
    
    jvm_args = ver_json.get("arguments", {}).get("jvm", [])
    game_args = ver_json.get("arguments", {}).get("game", [])
    
    uuid_val = get_offline_uuid(username)
    game_args.extend([
        "--uuid", uuid_val,
        "--username", username,
        "--versionType", "custom"
    ])

    cmd = [java_path, "-cp", classpath] + jvm_args + ["net.minecraft.client.main.Main"] + game_args
    print(f"🚀 Запуск: {' '.join(cmd)}")
    
    subprocess.run(cmd)
