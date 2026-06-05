import sys
import os
import shutil
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QLabel, QVBoxLayout, QHBoxLayout,
    QCheckBox, QPushButton, QTextEdit, QMessageBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPixmap, QIcon

# Путь к папке лаунчера (где лежит этот installer.py)
LAUNCHER_DIR = Path(__file__).parent.absolute()

def create_desktop_shortcut():
    """Создаёт ярлык на рабочем столе (.lnk если возможно, иначе .bat)"""
    desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    
    # Создаём bat-версию
    bat_content = f'''@echo off
cd /d "{LAUNCHER_DIR}"
pythonw main.py
'''
    local_bat = LAUNCHER_DIR / "Amaterasu.bat"
    with open(local_bat, "w", encoding="cp1251") as f:
        f.write(bat_content)
    
    # Пробуем создать красивый .lnk через PowerShell
    try:
        lnk_path = desktop / "Amaterasu.lnk"
        icon_path = LAUNCHER_DIR / "assets" / "icon_amaterasu.png"
        ps = f'''
        $WshShell = New-Object -comObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut("{lnk_path}")
        $Shortcut.TargetPath = "pythonw.exe"
        $Shortcut.Arguments = '"{LAUNCHER_DIR / "main.py"}"'
        $Shortcut.WorkingDirectory = "{LAUNCHER_DIR}"
        if (Test-Path "{icon_path}") {{
            $Shortcut.IconLocation = "{icon_path},0"
        }}
        $Shortcut.Save()
        '''
        result = subprocess.run(["powershell", "-Command", ps], capture_output=True, text=True)
        if lnk_path.exists():
            return str(lnk_path)
    except Exception:
        pass
    
    # Fallback — .bat на рабочем столе
    desktop_bat = desktop / "Amaterasu.bat"
    with open(desktop_bat, "w", encoding="cp1251") as f:
        f.write(bat_content)
    return str(desktop_bat)

class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Добро пожаловать!")
        
        layout = QVBoxLayout()
        
        label = QLabel(
            "Этот Мастер поможет вам установить Amaterasu Launcher на ваш компьютер.\n\n"
            "Для продолжения установки нажмите «Продолжить»."
        )
        label.setWordWrap(True)
        label.setStyleSheet("font-size: 14px;")
        layout.addWidget(label)
        
        layout.addStretch()
        self.setLayout(layout)

class LicensePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Лицензионное соглашение")
        
        layout = QVBoxLayout()
        
        info = QLabel("Пожалуйста, ознакомьтесь с лицензионным соглашением.")
        layout.addWidget(info)
        
        license_text = QTextEdit()
        license_text.setReadOnly(True)
        license_text.setPlainText(
            'Amaterasu Launcher - Лицензионное соглашение\n\n'
            'Это бесплатное программное обеспечение.\n'
            'Вы можете использовать, копировать и распространять его.\n'
            'Не вносите изменений в оригинальный архив.\n\n'
            'Используя программу, вы соглашаетесь с условиями.'
        )
        layout.addWidget(license_text)
        
        self.accept_cb = QCheckBox("Я принимаю условия лицензионного соглашения.")
        self.accept_cb.toggled.connect(self.completeChanged)
        layout.addWidget(self.accept_cb)
        
        self.setLayout(layout)
    
    def isComplete(self):
        return self.accept_cb.isChecked()

class ReadyPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Всё готово к установке!")
        
        layout = QVBoxLayout()
        
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        
        self.clean_cb = QCheckBox("Выполнить чистую установку (удалить старые данные .amaterasu)")
        layout.addWidget(self.clean_cb)
        
        layout.addStretch()
        self.setLayout(layout)
    
    def initializePage(self):
        appdata = os.getenv("APPDATA", str(Path.home()))
        data_dir = Path(appdata) / ".amaterasu"
        self.info_label.setText(
            f"Установка будет выполнена с параметрами:\n\n"
            f"Папка данных Minecraft: {data_dir}\n"
            f"Ярлык на рабочем столе: Amaterasu\n\n"
            f"Нажмите «Продолжить» для начала установки."
        )

def install_launcher(clean: bool):
    appdata = os.getenv("APPDATA")
    if not appdata:
        appdata = str(Path.home())
    
    data_dir = Path(appdata) / ".amaterasu"
    
    if clean and data_dir.exists():
        try:
            shutil.rmtree(data_dir)
        except:
            pass
    
    data_dir.mkdir(parents=True, exist_ok=True)
    
    # Создаём ярлык
    shortcut = create_desktop_shortcut()
    
    # Можно создать также в меню Пуск (упрощённо)
    try:
        start_menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        start_menu.mkdir(parents=True, exist_ok=True)
        start_bat = start_menu / "Amaterasu.bat"
        shutil.copy2(LAUNCHER_DIR / "Amaterasu.bat", start_bat)
    except:
        pass
    
    return shortcut, str(data_dir)

class InstallerWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Установка Amaterasu Launcher")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(600, 450)
        
        icon_path = LAUNCHER_DIR / "assets" / "icon_amaterasu.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        
        self.addPage(WelcomePage())
        self.addPage(LicensePage())
        self.addPage(ReadyPage())
        
        self.finished.connect(self.on_finished)
    
    def on_finished(self, result):
        if result == QWizard.DialogCode.Accepted:
            clean = self.page(2).clean_cb.isChecked()
            shortcut, data_dir = install_launcher(clean)
            
            msg = QMessageBox(self)
            msg.setWindowTitle("Установка завершена")
            msg.setText(
                f"Amaterasu Launcher успешно установлен!\n\n"
                f"Папка данных: {data_dir}\n"
                f"Ярлык создан: {shortcut}\n\n"
                f"Теперь вы можете запускать лаунчер с рабочего стола."
            )
            msg.exec()
            
            # Автозапуск (опционально)
            # subprocess.Popen([sys.executable, str(LAUNCHER_DIR / "main.py")])

if __name__ == "__main__":
    app = QApplication(sys.argv)
    wizard = InstallerWizard()
    wizard.show()
    sys.exit(app.exec())