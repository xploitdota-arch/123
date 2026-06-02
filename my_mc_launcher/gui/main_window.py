import sys
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QComboBox, QLineEdit, QPushButton, QTextEdit, QLabel, QProgressBar)
from launcher.api import get_version_list
from launcher.java import find_java
from launcher.downloader import download_file
from launcher.api import get_version_info
from launcher.launcher_core import launch_minecraft

class UI: pass  # Хранилище для виджетов

class MainWindow(QMainWindow): 
    def __init__(self):
        super().__init__()
        self.setWindowTitle("My MC Launcher")
        self.resize(650, 480)
        
        self.mc_dir = Path(__file__).parent.parent / ".minecraft"
        self.mc_dir.mkdir(exist_ok=True)
        
        self.ui = UI()  # ✅ Инициализация объекта интерфейса
        self.setup_ui()
        self.load_versions()
        self.java_path = find_java(8) or "java"
        self.ui.java_edit.setText(self.java_path)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        
        layout.addWidget(QLabel("Версия:"))
        self.ui.version_combo = QComboBox()
        layout.addWidget(self.ui.version_combo)
        
        user_layout = QHBoxLayout()
        user_layout.addWidget(QLabel("Никнейм:"))
        self.ui.username_edit = QLineEdit("Steve")
        user_layout.addWidget(self.ui.username_edit)
        layout.addLayout(user_layout)
        
        java_layout = QHBoxLayout()
        java_layout.addWidget(QLabel("Java путь:"))
        self.ui.java_edit = QLineEdit()
        java_layout.addWidget(self.ui.java_edit)
        browse_btn = QPushButton("Обзор")
        java_layout.addWidget(browse_btn)
        layout.addLayout(java_layout)
        
        self.ui.play_btn = QPushButton("▶ Играть", styleSheet="QPushButton { padding: 10px; font-size: 16px; }")
        layout.addWidget(self.ui.play_btn)
        
        self.ui.progress = QProgressBar()
        self.ui.progress.setVisible(False)
        layout.addWidget(self.ui.progress)
        
        layout.addWidget(QLabel("Лог:"))
        self.ui.log_edit = QTextEdit()
        self.ui.log_edit.setReadOnly(True)
        self.ui.log_edit.setMaximumHeight(150)
        layout.addWidget(self.ui.log_edit)
        
        self.ui.play_btn.clicked.connect(self.start_game)

    def load_versions(self):
        versions = get_version_list()
        ids = [v["id"] for v in versions]
        ids.sort(reverse=True)
        self.ui.version_combo.addItems(ids[:20]) 

    def log(self, text: str):
        self.ui.log_edit.append(text)
        self.ui.log_edit.verticalScrollBar().setValue(
            self.ui.log_edit.verticalScrollBar().maximum()
        )

    def start_game(self):
        version = self.ui.version_combo.currentText()
        username = self.ui.username_edit.text().strip()
        java_path = self.ui.java_edit.text().strip() 

        if not version or not username:
            self.log("⚠️ Заполни версию и никнейм")
            return

        self.log(f"📦 Подготовка версии {version}...")
        self.ui.progress.setVisible(True)
        self.ui.play_btn.setEnabled(False) 

        try:
            ver_json = get_version_info(version)
            libs_dir = self.mc_dir / "libraries" 
            
            downloaded_count = 0
            if "libraries" in ver_json:
                for lib in ver_json["libraries"]:
                    if "downloads" not in lib or "artifact" not in lib.get("downloads", {}): continue
                    
                    url = lib["downloads"]["artifact"]["url"]
                    name = lib["name"]
                    lib_ver = lib["version"]
                    
                    jar_path = libs_dir / f"{name.replace('.', '/')}/{lib_ver}/{name}-{lib_ver}.jar"
                    
                    if not jar_path.exists():
                        jar_path.parent.mkdir(parents=True, exist_ok=True)
                        download_file(url, jar_path)
                        downloaded_count += 1

            self.log(f"✅ Скачано {downloaded_count} новых файлов")
            self.log("🚀 Запуск Minecraft...")
            launch_minecraft(version, username, java_path, self.mc_dir)

        except Exception as e:
            self.log(f"❌ Ошибка при подготовке: {e}")
        
        finally:
            self.ui.progress.setVisible(False)
            self.ui.play_btn.setEnabled(True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
