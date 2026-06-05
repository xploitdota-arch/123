import json
import urllib.request
import time
from pathlib import Path

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"


def get_version_list(mc_dir: Path = None) -> list[dict]:
    """Загружает список версий. 2 попытки."""
    last_error = None

    for attempt in range(2):
        timeout = 60 + attempt * 60  # 60с, 120с
        try:
            print(f"[api] Загрузка версий (таймаут {timeout}с)...")
            req = urllib.request.Request(MANIFEST_URL)
            req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())

            if mc_dir:
                mc_dir.mkdir(parents=True, exist_ok=True)
                with open(mc_dir / "manifest.json", 'w', encoding='utf-8') as f:
                    json.dump(data, f)

            print(f"[api] ✅ Загружено {len(data['versions'])} версий")
            return data["versions"]

        except Exception as e:
            last_error = e
            print(f"[api] ❌ Попытка {attempt + 1}: {e}")
            if attempt < 1:
                time.sleep(2)

    # Fallback — кэш
    if mc_dir and (mc_dir / "manifest.json").exists():
        print("[api] Загружаю из кэша...")
        with open(mc_dir / "manifest.json", 'r', encoding='utf-8') as f:
            return json.load(f)["versions"]

    return []


def get_version_info(version_id: str) -> dict | None:
    try:
        versions = get_version_list()
        ver_entry = next((v for v in versions if v["id"] == version_id), None)
        if not ver_entry:
            return None
        req = urllib.request.Request(ver_entry["url"])
        req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[api] Ошибка загрузки версии: {e}")
        return None
