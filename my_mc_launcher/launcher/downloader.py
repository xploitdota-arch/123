import urllib.request
from pathlib import Path

def download_file(url: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, open(path, 'wb') as out_file:
        shutil_copy = True # simplified
        out_file.write(response.read())