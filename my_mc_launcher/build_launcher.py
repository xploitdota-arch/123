"""
Сборка ЛАУНЧЕРА в один .exe через PyInstaller.

Запуск (Windows, из папки my_mc_launcher):
    pip install pyinstaller
    python build_launcher.py

Результат:
    dist/Amaterasu/Amaterasu.exe   (+ассеты рядом)
    Amaterasu.zip                  (готовый архив для GitHub Releases)

Этот zip нужно залить в GitHub Releases как ассет с именем Amaterasu.zip,
чтобы установщик мог его скачать (см. GITHUB_ZIP_URL в installer.py).
"""

import os
import sys
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.absolute()
APP_NAME = "Amaterasu"
SEP = ";" if os.name == "nt" else ":"


def have_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa
        return True
    except ImportError:
        return False


def build():
    if not have_pyinstaller():
        print("PyInstaller не установлен. Установи:  pip install pyinstaller")
        sys.exit(1)

    icon = ROOT / "assets" / "icon_amaterasu.png"
    icon_ico = ROOT / "assets" / "icon_amaterasu.ico"
    # PyInstaller на Windows хочет .ico — конвертируем при наличии Pillow
    icon_arg = []
    if os.name == "nt":
        if not icon_ico.exists() and icon.exists():
            try:
                from PIL import Image
                Image.open(icon).save(icon_ico, sizes=[(256, 256), (128, 128),
                                                        (64, 64), (32, 32), (16, 16)])
                print(f"Создан {icon_ico}")
            except Exception as e:
                print(f"Не удалось создать .ico ({e}), exe будет без иконки")
        if icon_ico.exists():
            icon_arg = ["--icon", str(icon_ico)]

    # Данные, которые нужно положить рядом с exe (--add-data SRC;DEST)
    add_data = []
    for folder in ("assets", "fonts"):
        p = ROOT / folder
        if p.exists():
            add_data += ["--add-data", f"{p}{SEP}{folder}"]
    # большие файлы NeoForge — лаунчер ждёт их рядом с собой
    for f in ("neoforge-21.4.157-installer-fat.jar", "neoforge-full-pack.zip",
              "launcher_settings.json"):
        p = ROOT / f
        if p.exists():
            add_data += ["--add-data", f"{p}{SEP}."]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",                 # без консоли
        "--onedir",                   # папка (быстрее старт, проще ресурсы)
        *icon_arg,
        *add_data,
        # PyQt6 и minecraft_launcher_lib подтягиваем целиком
        "--collect-all", "minecraft_launcher_lib",
        "--hidden-import", "PyQt6.QtSvg",
        str(ROOT / "main.py"),
    ]
    print("Команда сборки:\n ", " ".join(cmd), "\n")
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    # Упаковка результата в zip
    dist_app = ROOT / "dist" / APP_NAME
    if not dist_app.exists():
        print("Сборка не найдена в dist/, что-то пошло не так")
        sys.exit(1)

    zip_path = ROOT / f"{APP_NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"Упаковка в {zip_path} ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in dist_app.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(dist_app))
    print(f"\n✅ Готово!\n  EXE: {dist_app / (APP_NAME + '.exe')}\n  ZIP: {zip_path}")
    print("\nДальше: залей Amaterasu.zip в GitHub Releases этого репозитория")
    print("       (тег latest), затем собери установщик: python build_installer.py")


if __name__ == "__main__":
    build()
