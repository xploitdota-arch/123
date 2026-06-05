"""
Amaterasu Launcher — Установщик (в стиле TLauncher)

Мастер установки:
  1. Приветствие
  2. Лицензионное соглашение
  3. Всё готово к установке (показ пути + опции)
  4. Установка (прогресс: скачивание с GitHub → распаковка → ярлык)
  5. Завершение

Логика:
  • Лаунчер ставится в  %LOCALAPPDATA%\\Amaterasu
  • Данные игры лежат в  %APPDATA%\\.amaterasu  (создаёт сам лаунчер)
  • Файлы скачиваются с GitHub Releases (zip), либо берутся из локальной папки
    рядом с установщиком (fallback для оффлайн-сборки).

Сборка в .exe — см. build_installer.py
"""

import sys
import os
import shutil
import zipfile
import tempfile
import subprocess
import urllib.request
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QLabel, QVBoxLayout, QHBoxLayout,
    QCheckBox, QTextEdit, QMessageBox, QProgressBar, QRadioButton, QWidget,
    QButtonGroup
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap, QIcon, QColor

# ────────────────────────────────────────────────────────────────────
#  КОНФИГУРАЦИЯ — поменяй под свой репозиторий/релиз
# ────────────────────────────────────────────────────────────────────
APP_NAME = "Amaterasu"
APP_VERSION = "1.0.0"

# Прямая ссылка на zip-архив лаунчера в GitHub Releases.
# Залей собранный Amaterasu (папку с Amaterasu.exe и ресурсами) в zip
# и опубликуй как Release Asset, затем впиши сюда прямую ссылку.
GITHUB_ZIP_URL = (
    "https://github.com/xploitdota-arch/amateras/releases/download/latest/Amaterasu.zip"
)

# Имя главного исполняемого файла лаунчера внутри архива (после распаковки)
LAUNCHER_EXE = "Amaterasu.exe"
# Если лаунчер запускается как python-скрипт (не .exe), укажи main.py
LAUNCHER_MAIN_PY = "main.py"

# Путь к ресурсам установщика (работает и из .exe PyInstaller, и из исходников)
def res_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent.absolute()

RES = res_dir()
ASSETS = RES / "assets"

# Куда устанавливаем лаунчер
def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME

# Папка данных игры (её создаёт сам лаунчер, но при чистой установке можем удалить)
def game_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / ".amaterasu"


# ────────────────────────────────────────────────────────────────────
#  ЯРЛЫКИ
# ────────────────────────────────────────────────────────────────────
def _create_shortcut(lnk_path: Path, target: Path, workdir: Path, icon: Path | None,
                     args: str = "") -> bool:
    """Создаёт .lnk через PowerShell. Возвращает True при успехе."""
    try:
        icon_line = ""
        if icon and icon.exists():
            icon_line = f'$s.IconLocation = "{icon}"'
        args_line = f'$s.Arguments = \'{args}\'' if args else ""
        ps = f'''
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut("{lnk_path}")
$s.TargetPath = "{target}"
{args_line}
$s.WorkingDirectory = "{workdir}"
{icon_line}
$s.Save()
'''
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return lnk_path.exists()
    except Exception:
        return False


def make_shortcuts(target_exe: Path, install_path: Path,
                   desktop: bool, start_menu: bool) -> list[str]:
    """Создаёт ярлыки. Возвращает список созданных путей."""
    created = []
    icon = install_path / "assets" / "icon_amaterasu.png"
    if not icon.exists():
        icon = target_exe  # exe сам содержит иконку

    # Если запуск через python (нет .exe), цель = pythonw + main.py
    is_exe = target_exe.suffix.lower() == ".exe"
    if is_exe:
        tgt, args = target_exe, ""
    else:
        tgt = Path(shutil.which("pythonw") or shutil.which("python") or "python")
        args = f'"{target_exe}"'

    if desktop:
        d = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        lnk = d / f"{APP_NAME}.lnk"
        if _create_shortcut(lnk, tgt, install_path, icon, args):
            created.append(str(lnk))

    if start_menu:
        sm = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        sm.mkdir(parents=True, exist_ok=True)
        lnk = sm / f"{APP_NAME}.lnk"
        if _create_shortcut(lnk, tgt, install_path, icon, args):
            created.append(str(lnk))

    return created


def write_uninstaller(install_path: Path):
    """Создаёт простой uninstall.bat."""
    desktop_lnk = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop" / f"{APP_NAME}.lnk"
    sm_lnk = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
    content = f'''@echo off
echo Удаление {APP_NAME}...
del /f /q "{desktop_lnk}" 2>nul
del /f /q "{sm_lnk}" 2>nul
timeout /t 1 >nul
rmdir /s /q "{install_path}"
echo Готово.
pause
'''
    try:
        (install_path / "uninstall.bat").write_text(content, encoding="cp1251")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
#  ПОТОК УСТАНОВКИ
# ────────────────────────────────────────────────────────────────────
class InstallWorker(QThread):
    progress = pyqtSignal(int)          # 0..100
    status = pyqtSignal(str)            # текст статуса
    finished_ok = pyqtSignal(list)      # список ярлыков
    failed = pyqtSignal(str)            # текст ошибки

    def __init__(self, clean: bool, desktop: bool, start_menu: bool):
        super().__init__()
        self.clean = clean
        self.desktop = desktop
        self.start_menu = start_menu

    def run(self):
        try:
            inst = install_dir()

            # 1. Чистая установка — удаляем старое
            if self.clean:
                self.status.emit("Удаление старых данных...")
                if inst.exists():
                    shutil.rmtree(inst, ignore_errors=True)
                gd = game_data_dir()
                if gd.exists():
                    shutil.rmtree(gd, ignore_errors=True)
            self.progress.emit(3)

            inst.mkdir(parents=True, exist_ok=True)

            # 2. Получаем zip с лаунчером
            tmp_zip = Path(tempfile.gettempdir()) / "amaterasu_pkg.zip"
            local_pkg = self._find_local_package()

            if local_pkg:
                self.status.emit("Копирование локального пакета...")
                shutil.copy2(local_pkg, tmp_zip)
                self.progress.emit(40)
            else:
                self.status.emit("Скачивание файлов с GitHub...")
                self._download(GITHUB_ZIP_URL, tmp_zip)

            # 3. Распаковка
            self.status.emit("Распаковка файлов...")
            self._extract(tmp_zip, inst)
            self.progress.emit(92)

            # 4. Ищем исполняемый файл лаунчера
            target = self._find_launcher(inst)
            if target is None:
                self.failed.emit(
                    "В архиве не найден исполняемый файл лаунчера "
                    f"({LAUNCHER_EXE} или {LAUNCHER_MAIN_PY})."
                )
                return

            # 5. Ярлыки + деинсталлятор
            self.status.emit("Создание ярлыков...")
            shortcuts = make_shortcuts(target, inst, self.desktop, self.start_menu)
            write_uninstaller(inst)
            self.progress.emit(98)

            # 6. Уборка
            try:
                tmp_zip.unlink(missing_ok=True)
            except Exception:
                pass

            self.progress.emit(100)
            self.status.emit("Установка завершена!")
            self.finished_ok.emit(shortcuts)

        except Exception as e:
            self.failed.emit(str(e))

    # ── helpers ──
    def _find_local_package(self) -> Path | None:
        """Ищет готовый zip рядом с установщиком (для оффлайн-сборки)."""
        for name in ("Amaterasu.zip", "amaterasu_pkg.zip", "launcher.zip"):
            p = RES / name
            if p.exists():
                return p
        return None

    def _download(self, url: str, dest: Path):
        req = urllib.request.Request(url, headers={"User-Agent": "Amaterasu-Installer/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 1024 * 256
            with open(dest, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total > 0:
                        # маппим 5..88%
                        pct = 5 + int(downloaded / total * 83)
                        self.progress.emit(min(pct, 88))
                        mb = downloaded / 1024 / 1024
                        tmb = total / 1024 / 1024
                        self.status.emit(f"Скачивание: {mb:.1f} / {tmb:.1f} МБ")

    def _extract(self, zip_path: Path, dest: Path):
        with zipfile.ZipFile(zip_path) as z:
            members = z.namelist()
            # Определяем общий корневой каталог (если архив с одной папкой внутри)
            top = self._common_top(members)
            total = len(members)
            for i, m in enumerate(members):
                z.extract(m, dest)
                if i % 20 == 0 and total:
                    self.progress.emit(92 + int(i / total * 0))  # держим 92
            # Если был общий корень — поднимаем содержимое на уровень выше
            if top:
                self._flatten(dest / top, dest)

    @staticmethod
    def _common_top(members: list[str]) -> str | None:
        tops = set()
        for m in members:
            m = m.replace("\\", "/").strip("/")
            if not m:
                continue
            tops.add(m.split("/")[0])
        if len(tops) == 1:
            only = tops.pop()
            return only
        return None

    @staticmethod
    def _flatten(src: Path, dest: Path):
        if not src.is_dir() or src == dest:
            return
        for item in src.iterdir():
            target = dest / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            shutil.move(str(item), str(dest))
        shutil.rmtree(src, ignore_errors=True)

    @staticmethod
    def _find_launcher(inst: Path) -> Path | None:
        exe = inst / LAUNCHER_EXE
        if exe.exists():
            return exe
        # поиск глубже
        for p in inst.rglob(LAUNCHER_EXE):
            return p
        py = inst / LAUNCHER_MAIN_PY
        if py.exists():
            return py
        for p in inst.rglob(LAUNCHER_MAIN_PY):
            return p
        return None


# ────────────────────────────────────────────────────────────────────
#  СТРАНИЦЫ МАСТЕРА
# ────────────────────────────────────────────────────────────────────
class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Добро пожаловать!")
        self.setSubTitle(f"Мастер установки {APP_NAME} Launcher")

        layout = QHBoxLayout()

        # Картинка слева
        img = QLabel()
        pic = ASSETS / "icon_amaterasu.png"
        if pic.exists():
            pm = QPixmap(str(pic)).scaled(
                140, 140, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            img.setPixmap(pm)
        img.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(img)

        text = QLabel(
            f"Этот Мастер поможет вам выполнить установку "
            f"<b>{APP_NAME} Launcher</b> на ваш компьютер.<br><br>"
            f"Для продолжения установки нажмите «<b>Продолжить</b>»."
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setStyleSheet("font-size: 14px;")
        layout.addWidget(text, 1)

        self.setLayout(layout)


class LicensePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Лицензионное соглашение")
        self.setSubTitle("Пожалуйста, ознакомьтесь с лицензионным соглашением.")

        layout = QVBoxLayout()

        license_text = QTextEdit()
        license_text.setReadOnly(True)
        license_text.setPlainText(
            f"{APP_NAME} Launcher — Лицензионное соглашение\n\n"
            f'"{APP_NAME}" является бесплатным программным обеспечением. '
            "Вы можете устанавливать, тестировать и использовать его без "
            "ограничений по времени.\n\n"
            "Вы имеете право делать архивные копии данного ПО и любой "
            "сопутствующей документации. Вы можете передавать копии этого ПО "
            "при условии, что не вносите изменений в оригинальный архив.\n\n"
            "Программа предоставляется «как есть», без каких-либо гарантий. "
            "Авторы не несут ответственности за любой ущерб, связанный с "
            "использованием программы.\n\n"
            "Minecraft является торговой маркой Mojang AB. Данный лаунчер не "
            "аффилирован с Mojang/Microsoft.\n\n"
            "Используя программу, вы соглашаетесь с условиями данного соглашения."
        )
        layout.addWidget(license_text)

        self.bg = QButtonGroup(self)
        self.rb_accept = QRadioButton("Я принимаю условия лицензионного соглашения.")
        self.rb_decline = QRadioButton("Я не согласен с пунктами лицензионного соглашения.")
        self.rb_decline.setChecked(True)
        self.bg.addButton(self.rb_accept)
        self.bg.addButton(self.rb_decline)
        self.rb_accept.toggled.connect(self.completeChanged)

        layout.addWidget(self.rb_accept)
        layout.addWidget(self.rb_decline)

        self.setLayout(layout)

    def isComplete(self):
        return self.rb_accept.isChecked()


class ReadyPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Всё готово к установке!")
        self.setSubTitle("Установка будет выполнена с данными параметрами.")

        layout = QVBoxLayout()

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.info)

        layout.addSpacing(8)
        self.cb_desktop = QCheckBox("Создать ярлык на рабочем столе")
        self.cb_desktop.setChecked(True)
        self.cb_startmenu = QCheckBox("Добавить в меню «Пуск»")
        self.cb_startmenu.setChecked(True)
        self.cb_clean = QCheckBox("Выполнить чистую установку (удалить старые данные)")
        layout.addWidget(self.cb_desktop)
        layout.addWidget(self.cb_startmenu)
        layout.addWidget(self.cb_clean)

        layout.addStretch()
        self.setLayout(layout)

    def initializePage(self):
        self.info.setText(
            "Папка установки:<br><b>{inst}</b><br><br>"
            "Папка данных Minecraft:<br><b>{data}</b><br><br>"
            "Нажмите «<b>Продолжить</b>» для начала установки.".format(
                inst=install_dir(), data=game_data_dir()
            )
        )


class InstallPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Установка")
        self.setSubTitle("Пожалуйста, подождите. Идёт установка...")
        self._done = False
        self._shortcuts = []
        self.worker = None

        layout = QVBoxLayout()

        self.status_lbl = QLabel("Подготовка...")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        layout.addWidget(self.log)

        layout.addStretch()
        self.setLayout(layout)

    def initializePage(self):
        wiz: "InstallerWizard" = self.wizard()  # type: ignore
        ready: ReadyPage = wiz.ready_page
        # Блокируем кнопки во время установки
        wiz.button(QWizard.WizardButton.BackButton).setEnabled(False)
        wiz.button(QWizard.WizardButton.NextButton).setEnabled(False)
        wiz.button(QWizard.WizardButton.CancelButton).setEnabled(False)

        self.worker = InstallWorker(
            clean=ready.cb_clean.isChecked(),
            desktop=ready.cb_desktop.isChecked(),
            start_menu=ready.cb_startmenu.isChecked(),
        )
        self.worker.progress.connect(self.bar.setValue)
        self.worker.status.connect(self._on_status)
        self.worker.finished_ok.connect(self._on_ok)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_status(self, s):
        self.status_lbl.setText(s)
        self.log.append(s)

    def _on_ok(self, shortcuts):
        self._done = True
        self._shortcuts = shortcuts
        self.log.append("✅ Готово!")
        wiz = self.wizard()
        wiz.button(QWizard.WizardButton.NextButton).setEnabled(True)
        wiz.button(QWizard.WizardButton.CancelButton).setEnabled(True)
        self.completeChanged.emit()

    def _on_fail(self, err):
        self.status_lbl.setText("Ошибка установки")
        self.log.append(f"❌ {err}")
        wiz = self.wizard()
        wiz.button(QWizard.WizardButton.CancelButton).setEnabled(True)
        QMessageBox.critical(self, "Ошибка установки", err)

    def isComplete(self):
        return self._done


class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Установка завершена")
        self.setSubTitle(f"{APP_NAME} Launcher успешно установлен!")

        layout = QVBoxLayout()
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.info)

        self.cb_run = QCheckBox(f"Запустить {APP_NAME} Launcher сейчас")
        self.cb_run.setChecked(True)
        layout.addWidget(self.cb_run)

        layout.addStretch()
        self.setLayout(layout)

    def initializePage(self):
        wiz = self.wizard()
        wiz.button(QWizard.WizardButton.CancelButton).setEnabled(False)
        self.info.setText(
            f"<b>{APP_NAME} Launcher</b> установлен в:<br>"
            f"<b>{install_dir()}</b><br><br>"
            "Нажмите «<b>Готово</b>», чтобы завершить работу мастера."
        )


# ────────────────────────────────────────────────────────────────────
#  МАСТЕР
# ────────────────────────────────────────────────────────────────────
class InstallerWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Установка {APP_NAME} Launcher")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(640, 480)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.NoCancelButtonOnLastPage, True)

        icon = ASSETS / "icon_amaterasu.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        # Русские подписи кнопок (как у TLauncher)
        self.setButtonText(QWizard.WizardButton.NextButton, "Продолжить")
        self.setButtonText(QWizard.WizardButton.BackButton, "Назад")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Отмена")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Готово")

        self.welcome_page = WelcomePage()
        self.license_page = LicensePage()
        self.ready_page = ReadyPage()
        self.install_page = InstallPage()
        self.finish_page = FinishPage()

        self.addPage(self.welcome_page)
        self.addPage(self.license_page)
        self.addPage(self.ready_page)
        self.addPage(self.install_page)
        self.addPage(self.finish_page)

        self.finished.connect(self._on_finished)

    def _on_finished(self, result):
        if result == QWizard.DialogCode.Accepted and self.finish_page.cb_run.isChecked():
            target = InstallWorker._find_launcher(install_dir())
            if target:
                try:
                    if target.suffix.lower() == ".exe":
                        subprocess.Popen([str(target)], cwd=str(install_dir()))
                    else:
                        py = shutil.which("pythonw") or shutil.which("python") or sys.executable
                        subprocess.Popen([py, str(target)], cwd=str(target.parent))
                except Exception:
                    pass


DARK_QSS = """
QWizard, QWizardPage, QWidget { background-color: #2b2b2b; color: #e6e6e6; }
QLabel { color: #e6e6e6; }
QTextEdit { background-color: #1e1e1e; color: #cccccc; border: 1px solid #3c3c3c; }
QCheckBox, QRadioButton { color: #e6e6e6; }
QProgressBar {
    border: 1px solid #3c3c3c; border-radius: 4px; text-align: center;
    background-color: #1e1e1e; color: #ffffff; height: 22px;
}
QProgressBar::chunk { background-color: #4caf50; border-radius: 3px; }
QPushButton {
    background-color: #3c3c3c; color: #e6e6e6; border: 1px solid #555;
    padding: 6px 18px; border-radius: 4px;
}
QPushButton:hover { background-color: #484848; }
QPushButton:default { background-color: #4caf50; color: white; border: none; }
QPushButton:default:hover { background-color: #5bbf60; }
QPushButton:disabled { background-color: #333; color: #777; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(f"{APP_NAME} Installer")

    icon = ASSETS / "icon_amaterasu.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    app.setStyleSheet(DARK_QSS)

    wiz = InstallerWizard()
    wiz.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
