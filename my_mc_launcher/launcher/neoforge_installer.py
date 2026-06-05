"""
NeoForge installer — runs processors from installer-fat.jar locally.
No VPN needed — all files come from installer.jar or Mojang Direct.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def _download(url: str, dest: Path, log_fn=print):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
    with urllib.request.urlopen(req, timeout=180) as resp:
        dest.write_bytes(resp.read())


def _maven_to_path(artifact: str) -> str:
    """Convert maven coordinate to file path.
    net.neoforged:neoforge:21.4.157:client -> net/neoforged/neoforge/21.4.157/neoforge-21.4.157-client.jar
    """
    parts = artifact.strip('[]').split(':')
    group = parts[0].replace('.', '/')
    name = parts[1]
    version = parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    ext = 'jar'
    if classifier and '@' in classifier:
        classifier, ext = classifier.split('@')
    elif '@' in version:
        version, ext = version.split('@')
        classifier = None

    if classifier:
        return f"{group}/{name}/{version}/{name}-{version}-{classifier}.{ext}"
    return f"{group}/{name}/{version}/{name}-{version}.{ext}"


def install_neoforge(mc_dir: Path, installer_path: Path, java_exe: str, log_fn=print):
    """
    Install NeoForge by running processors from installer-fat.jar.
    
    Steps:
    1. Extract all maven/ jars from installer → libraries/
    2. Extract data files (client.lzma, neoform, etc.)
    3. Run each processor using Java
    """
    libs_dir = mc_dir / "libraries"
    libs_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="nf_proc_"))

    with zipfile.ZipFile(str(installer_path), 'r') as izf:
        profile = json.loads(izf.read('install_profile.json'))

        # 1. Extract all maven/ files → libraries/
        log_fn("📂 Извлечение библиотек из инсталлера...")
        extracted = 0
        for member in izf.namelist():
            if member.startswith("maven/") and not member.endswith("/"):
                rel_path = member[len("maven/"):]
                dest = libs_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.write_bytes(izf.read(member))
                    extracted += 1
        log_fn(f"✅ Извлечено {extracted} файлов")

        # 2. Extract data files
        log_fn("📂 Извлечение данных...")
        data_dir = tmp_dir / "data"
        data_dir.mkdir(exist_ok=True)
        for member in izf.namelist():
            if member.startswith("data/") and not member.endswith("/"):
                dest = tmp_dir / member
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(izf.read(member))

        # 3. Resolve data paths
        data_map = {}
        for key, val in profile.get('data', {}).items():
            client_val = val.get('client', '')
            if client_val.startswith('[') and client_val.endswith(']'):
                # Maven artifact → path in libraries
                artifact_path = _maven_to_path(client_val)
                data_map[key] = str(libs_dir / artifact_path)
            elif client_val.startswith('/'):
                # Relative to installer jar → extracted to tmp
                data_map[key] = str(tmp_dir / client_val.lstrip('/'))
            elif client_val.startswith("'") and client_val.endswith("'"):
                data_map[key] = client_val.strip("'")
            else:
                data_map[key] = client_val

        # MINECRAFT_JAR
        mc_jar = mc_dir / "versions" / "1.21.4" / "1.21.4.jar"
        data_map['MINECRAFT_JAR'] = str(mc_jar)
        data_map['SIDE'] = 'client'
        data_map['ROOT'] = str(mc_dir)

        # 4. Run processors
        processors = profile.get('processors', [])
        client_processors = []
        for proc in processors:
            sides = proc.get('sides', [])
            if sides and 'client' not in sides:
                continue
            client_processors.append(proc)

        log_fn(f"🔧 Запуск {len(client_processors)} процессоров...")

        for i, proc in enumerate(client_processors):
            jar_name = proc['jar']
            args_template = proc.get('args', [])

            # Resolve jar path
            jar_path = libs_dir / _maven_to_path(jar_name)
            if not jar_path.exists():
                log_fn(f"⚠ Пропуск процессора {i + 1} — нет {jar_path.name}")
                continue

            # Build classpath
            classpath = [str(jar_path)]
            for cp_entry in proc.get('classpath', []):
                cp_path = libs_dir / _maven_to_path(cp_entry)
                if cp_path.exists():
                    classpath.append(str(cp_path))

            # Resolve arguments
            resolved_args = []
            for arg in args_template:
                if arg.startswith('{') and arg.endswith('}'):
                    key = arg[1:-1]
                    resolved_args.append(data_map.get(key, arg))
                elif arg.startswith('[') and arg.endswith(']'):
                    artifact_path = _maven_to_path(arg)
                    resolved_args.append(str(libs_dir / artifact_path))
                else:
                    resolved_args.append(arg)

            # Ensure output directories exist
            for arg in resolved_args:
                if arg.endswith(('.jar', '.txt', '.zip')):
                    Path(arg).parent.mkdir(parents=True, exist_ok=True)

            # Get main class from jar manifest
            main_class = None
            try:
                with zipfile.ZipFile(str(jar_path), 'r') as jf:
                    manifest = jf.read('META-INF/MANIFEST.MF').decode()
                    for line in manifest.split('\n'):
                        if line.startswith('Main-Class:'):
                            main_class = line.split(':', 1)[1].strip()
                            break
            except Exception:
                pass

            if not main_class:
                log_fn(f"⚠ Пропуск процессора {i + 1} — нет Main-Class")
                continue

            sep = ';' if os.name == 'nt' else ':'
            cp_str = sep.join(classpath)

            cmd = [java_exe, '-cp', cp_str, main_class] + resolved_args

            log_fn(f"   [{i + 1}/{len(client_processors)}] {jar_path.stem}...")

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300,
                    cwd=str(tmp_dir)
                )
                if result.returncode != 0:
                    # Show error but continue
                    err = result.stderr.strip().split('\n')[-1] if result.stderr else ''
                    log_fn(f"   ⚠ код {result.returncode}: {err[:80]}")
            except subprocess.TimeoutExpired:
                log_fn(f"   ⚠ таймаут")
            except Exception as e:
                log_fn(f"   ⚠ ошибка: {e}")

        log_fn("✅ Процессоры завершены")

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)
