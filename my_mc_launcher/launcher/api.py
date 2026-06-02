import json
import urllib.request
from pathlib import Path

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

def get_version_list() -> list[dict]:
    """Возвращает список всех версий из манифеста Mojang"""
    with urllib.request.urlopen(MANIFEST_URL) as resp:
        data = json.loads(resp.read())
    return data["versions"]

def get_version_info(version_id: str) -> dict | None:
    """Загружает version.json для конкретной версии"""
    versions = get_version_list()
    ver_entry = next((v for v in versions if v["id"] == version_id), None)
    if not ver_entry:
        return None
    
    with urllib.request.urlopen(ver_entry["url"]) as resp:
        return json.loads(resp.read())

# Тест (раскомментируй в main.py для проверки):
# print(get_version_info("1.20.4") is not None)
