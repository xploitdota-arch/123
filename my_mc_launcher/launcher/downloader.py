import requests
from tqdm import tqdm
from pathlib import Path

def download_file(url: str, dest: Path) -> bool:
    """Скачивает файл с прогресс-баром"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return True
    
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            with tqdm(total=total, unit='iB', unit_scale=True, desc=dest.name) as bar:
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        bar.update(len(chunk))
        return True
    except Exception as e:
        print(f"❌ Ошибка загрузки {dest.name}: {e}")
        return False
