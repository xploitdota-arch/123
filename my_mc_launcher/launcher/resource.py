import sys
from pathlib import Path

def get_resource_path(relative_path: str) -> Path:
    """
    Возвращает правильный путь к ресурсу.
    Работает и при запуске из исходников, и в собранном .exe (PyInstaller).
    """
    if hasattr(sys, '_MEIPASS'):
        # Запущено из PyInstaller
        base_path = Path(sys._MEIPASS)
    else:
        # Обычный запуск
        base_path = Path(__file__).parent.parent  # amateras/launcher -> amateras/

    return base_path / relative_path
