"""
Сборка УСТАНОВЩИКА в один .exe через PyInstaller.

Запуск (Windows, из папки my_mc_launcher):
    pip install pyinstaller
    python build_installer.py

Результат:
    dist/AmaterasuSetup.exe   ← это и есть Setup.exe, который раздаёшь людям

По умолчанию установщик СКАЧИВАЕТ лаунчер с GitHub Releases
(см. GITHUB_ZIP_URL в installer.py).

Если хочешь ОФФЛАЙН-установщик (всё внутри одного .exe), сначала собери
лаунчер (python build_launcher.py), потом запусти:
    python build_installer.py --offline
— тогда Amaterasu.zip будет вшит в Setup.exe и скачивание не потребуется.
"""

import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.absolute()
SETUP_NAME = "AmaterasuSetup"
SEP = ";" if os.name == "nt" else ":"
OFFLINE = "--offline" in sys.argv


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

    icon_ico = ROOT / "assets" / "icon_amaterasu.ico"
    icon = ROOT / "assets" / "icon_amaterasu.png"
    icon_arg = []
    if os.name == "nt":
        if not icon_ico.exists() and icon.exists():
            try:
                from PIL import Image
                Image.open(icon).save(icon_ico, sizes=[(256, 256), (128, 128),
                                                        (64, 64), (32, 32), (16, 16)])
            except Exception:
                pass
        if icon_ico.exists():
            icon_arg = ["--icon", str(icon_ico)]

    # Ресурсы установщика (иконка для окон мастера)
    add_data = []
    assets = ROOT / "assets"
    if assets.exists():
        add_data += ["--add-data", f"{assets}{SEP}assets"]

    if OFFLINE:
        pkg = ROOT / "Amaterasu.zip"
        if not pkg.exists():
            print("Amaterasu.zip не найден. Сначала: python build_launcher.py")
            sys.exit(1)
        add_data += ["--add-data", f"{pkg}{SEP}."]
        print("Режим: ОФФЛАЙН (лаунчер вшит в установщик)")
    else:
        print("Режим: ОНЛАЙН (установщик скачает лаунчер с GitHub)")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", SETUP_NAME,
        "--windowed",
        "--onefile",                  # установщик — один файл
        *icon_arg,
        *add_data,
        "--hidden-import", "PyQt6.QtSvg",
        str(ROOT / "installer.py"),
    ]
    print("Команда сборки:\n ", " ".join(cmd), "\n")
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    out = ROOT / "dist" / (SETUP_NAME + (".exe" if os.name == "nt" else ""))
    print(f"\n✅ Готово!\n  Установщик: {out}")
    print("  Раздавай этот файл пользователям.")


if __name__ == "__main__":
    build()
