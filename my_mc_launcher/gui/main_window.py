import sys
import os
import json
import shutil
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QCheckBox, QPushButton, QTextEdit, QLabel,
    QLineEdit, QFrame, QGraphicsDropShadowEffect, QProgressBar,
    QStackedWidget, QListWidget, QListWidgetItem, QMessageBox
)
from PyQt6.QtCore import Qt, QPoint, QThread, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QRectF
from PyQt6.QtGui import (
    QFont, QFontDatabase, QPixmap, QPainter, QColor, QCursor, QMovie,
    QPen, QPainterPath, QLinearGradient, QRadialGradient, QRegion, QIcon
)
import random
import math

from launcher.api import get_version_list
from launcher.launcher_core import get_offline_uuid
from launcher.java_installer import download_java, find_java_executable, is_java_installed
from launcher.neoforge_installer import install_neoforge
import minecraft_launcher_lib as mll

SETTINGS_PATH = Path(__file__).parent.parent / "launcher_settings.json"


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return {"java_path": "", "window_width": 1000, "window_height": 600, "ram": "2G", "low_perf": False, "mc_perf": True}


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_installed_versions_list(mc_dir: Path) -> list[str]:
    try:
        all_versions = mll.utils.get_installed_versions(str(mc_dir))
        ids = [v["id"] for v in all_versions]

        # Собираем базовые версии, для которых есть модded-профили (чтобы скрыть ваниль)
        bases_with_modded = set()
        for vid in ids:
            try:
                pjson = Path(mc_dir) / "versions" / vid / f"{vid}.json"
                if pjson.exists():
                    with open(pjson, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "inheritsFrom" in data:
                        bases_with_modded.add(data["inheritsFrom"])
            except Exception:
                pass

        # Фильтруем: не показываем ванильные базы, если для них есть Forge/Fabric/NeoForge
        # Это решает проблему "появляется ванила предыдущей версии" при установке новых Forge
        filtered = [vid for vid in ids if vid not in bases_with_modded]
        return filtered
    except Exception:
        return []


def get_base_minecraft_version(mc_dir: Path, version: str) -> str:
    """Возвращает базовую MC-версию из inheritsFrom (для Forge/Fabric/NeoForge профилей) или сам version."""
    try:
        json_path = Path(mc_dir) / "versions" / version / f"{version}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("inheritsFrom", version)
    except Exception:
        pass
    return version


# Очистка ванили отключена — файлы базовой версии нужны для наследования в Forge/Fabric профилях (inheritsFrom).
# Вместо удаления используем фильтр в get_installed_versions_list, чтобы ваниль не отображалась в списке.


def get_required_java_major(mc_dir: Path, version: str) -> int:
    """Определяет требуемую major версию Java из локального version.json (с учётом inheritsFrom для Forge/Fabric/NeoForge).
    Используется вместо mll.utils.get_version_info (которой нет в актуальных версиях minecraft_launcher_lib).
    Это решает UnsupportedClassVersionError (class file 69.0 требует Java 25+)."""
    try:
        version_dir = Path(mc_dir) / "versions" / version
        json_path = version_dir / f"{version}.json"
        if not json_path.exists():
            return 21
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Если есть inheritsFrom (типично для Forge/Fabric), берём javaVersion из родителя, если нет в дочернем
        if "inheritsFrom" in data:
            inherit_version = data["inheritsFrom"]
            inherit_path = Path(mc_dir) / "versions" / inherit_version / f"{inherit_version}.json"
            if inherit_path.exists():
                with open(inherit_path, "r", encoding="utf-8") as f:
                    parent_data = json.load(f)
                if "javaVersion" not in data or not data.get("javaVersion"):
                    if "javaVersion" in parent_data:
                        data["javaVersion"] = parent_data["javaVersion"]
        if "javaVersion" in data and isinstance(data["javaVersion"], dict):
            major = data["javaVersion"].get("majorVersion")
            if isinstance(major, int):
                return major

        # Fallback: если в локальном json (даже после inherits) нет javaVersion — пробуем через launcher/api (удалённый manifest для базовой MC-версии)
        # Это поможет для новых версий типа 1.26.1.2 / forge-64.0.0 где локальный json может быть неполным на момент первого запуска
        try:
            from launcher.api import get_version_info as get_vi
            # Берём базовую версию: inheritsFrom если есть, иначе сам version
            base_ver = data.get("inheritsFrom") if "inheritsFrom" in data else version
            ver_info = get_vi(base_ver)
            if ver_info and "javaVersion" in ver_info:
                major = ver_info["javaVersion"].get("majorVersion")
                if isinstance(major, int):
                    return major
        except Exception:
            pass

        return 21
    except Exception:
        # fallback для старых профилей или ошибок парсинга
        return 21


# ─── Threads ───

class InstallThread(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, version: str, username: str, mc_dir: Path, settings: dict, parent=None):
        super().__init__(parent)
        self.version = version
        self.username = username
        self.mc_dir = mc_dir
        self.settings = settings
        self._max = 100

    def run(self):
        try:
            # Определяем требуемую версию Java для этой версии Minecraft (из локального json)
            required_java = get_required_java_major(self.mc_dir, self.version)
            self.log_signal.emit(f"☕ Требуется Java {required_java} (профиль: {self.version})")

            # Для ITE/NeoForge версий — пропускаем install (файлы уже есть)
            if not self.version.startswith("ITE-"):
                def set_status(s: str):
                    self.log_signal.emit(f"   ...{s}")
                def set_max(m: int):
                    self._max = max(m, 1)
                    self.progress_signal.emit(0, self._max)
                def set_progress(p: int):
                    self.progress_signal.emit(p, self._max)

                mll.install.install_minecraft_version(
                    self.version, str(self.mc_dir),
                    callback={"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}
                )
                self.log_signal.emit("✅ Файлы готовы")
            else:
                self.log_signal.emit("✅ NeoForge — пропуск установки (файлы готовы)")

            options = {
                "username": self.username,
                "uuid": get_offline_uuid(self.username),
                "token": "0",
                "launcherName": "Amaterasu",
                "launcherVersion": "1.0",
                "gameDirectory": str(self.mc_dir),
            }

            ram = self.settings.get('ram', '2G')
            jvm_args = [f"-Xmx{ram}"]

            # Оптимизационные флаги JVM для Minecraft (меньше CPU/RAM usage, лучше GC)
            if self.settings.get("mc_perf", True):
                jvm_args += [
                    "-XX:+UseG1GC",
                    "-XX:MaxGCPauseMillis=50",
                    "-XX:+UnlockExperimentalVMOptions",
                    "-XX:+UseStringDeduplication",
                    "-XX:+OptimizeStringConcat",
                    "-XX:InitiatingHeapOccupancyPercent=75",
                    "-Dfile.encoding=UTF-8",
                ]

            options["jvmArguments"] = jvm_args
            # Java: настройки → Adoptium (нужная версия) → системная
            java_path = self.settings.get("java_path", "")
            if not java_path:
                java_path = find_java_executable(self.mc_dir, required_java) or ""
                if not java_path:
                    # Автоматически скачиваем нужную версию Java
                    java_path = download_java(self.mc_dir, required_java, log_fn=lambda s: self.log_signal.emit(s)) or ""
            if java_path:
                options["executablePath"] = java_path
            else:
                self.log_signal.emit("⚠️ Не удалось найти/скачать Adoptium Java — fallback на системную 'java' (может не подойти для class file 69+)")
                options["executablePath"] = "java"

            if "executablePath" in options:
                java_exe = options["executablePath"]
                self.log_signal.emit(f"☕ Используемая Java: {java_exe}")
                # Проверить версию Java (чтобы сразу видеть, 21 или 25/26)
                try:
                    ver_out = subprocess.check_output([java_exe, "-version"], stderr=subprocess.STDOUT, text=True, timeout=10)
                    first_line = ver_out.splitlines()[0] if ver_out else "неизвестно"
                    self.log_signal.emit(f"☕ Версия Java: {first_line}")
                except Exception as e:
                    self.log_signal.emit(f"⚠️ Не удалось проверить версию Java: {e}")

            cmd = mll.command.get_minecraft_command(self.version, str(self.mc_dir), options)

            # Фильтруем проблемные/экспериментальные флаги JVM, которые вызывают "Unrecognized VM option" и краши
            # (UseCompactObjectHeaders, --sun-misc-unsafe-memory-access=allow и т.п. из новых профилей 1.21+ / NeoForge)
            # Легко расширять список bad_patterns при появлении новых ошибок
            bad_patterns = ["UseCompactObjectHeaders", "--sun-misc-unsafe-memory-access"]
            filtered_cmd = []
            for arg in cmd:
                if any(pat in arg for pat in bad_patterns):
                    self.log_signal.emit(f"⚠️ Пропущен неподдерживаемый JVM флаг: {arg}")
                    continue
                filtered_cmd.append(arg)
            cmd = filtered_cmd

            self.log_signal.emit(f"☕ Java в команде: {Path(cmd[0]).name}")
            self.log_signal.emit(f"🚀 Полная команда (первые 12 аргументов): {' '.join(cmd[:12])} ...")
            self.log_signal.emit("🚀 Запуск Minecraft...")
            subprocess.Popen(cmd, cwd=str(self.mc_dir))
            self.finished_signal.emit()
        except Exception as e:
            self.error_signal.emit(str(e))


class DownloadThread(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, version: str, mc_dir: Path, parent=None):
        super().__init__(parent)
        self.version = version
        self.mc_dir = mc_dir
        self._max = 100

    def run(self):
        try:
            def set_status(s: str):
                self.log_signal.emit(f"   ...{s}")
            def set_max(m: int):
                self._max = max(m, 1)
                self.progress_signal.emit(0, self._max)
            def set_progress(p: int):
                self.progress_signal.emit(p, self._max)

            mll.install.install_minecraft_version(
                self.version, str(self.mc_dir),
                callback={"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}
            )
            self.finished_signal.emit()
        except Exception as e:
            self.error_signal.emit(str(e))


# ─── Mod Install Thread ───

class IBEInstallThread(QThread):
    """Downloads and installs IBE Editor from GitHub releases."""
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    RELEASE_URL = "https://api.github.com/repos/Wynasik/mods/releases/tags/0.2.0"
    MC_VERSION = "1.21.4"
    ITE_VERSION = "ITE-21.4.157"

    def __init__(self, mc_dir: Path, parent=None):
        super().__init__(parent)
        self.mc_dir = mc_dir

    def _download(self, url: str, dest: Path):
        import urllib.request
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
        with urllib.request.urlopen(req, timeout=300) as resp:
            dest.write_bytes(resp.read())

    def run(self):
        import urllib.request
        import zipfile
        import tempfile

        try:
            # 1. Get release info
            self.log_signal.emit("📡 Получение информации о релизе...")
            self.progress_signal.emit(0, 6)

            req = urllib.request.Request(self.RELEASE_URL)
            req.add_header("User-Agent", "Amaterasu-Launcher/1.0")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}

            full_pack_name = None
            mod_name = None
            for name in assets:
                if "full-pack" in name:
                    full_pack_name = name
                elif "ibeeditor" in name:
                    mod_name = name

            if not mod_name:
                self.error_signal.emit("Не найдены файлы в релизе")
                return

            # 2. Download Java 21
            self.progress_signal.emit(1, 6)
            download_java(self.mc_dir, 21, log_fn=lambda s: self.log_signal.emit(s))

            # 3. Install vanilla MC 1.21.4
            self.progress_signal.emit(2, 6)
            self.log_signal.emit(f"📦 Установка Minecraft {self.MC_VERSION}...")

            self._ibe_max = 100
            def ibe_status(s): self.log_signal.emit(f"   ...{s}")
            def ibe_max(m): self._ibe_max = max(m, 1)
            def ibe_progress(p):
                if self._ibe_max > 0 and (p % 50 == 0 or p == self._ibe_max):
                    self.log_signal.emit(f"   📥 {p}/{self._ibe_max}")
            mll.install.install_minecraft_version(
                self.MC_VERSION, str(self.mc_dir),
                callback={"setStatus": ibe_status, "setMax": ibe_max, "setProgress": ibe_progress}
            )
            self.log_signal.emit(f"✅ Minecraft {self.MC_VERSION} установлен")

            # 4. Установка NeoForge с помощью локального установщика
            self.progress_signal.emit(3, 6)
            self.log_signal.emit("📦 Установка NeoForge из локального файла...")
            
            installer_jar = Path(__file__).parent.parent / "neoforge-21.4.157-installer-fat.jar"
            if not installer_jar.exists():
                self.error_signal.emit(f"Installer jar не найден: {installer_jar}")
                return
                
            # Для корректной работы инсталлера нужен launcher_profiles.json
            profiles_json = self.mc_dir / "launcher_profiles.json"
            if not profiles_json.exists():
                profiles_json.write_text("{}", encoding="utf-8")
                
            java_path = find_java_executable(self.mc_dir, 21) or "java"
            
            self.progress_signal.emit(4, 6)
            self.log_signal.emit("⚙ Работает установщик NeoForge...")
            try:
                subprocess.run(
                    [java_path, "-jar", str(installer_jar), "--installClient", str(self.mc_dir)],
                    check=True, capture_output=True, text=True
                )
            except subprocess.CalledProcessError as e:
                self.error_signal.emit(f"Ошибка установки NeoForge: {e.stderr}")
                return
                
            self.progress_signal.emit(5, 6)
            self.log_signal.emit("📝 Настройка профиля ITE...")
            
            versions_dir = self.mc_dir / "versions"
            nf_dir = versions_dir / "neoforge-21.4.157"
            ite_dir = versions_dir / self.ITE_VERSION
            
            # Переименовываем профиль neoforge в ITE
            if nf_dir.exists():
                if ite_dir.exists():
                    import shutil
                    shutil.rmtree(ite_dir, ignore_errors=True)
                nf_dir.rename(ite_dir)
                
            nf_json = ite_dir / "neoforge-21.4.157.json"
            ite_json = ite_dir / f"{self.ITE_VERSION}.json"
            
            if nf_json.exists():
                nf_json.rename(ite_json)
                
            if ite_json.exists():
                data = json.loads(ite_json.read_text(encoding="utf-8"))
                data["id"] = self.ITE_VERSION
                ite_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
                
            # Copy vanilla jar as ITE jar
            vanilla_jar = versions_dir / self.MC_VERSION / f"{self.MC_VERSION}.jar"
            ite_jar = ite_dir / f"{self.ITE_VERSION}.jar"
            if vanilla_jar.exists() and not ite_jar.exists():
                import shutil
                shutil.copy2(str(vanilla_jar), str(ite_jar))
                self.log_signal.emit(f"✅ Скопирован JAR для {self.ITE_VERSION}")

            self.log_signal.emit("✅ NeoForge успешно установлен!")

            # 6. Download mod
            self.progress_signal.emit(6, 6)
            mods_dir = self.mc_dir / "mods"
            mods_dir.mkdir(exist_ok=True)
            mod_path = mods_dir / mod_name

            if not mod_path.exists():
                self.log_signal.emit(f"⬇ Скачивание {mod_name}...")
                self._download(assets[mod_name], mod_path)
                self.log_signal.emit(f"✅ Мод: {mod_name}")
            else:
                self.log_signal.emit(f"✅ Мод уже есть: {mod_name}")

            
            self.finished_signal.emit()

        except Exception as e:
            self.error_signal.emit(str(e))




class VersionListThread(QThread):
    """Загружает список версий в фоне."""
    finished = pyqtSignal(list)  # list of version dicts
    error = pyqtSignal(str)

    def run(self):
        try:
            versions = get_version_list()
            self.finished.emit(versions)
        except Exception as e:
            self.error.emit(str(e))


class ModInstallThread(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal(str)  # installed version id
    error_signal = pyqtSignal(str)

    def __init__(self, mod_type: str, mc_version: str, mc_dir: Path, parent=None):
        super().__init__(parent)
        self.mod_type = mod_type  # "forge" or "fabric"
        self.mc_version = mc_version
        self.mc_dir = mc_dir
        self._max = 100

    def run(self):
        try:
            def set_status(s: str):
                self.log_signal.emit(f"   ...{s}")
            def set_max(m: int):
                self._max = max(m, 1)
                self.progress_signal.emit(0, self._max)
            def set_progress(p: int):
                self.progress_signal.emit(p, self._max)

            callback = {"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}

            # Определяем требуемую Java для mc_version (из удалённого json vanilla, до установки)
            # Это нужно, чтобы процессоры Forge/Fabric (в mll.install) запускались с правильной Java
            # и не было проблем с class file version 69.0 при установке/запуске
            required_java = 21
            try:
                from launcher.api import get_version_info as get_vi
                ver_info = get_vi(self.mc_version)
                if ver_info and "javaVersion" in ver_info:
                    required_java = ver_info.get("javaVersion", {}).get("majorVersion", 21)
            except Exception:
                pass

            # Получаем/скачиваем Adoptium Java нужной версии для процессоров установки
            java_path = find_java_executable(self.mc_dir, required_java) or ""
            if not java_path:
                java_path = download_java(self.mc_dir, required_java, log_fn=lambda s: self.log_signal.emit(s)) or ""
            if not java_path:
                java_path = "java"
            self.log_signal.emit(f"☕ Для установки {self.mod_type} используется Java {required_java}")

            if self.mod_type == "forge":
                self.log_signal.emit(f"🔨 Установка Forge для {self.mc_version}...")
                forge_ver = mll.forge.find_forge_version(self.mc_version)
                if not forge_ver:
                    self.error_signal.emit(f"Forge не найден для {self.mc_version}")
                    return
                self.log_signal.emit(f"📦 Forge: {forge_ver}")
                mll.forge.install_forge_version(forge_ver, str(self.mc_dir), callback=callback, java=java_path)
                installed_id = forge_ver
            else:
                self.log_signal.emit(f"🧵 Установка Fabric для {self.mc_version}...")
                if not mll.fabric.is_minecraft_version_supported(self.mc_version):
                    self.error_signal.emit(f"Fabric не поддерживает {self.mc_version}")
                    return
                mll.fabric.install_fabric(self.mc_version, str(self.mc_dir), callback=callback, java=java_path)
                loader = mll.fabric.get_latest_loader_version()
                installed_id = f"fabric-loader-{loader}-{self.mc_version}"

            self.log_signal.emit(f"✅ {self.mod_type.capitalize()} установлен!")

            # Ванильные файлы оставляем (нужны для inheritsFrom в Forge/Fabric).
            # Список скрывает ваниль благодаря фильтру в get_installed_versions_list.

            self.finished_signal.emit(installed_id)
        except Exception as e:
            self.error_signal.emit(str(e))


# ─── Rounded Popup Menu ───

class RoundedMenu(QFrame):
    page_selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("""
            RoundedMenu {
                background-color: #1a0a20;
                border: 1px solid #4a2060;
                border-radius: 10px;
            }
            QPushButton {
                background: transparent;
                border: none;
                color: #c0a0d0;
                padding: 10px 20px;
                font-size: 13px;
                text-align: left;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #2a1040;
                color: #e0c0ff;
            }
        """)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(2)

        for idx, label in [(1, "⚡ Менеджер версий"), (3, "🧩 Моды"), (2, "⚙ Настройки лаунчера")]:
            btn = QPushButton(label)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, i=idx: self.page_selected.emit(i))
            v.addWidget(btn)

        v.addSpacing(4)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #3a1555;")
        v.addWidget(sep)
        lbl = QLabel("Amaterasu v1.0")
        lbl.setStyleSheet("color:#5a3070;font-size:10px;padding:2px 4px;")
        v.addWidget(lbl)
        self.adjustSize()


# ─── Vector Icon Button ───

class IconBtn(QPushButton):
    def __init__(self, icon_type: str, tooltip: str, parent=None):
        super().__init__(parent)
        self.icon_type = icon_type
        self.setFixedSize(34, 34)
        self.setToolTip(tooltip)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet("background: transparent; border: none;")
        self._hover = False

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)
    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(180, 100, 255) if self._hover else QColor(120, 80, 160)
        painter.setPen(QPen(c, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        r = self.rect().adjusted(5, 5, -5, -5)
        cx, cy = r.center().x(), r.center().y()

        if self.icon_type == "info":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(r)
            painter.setPen(QPen(c, 2))
            painter.drawPoint(cx, cy - 3)
            painter.drawLine(cx, cy, cx, cy + 5)
        elif self.icon_type == "folder":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(r.x(), r.y() + 5, r.width(), r.height() - 5)
            p = QPainterPath()
            p.moveTo(r.x(), r.y() + 5)
            p.lineTo(r.x() + r.width() * 0.38, r.y() + 5)
            p.lineTo(r.x() + r.width() * 0.48, r.y())
            p.lineTo(r.x() + r.width(), r.y())
            p.lineTo(r.x() + r.width(), r.y() + 5)
            painter.drawPath(p)
        elif self.icon_type == "refresh":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(r.x(), r.y(), r.width(), r.height(), 30 * 16, 280 * 16)
            ax = int(r.x() + r.width() * 0.78)
            ay = int(r.y() + r.height() * 0.22)
            painter.drawLine(ax, ay, ax + 4, ay - 3)
            painter.drawLine(ax, ay, ax + 1, ay + 4)
        elif self.icon_type == "menu":
            painter.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(3):
                y = int(r.y() + 3 + i * r.height() * 0.34)
                painter.drawLine(r.x(), y, r.x() + r.width(), y)
        painter.end()


# ─── Background ───

class GifBg(QWidget):
    def __init__(self, image_path: Path, parent=None):
        super().__init__(parent)
        self.pixmap = None
        self.movie_label = None
        for ext in (".gif", ".png", ".jpg", ".jpeg"):
            p = image_path.with_suffix(ext)
            if p.exists():
                if ext == ".gif":
                    self.movie_label = QLabel(self)
                    movie = QMovie(str(p))
                    self.movie_label.setMovie(movie)
                    self.movie_label.setScaledContents(True)
                    movie.start()
                else:
                    self.pixmap = QPixmap(str(p))
                break

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.movie_label:
            self.movie_label.setGeometry(self.rect())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(8, 4, 14, 255))
        if self.pixmap and not self.pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scaled = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width() - scaled.width()) // 3,
                               (self.height() - scaled.height()) // 2, scaled)
        painter.end()


# ─── Particle for launch effect ───

class Particle:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(1.5, 5.0)
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed - random.uniform(1, 3)  # bias upward
        self.life = random.uniform(0.6, 1.0)
        self.decay = random.uniform(0.015, 0.035)
        self.size = random.uniform(2, 5)
        # Purple-ish colors
        self.hue = random.randint(250, 290)
        self.brightness = random.randint(160, 255)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += 0.08  # gravity
        self.vx *= 0.98
        self.life -= self.decay
        self.size *= 0.97

    def is_alive(self):
        return self.life > 0 and self.size > 0.5


class ParticleOverlay(QWidget):
    """Transparent overlay that draws particles over the launch button area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.particles: list[Particle] = []
        self._active = False

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps для снижения нагрузки на ПК
        self._timer.timeout.connect(self._tick)

    def start(self, source_rect):
        """Start emitting particles from source_rect (in parent coords)."""
        self._source = source_rect
        self._active = True
        self._spawn_counter = 0
        self.particles.clear()
        self.raise_()
        self.show()
        self._timer.start()

    def stop(self):
        self._active = False
        # Let existing particles die out

    def _tick(self):
        # Spawn new particles if active (уменьшено для оптимизации нагрузки)
        if self._active:
            self._spawn_counter += 1
            for _ in range(1):  # 1 particle per frame вместо 3
                x = self._source.x() + random.randint(0, self._source.width())
                y = self._source.y() + random.randint(-5, 5)
                # Emit from edges and top
                if random.random() < 0.5:
                    y = self._source.y() - random.randint(0, 10)
                else:
                    x = self._source.x() + random.choice([0, self._source.width()])
                    y = self._source.y() + random.randint(0, self._source.height())
                self.particles.append(Particle(x, y))

        # Update particles (ограничение для оптимизации)
        self.particles = [p for p in self.particles if p.is_alive()]
        if len(self.particles) > 30:
            self.particles = self.particles[-30:]
        for p in self.particles:
            p.update()

        # Stop timer when no particles left
        if not self._active and not self.particles:
            self._timer.stop()
            self.hide()

        self.update()

    def paintEvent(self, event):
        if not self.particles:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for p in self.particles:
            alpha = max(0, min(255, int(p.life * 255)))
            hue = max(0, min(359, int(p.hue) % 360))
            val = max(0, min(255, int(p.brightness)))
            # Core color
            color = QColor.fromHsv(hue, 180, val, alpha)

            # Glow
            grad = QRadialGradient(p.x, p.y, p.size * 3)
            grad.setColorAt(0, QColor(val, 100, 255, alpha))
            grad.setColorAt(0.4, color)
            grad.setColorAt(1, QColor(hue % 256, 0, val // 2, 0))

            painter.setBrush(grad)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(p.x - p.size, p.y - p.size, p.size * 2, p.size * 2))

        painter.end()


# ─── Animated Launch Button ───

class AnimatedLaunchBtn(QPushButton):
    """Launch button with pulsing glow and spinner animation while loading."""

    SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._base_text = text
        self._loading = False
        self._spinner_idx = 0
        self._glow_phase = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(100)  # медленнее для снижения нагрузки
        self._pulse_timer.timeout.connect(self._pulse_tick)

        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(150)
        self._spinner_timer.timeout.connect(self._spinner_tick)

        self._glow_effect = None

    def set_glow(self, effect: QGraphicsDropShadowEffect):
        self._glow_effect = effect

    def start_loading(self, text="ЗАГРУЗКА..."):
        self._loading = True
        self._base_text = text
        self._spinner_idx = 0
        self._glow_phase = 0.0
        self._pulse_timer.start()
        self._spinner_timer.start()

    def stop_loading(self, text="▶  ЗАПУСТИТЬ"):
        self._loading = False
        self._pulse_timer.stop()
        self._spinner_timer.stop()
        self.setText(text)
        if self._glow_effect:
            self._glow_effect.setColor(QColor(120, 40, 200, 180))
            self._glow_effect.setBlurRadius(28)

    def _pulse_tick(self):
        self._glow_phase += 0.12
        if self._glow_effect:
            # Pulsating glow: blur radius oscillates 10..25 (снижено для оптимизации)
            t = (math.sin(self._glow_phase) + 1) / 2  # 0..1
            blur = 10 + t * 15
            alpha = int(80 + t * 80)
            # Shift hue slightly for shimmer
            hue_shift = int(math.sin(self._glow_phase * 0.7) * 20)
            r = max(0, min(255, 120 + hue_shift))
            g = max(0, min(255, 40))
            b = max(0, min(255, 220 - hue_shift))
            self._glow_effect.setColor(QColor(r, g, b, alpha))
            self._glow_effect.setBlurRadius(blur)

    def _spinner_tick(self):
        if self._loading:
            char = self.SPINNER_CHARS[self._spinner_idx % len(self.SPINNER_CHARS)]
            self.setText(f"{char}  {self._base_text}")
            self._spinner_idx += 1


# ─── Separator Line ───

class GlowSeparator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(2)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(0.3, QColor(140, 60, 220, 200))
        grad.setColorAt(0.5, QColor(180, 100, 255, 255))
        grad.setColorAt(0.7, QColor(140, 60, 220, 200))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), grad)
        painter.end()


# ─── Stylesheet ───

def stylesheet(mc_font: str = "Segoe UI", title_font: str = "Segoe UI") -> str:
    return f"""
    QWidget{{color:#e0d0f0;font-family:"Segoe UI";font-size:13px;}}
    #LeftPanel{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 rgba(18,8,30,250),
            stop:0.5 rgba(12,5,22,252),
            stop:1 rgba(18,8,30,250));
        border:none;
        border-right:1px solid rgba(120,50,180,40);
    }}
    #RightPanel{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 rgba(18,8,30,250),
            stop:1 rgba(12,5,22,252));
        border:none;
        border-left:1px solid rgba(120,50,180,40);
    }}
    #CenterBg{{
        background-color:#08040e;
    }}
    #TopBar{{
        background-color:rgba(12,5,20,252);
        border-bottom:1px solid rgba(140,60,220,60);
    }}

    QLineEdit{{
        background-color:rgba(25,12,40,200);
        border:1px solid rgba(120,50,180,100);
        border-radius:8px;
        padding:8px 12px;
        color:#e0d0f0;
        font-size:13px;
        selection-background-color:#5a2090;
    }}
    QLineEdit:focus{{border:1px solid #9050e0;}}
    QLineEdit::placeholder{{color:rgba(160,130,200,120);}}

    QComboBox{{
        background-color:rgba(25,12,40,200);
        border:1px solid rgba(120,50,180,100);
        border-radius:8px;
        padding:8px 12px;
        padding-right:28px;
        color:#e0d0f0;
        font-size:13px;
    }}
    QComboBox::drop-down{{background:transparent;border:none;width:24px;}}
    QComboBox::down-arrow{{
        image:none;
        border-left:5px solid transparent;
        border-right:5px solid transparent;
        border-top:7px solid #8040c0;
        width:0;height:0;margin-top:2px;
    }}
    QComboBox QAbstractItemView{{
        background:#140822;
        border:1px solid #4a2060;
        selection-background-color:#3a1555;
        color:#e0d0f0;
        padding:4px;
        border-radius:6px;
    }}

    QCheckBox{{spacing:8px;color:#a090c0;font-size:12px;}}
    QCheckBox::indicator{{
        width:18px;height:18px;border-radius:4px;
        border:1px solid #6030a0;
        background:rgba(20,10,35,200);
    }}
    QCheckBox::indicator:hover{{border:1px solid #9050e0;}}
    QCheckBox::indicator:checked{{
        background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #7030b0,stop:1 #a050e0);
        border:1px solid #b070ff;
    }}

    QProgressBar{{
        background-color:rgba(20,10,35,200);
        border:1px solid rgba(120,50,180,80);
        border-radius:6px;
        color:#e0d0f0;
        text-align:center;
        font-size:11px;
        max-height:20px;
    }}
    QProgressBar::chunk{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #4a1580,stop:0.5 #8040d0,stop:1 #b060ff);
        border-radius:5px;
    }}

    #LaunchBtn{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #6020a0,stop:0.5 #4a1580,stop:1 #30105a);
        border:1px solid #7030b0;
        border-radius:10px;
        padding:14px;
        font-family:"{mc_font}";
        font-size:16px;
        font-weight:bold;
        color:#fff;
        margin:6px 0;
    }}
    #LaunchBtn:hover{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #7830c0,stop:0.5 #5a20a0,stop:1 #3a1570);
        border:1px solid #a050e0;
    }}
    #LaunchBtn:pressed{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #2a0845,stop:1 #4a1580);
        padding-top:15px;padding-bottom:13px;
    }}
    #LaunchBtn:disabled{{
        background-color:rgba(30,15,50,200);
        color:#6050a0;
        border:1px solid #3a2060;
    }}

    QTextEdit#Log{{
        background-color:rgba(12,6,22,220);
        border:1px solid rgba(100,40,160,50);
        border-radius:8px;
        color:#c0a0e0;
        font-family:Consolas,"Courier New",monospace;
        font-size:11px;
        padding:8px;
        selection-background-color:#4a2070;
    }}

    #WinBtn{{
        background:transparent;border:none;
        color:#8070a0;font-size:16px;
        padding:2px 10px;border-radius:6px;
    }}
    #WinBtn:hover{{background-color:rgba(120,50,180,40);color:#e0d0f0;}}

    #CloseBtn{{
        background:transparent;border:none;
        color:#8070a0;font-size:16px;
        padding:2px 10px;border-radius:6px;
    }}
    #CloseBtn:hover{{background-color:#8020c0;color:#fff;}}

    QListWidget{{
        background-color:rgba(18,8,30,220);
        border:1px solid rgba(120,50,180,60);
        border-radius:8px;
        color:#e0d0f0;
        outline:none;
    }}
    QListWidget::item{{
        padding:8px 12px;
        border-bottom:1px solid rgba(120,50,180,20);
    }}
    QListWidget::item:selected{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #3a1560,stop:1 #2a1040);
        color:#fff;
        border-radius:6px;
    }}
    QListWidget::item:hover{{background-color:rgba(80,30,130,40);}}

    #ActBtn{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #3a1560,stop:1 #200a38);
        border:1px solid #5a2080;
        border-radius:8px;
        padding:9px 18px;
        color:#d0c0e0;
        font-family:"{mc_font}";
        font-size:13px;
    }}
    #ActBtn:checked{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #6020a0,stop:1 #4a1580);
        border:1px solid #9050e0;
        color:#fff;
    }}
    #ActBtn:hover{{
        background-color:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 #5a2090,stop:1 #3a1060);
        border:1px solid #8040c0;
        color:#fff;
    }}

    #PageTitle{{
        font-family:"{mc_font}";
        font-size:20px;
        font-weight:bold;
        color:#b070ff;
        padding-bottom:8px;
    }}
    #McLabel{{
        font-family:"{mc_font}";
        font-size:12px;
        color:#9080b0;
    }}
    #TitleBarLabel{{
        font-family:"{title_font}";
        font-size:22px;
        font-weight:normal;
        color:#ffffff;
        letter-spacing:4px;
    }}
    #VersionLabel{{
        color:#8070a0;
        font-size:11px;
    }}
    #ConsoleLabel{{
        color:#7060a0;
        font-size:11px;
        font-family:"{mc_font}";
    }}

    QScrollBar:vertical{{
        background:rgba(15,8,25,150);
        width:8px;
        border-radius:4px;
        margin:2px;
    }}
    QScrollBar::handle:vertical{{
        background:rgba(120,60,180,120);
        border-radius:4px;
        min-height:30px;
    }}
    QScrollBar::handle:vertical:hover{{background:rgba(150,80,220,180);}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:none;}}
    """


# ─── Main Window ───

class MainWindow(QMainWindow):
    BASE_WIDTH = 960
    EXPANDED_WIDTH = 1340

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Amaterasu")  # Чтобы в таскбаре и hover показывалось Amaterasu
        self.resize(self.BASE_WIDTH, 580)
        self.setMinimumSize(820, 480)
        self._drag_pos = None

        # ─── Fade in setup ───
        self.setWindowOpacity(0.0)

        # Используем .amaterasu вместо .minecraft, чтобы не конфликтовать с оригинальным Minecraft
        appdata = os.getenv("APPDATA")
        if appdata:
            self.mc_dir = Path(appdata) / ".amaterasu"
        else:
            self.mc_dir = Path(__file__).parent.parent / ".amaterasu"
        self.mc_dir.mkdir(parents=True, exist_ok=True)

        # ─── Иконка на панели задач ───
        icon_path = Path(__file__).parent.parent / "assets" / "icon_amaterasu.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # ─── Загрузка Minecraft шрифта ───
        font_path = Path(__file__).parent.parent / "fonts" / "minecraft-rus.ttf"
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id != -1:
            families = QFontDatabase.applicationFontFamilies(font_id)
            self.mc_font = families[0] if families else "Segoe UI"
        else:
            self.mc_font = "Segoe UI"

        # Подавляем предупреждение о размере шрифта <=0 (часто бывает с кастомными .ttf)
        try:
            from PyQt6.QtGui import QFont
            QFontDatabase.addApplicationFont(str(font_path))
        except Exception:
            pass

        # ─── Загрузка шрифта для заголовка (Yuji Syuku) ───
        title_font_path = Path(__file__).parent.parent / "fonts" / "yuji-syuku.ttf"
        title_font_id = QFontDatabase.addApplicationFont(str(title_font_path))
        if title_font_id != -1:
            title_families = QFontDatabase.applicationFontFamilies(title_font_id)
            self.title_font = title_families[0] if title_families else "Segoe UI"
        else:
            self.title_font = "Segoe UI"

        central = QWidget()
        self.setCentralWidget(central)

        main_h = QHBoxLayout(central)
        main_h.setContentsMargins(0, 44, 0, 0)
        main_h.setSpacing(0)

        # LEFT PANEL
        left = QFrame()
        left.setObjectName("LeftPanel")
        left.setFixedWidth(300)
        v = QVBoxLayout(left)
        v.setContentsMargins(20, 12, 20, 16)
        v.setSpacing(8)

        # ─── User section ───
        nick_lbl = QLabel("Ник:")
        nick_lbl.setObjectName("McLabel")
        v.addWidget(nick_lbl)
        self.nick_edit = QLineEdit()
        self.nick_edit.setText("Steve")
        self.nick_edit.setMaxLength(16)
        self.nick_edit.setPlaceholderText("Введи никнейм...")
        v.addWidget(self.nick_edit)

        v.addSpacing(4)

        ver_lbl = QLabel("Версия:")
        ver_lbl.setObjectName("McLabel")
        v.addWidget(ver_lbl)
        self.version_combo = QComboBox()
        self.version_combo.setEditable(False)
        v.addWidget(self.version_combo)

        v.addSpacing(4)

        self.chk_def = QCheckBox("Отложенный запуск")
        v.addWidget(self.chk_def)
        self.chk_upd = QCheckBox("Обновить клиент")
        self.chk_upd.setChecked(True)
        v.addWidget(self.chk_upd)

        v.addSpacing(6)

        self.play_progress = QProgressBar()
        self.play_progress.setMaximumHeight(20)
        self.play_progress.setTextVisible(True)
        self.play_progress.setVisible(False)
        v.addWidget(self.play_progress)

        self.play_btn = AnimatedLaunchBtn("▶  ЗАПУСТИТЬ")
        self.play_btn.setObjectName("LaunchBtn")
        self.play_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.play_btn.clicked.connect(self._on_play)

        self._btn_glow = QGraphicsDropShadowEffect(self.play_btn)
        self._btn_glow.setColor(QColor(120, 40, 200, 180))
        if self.settings.get("low_perf", False):
            self._btn_glow.setBlurRadius(12)  # меньше для низкой нагрузки
        else:
            self._btn_glow.setBlurRadius(28)
        self._btn_glow.setOffset(0, 0)
        self.play_btn.setGraphicsEffect(self._btn_glow)
        self.play_btn.set_glow(self._btn_glow)
        v.addWidget(self.play_btn)

        v.addSpacing(4)

        # ─── Separator ───
        v.addWidget(GlowSeparator())

        v.addSpacing(2)

        console_lbl = QLabel("Консоль:")
        console_lbl.setObjectName("ConsoleLabel")
        v.addWidget(console_lbl)
        self.log_edit = QTextEdit()
        self.log_edit.setObjectName("Log")
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(100)
        v.addWidget(self.log_edit)

        # ─── Bottom icons ───
        icons_h = QHBoxLayout()
        icons_h.setSpacing(8)
        icons_h.addStretch(1)

        self.btn_info = IconBtn("info", "Информация")
        self.btn_info.clicked.connect(lambda: self.log("Amaterasu Launcher v1.0"))

        self.btn_folder = IconBtn("folder", "Открыть .amaterasu")
        self.btn_folder.clicked.connect(self._open_folder)

        self.btn_refresh = IconBtn("refresh", "Обновить версии")
        self.btn_refresh.clicked.connect(self.refresh_main_versions)

        self.btn_menu = IconBtn("menu", "Меню")
        self.btn_menu.clicked.connect(self._show_menu)

        for b in (self.btn_info, self.btn_folder, self.btn_refresh, self.btn_menu):
            icons_h.addWidget(b)
        icons_h.addStretch(1)
        v.addLayout(icons_h)
        v.addStretch(0)

        main_h.addWidget(left)

        # CENTER (GIF)
        bg_path = Path(__file__).parent.parent / "assets" / "bg_amaterasu"
        self.center_gif = GifBg(bg_path)
        self.center_gif.setObjectName("CenterBg")
        main_h.addWidget(self.center_gif, 1)

        # RIGHT PANEL (initially hidden)
        self.right_panel = QFrame()
        self.right_panel.setObjectName("RightPanel")
        self.right_panel.setFixedWidth(360)
        self.right_panel.setVisible(False)

        rv = QVBoxLayout(self.right_panel)
        rv.setContentsMargins(16, 16, 16, 16)
        rv.setSpacing(10)

        self.right_stack = QStackedWidget()
        rv.addWidget(self.right_stack, 1)

        # Right pages
        self._build_right_pages()

        main_h.addWidget(self.right_panel, 0)

        # MENU POPUP
        self.popup = RoundedMenu(self)
        self.popup.page_selected.connect(self._switch_page)

        # TOP BAR
        self._build_title_bar()

        # ─── Particle overlay ───
        self._particles = ParticleOverlay(central)
        self._particles.setGeometry(central.rect())
        self._particles.hide()

        self.setStyleSheet(stylesheet(self.mc_font, self.title_font))
        self.refresh_main_versions()

        # ─── Если нет установленных версий — сразу открыть менеджер ───
        if self.version_combo.count() == 0 or self.version_combo.itemText(0) == "Нет установленных":
            QTimer.singleShot(50, lambda: self._switch_page(1))

    def _build_right_pages(self):
        # 0 placeholder
        self.right_stack.addWidget(QWidget())

        # 1: Versions
        ver_page = QWidget()
        vv = QVBoxLayout(ver_page)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.setSpacing(10)

        title = QLabel("⚡ Менеджер версий")
        title.setObjectName("PageTitle")
        vv.addWidget(title)

        self.ver_search = QLineEdit()
        self.ver_search.setPlaceholderText("🔍 Поиск версии...")
        self.ver_search.textChanged.connect(self._filter_versions)
        vv.addWidget(self.ver_search)

        self.all_versions_list = QListWidget()
        vv.addWidget(self.all_versions_list, 1)

        btn_h = QHBoxLayout()
        btn_h.setSpacing(8)
        btn_back_v = QPushButton("← Назад")
        btn_back_v.setObjectName("ActBtn")
        btn_back_v.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_back_v.clicked.connect(lambda: self._switch_page(0))
        btn_h.addWidget(btn_back_v)
        btn_h.addStretch(1)

        self.btn_download = QPushButton("⬇ Скачать")
        self.btn_download.setObjectName("ActBtn")
        self.btn_download.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_download.clicked.connect(self._on_download_version)
        btn_h.addWidget(self.btn_download)
        vv.addLayout(btn_h)

        self.ver_progress = QProgressBar()
        self.ver_progress.setMaximumHeight(20)
        self.ver_progress.setTextVisible(True)
        self.ver_progress.setVisible(False)
        vv.addWidget(self.ver_progress)

        self.ver_status = QLabel("")
        self.ver_status.setObjectName("VersionLabel")
        vv.addWidget(self.ver_status)
        self.right_stack.addWidget(ver_page)

        # 2: Settings
        set_page = QWidget()
        sv = QVBoxLayout(set_page)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(12)

        title2 = QLabel("⚙ Настройки")
        title2.setObjectName("PageTitle")
        sv.addWidget(title2)

        sv.addWidget(QLabel("Путь к Java (пусто = авто):", styleSheet="color:#9080b0;font-size:12px;"))
        self.set_java = QLineEdit()
        self.set_java.setText(self.settings.get("java_path", ""))
        self.set_java.setPlaceholderText("Автоопределение...")
        sv.addWidget(self.set_java)

        sv.addWidget(QLabel("RAM:", styleSheet="color:#9080b0;font-size:12px;"))
        self.set_ram = QComboBox()
        self.set_ram.addItems(["1G","2G","3G","4G","6G","8G","12G","16G"])
        self.set_ram.setCurrentText(self.settings.get("ram", "2G"))
        sv.addWidget(self.set_ram)

        self.set_low_perf = QCheckBox("Низкая нагрузка на ПК")
        self.set_low_perf.setToolTip("Отключить частицы, замедлить анимации и эффекты в лаунчере для снижения нагрузки на ПК.")
        self.set_low_perf.setChecked(self.settings.get("low_perf", False))
        sv.addWidget(self.set_low_perf)

        self.set_mc_perf = QCheckBox("Оптимизация Minecraft")
        self.set_mc_perf.setToolTip("Добавить оптимизационные JVM-флаги для Minecraft (лучший GC, меньше CPU/RAM, меньше лагов). Рекомендуется.")
        self.set_mc_perf.setChecked(self.settings.get("mc_perf", True))
        sv.addWidget(self.set_mc_perf)

        dim = QHBoxLayout()
        dim.addWidget(QLabel("Ширина окна MC:", styleSheet="color:#9080b0;font-size:12px;"))
        self.set_w = QLineEdit()
        self.set_w.setText(str(self.settings.get("window_width", 1000)))
        self.set_w.setFixedWidth(80)
        dim.addWidget(self.set_w)
        dim.addWidget(QLabel("Высота:", styleSheet="color:#9080b0;font-size:12px;"))
        self.set_h = QLineEdit()
        self.set_h.setText(str(self.settings.get("window_height", 600)))
        self.set_h.setFixedWidth(80)
        dim.addStretch(1)
        sv.addLayout(dim)

        btn_h = QHBoxLayout()
        btn_h.setSpacing(8)
        btn_back_s = QPushButton("← Назад")
        btn_back_s.setObjectName("ActBtn")
        btn_back_s.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_back_s.clicked.connect(lambda: self._switch_page(0))
        btn_save = QPushButton("💾 Сохранить")
        btn_save.setObjectName("ActBtn")
        btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_save.clicked.connect(self._save_settings)
        btn_h.addWidget(btn_back_s)
        btn_h.addWidget(btn_save)
        btn_h.addStretch(1)
        sv.addLayout(btn_h)
        sv.addStretch(1)
        self.right_stack.addWidget(set_page)

        self._all_versions_raw = []
        self._all_display_items = []
        # Загрузка версий в фоне (не блокирует UI)
        self._start_version_load()

        # 3: Mods page
        mod_page = QWidget()
        mv = QVBoxLayout(mod_page)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(12)

        title3 = QLabel("🧩 Моды")
        title3.setObjectName("PageTitle")
        mv.addWidget(title3)

        # ─── IBE Editor card ───
        ibe_card = QFrame()
        ibe_card.setStyleSheet("""
            QFrame {
                background-color: rgba(25, 12, 40, 200);
                border: 1px solid rgba(120, 50, 180, 80);
                border-radius: 10px;
                padding: 14px;
            }
        """)
        ibe_v = QVBoxLayout(ibe_card)
        ibe_v.setSpacing(8)

        ibe_title = QLabel("🏗 IBE Editor")
        ibe_title.setStyleSheet("font-size:16px;font-weight:bold;color:#d0b0ff;border:none;background:transparent;")
        ibe_v.addWidget(ibe_title)

        ibe_desc = QLabel("Редактор структур для Minecraft 1.21.4\nNeoForge 21.4.157 + IBE Editor мод")
        ibe_desc.setStyleSheet("color:#9080b0;font-size:12px;border:none;background:transparent;")
        ibe_desc.setWordWrap(True)
        ibe_v.addWidget(ibe_desc)

        ibe_ver = QLabel("Версия мода: 1.0.0  •  MC 1.21.4  •  NeoForge")
        ibe_ver.setStyleSheet("color:#7060a0;font-size:11px;border:none;background:transparent;")
        ibe_v.addWidget(ibe_ver)

        self.ibe_progress = QProgressBar()
        self.ibe_progress.setMaximumHeight(18)
        self.ibe_progress.setTextVisible(True)
        self.ibe_progress.setVisible(False)
        ibe_v.addWidget(self.ibe_progress)

        self.ibe_status = QLabel("")
        self.ibe_status.setStyleSheet("color:#9080b0;font-size:11px;border:none;background:transparent;")
        ibe_v.addWidget(self.ibe_status)

        self.btn_ibe_install = QPushButton("⬇ Установить IBE Editor 1.21.4")
        self.btn_ibe_install.setObjectName("ActBtn")
        self.btn_ibe_install.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_ibe_install.clicked.connect(self._on_install_ibe)
        ibe_v.addWidget(self.btn_ibe_install)

        mv.addWidget(ibe_card)

        mv.addStretch(1)

        # Back button
        mod_back_h = QHBoxLayout()
        btn_back_m = QPushButton("← Назад")
        btn_back_m.setObjectName("ActBtn")
        btn_back_m.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_back_m.clicked.connect(lambda: self._switch_page(0))
        mod_back_h.addWidget(btn_back_m)
        mod_back_h.addStretch(1)
        mv.addLayout(mod_back_h)

        self.right_stack.addWidget(mod_page)

    def _build_title_bar(self):
        self.title_bar = QWidget(self.centralWidget())
        self.title_bar.setObjectName("TopBar")
        self.title_bar.setGeometry(0, 0, self.width(), 44)

        h = QHBoxLayout(self.title_bar)
        h.setContentsMargins(14, 0, 6, 0)
        h.setSpacing(8)

        # ─── Иконка слева ───
        icon_path = Path(__file__).parent.parent / "assets" / "icon_amaterasu.png"
        icon_label = QLabel()
        pix = QPixmap(str(icon_path))
        if not pix.isNull():
            pix = pix.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            icon_label.setPixmap(pix)
        icon_label.setFixedSize(32, 32)
        icon_label.setStyleSheet("background: transparent; border: none;")
        h.addWidget(icon_label)

        h.addStretch(1)

        # ─── Кнопки справа ───
        btn_min = QPushButton("─")
        btn_min.setObjectName("WinBtn")
        btn_min.setFixedSize(44, 34)
        btn_min.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_min.clicked.connect(self.showMinimized)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("CloseBtn")
        btn_close.setFixedSize(44, 34)
        btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_close.clicked.connect(self.close)

        for b in (btn_min, btn_close):
            h.addWidget(b)

        # ─── 天照 абсолютно по центру title bar (overlay) ───
        self._title_label = QLabel("天照", self.title_bar)
        self._title_label.setObjectName("TitleBarLabel")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_label.setStyleSheet("background: transparent;")

        title_glow = QGraphicsDropShadowEffect(self._title_label)
        title_glow.setColor(QColor(140, 60, 220, 120))
        if self.settings.get("low_perf", False):
            title_glow.setBlurRadius(8)
        else:
            title_glow.setBlurRadius(16)
        title_glow.setOffset(0, 0)
        self._title_label.setGraphicsEffect(title_glow)
        self._update_title_pos()

    def _update_title_pos(self):
        """Position 天照 at the center of the left panel area (fixed 300px)."""
        if hasattr(self, "_title_label") and hasattr(self, "title_bar"):
            lbl = self._title_label
            lbl.adjustSize()
            # Центрируем относительно левой панели (300px шириной)
            left_panel_width = 300
            x = (left_panel_width - lbl.width()) // 2
            y = (self.title_bar.height() - lbl.height()) // 2 - 2
            lbl.move(x, y)
            lbl.move(x, y)

    def paintEvent(self, event):
        """Draw rounded window background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), QColor(10, 4, 18))
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Rounded window mask
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

        if hasattr(self, "title_bar"):
            self.title_bar.setFixedWidth(self.width())
        self._update_title_pos()
        if hasattr(self, "_particles"):
            self._particles.setGeometry(self.centralWidget().rect())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 44:
            self._drag_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self._drag_pos = event.globalPosition().toPoint()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _show_menu(self):
        pos = self.btn_menu.mapToGlobal(QPoint(-170, self.btn_menu.height() + 4))
        self.popup.move(pos)
        self.popup.show()

    def _switch_page(self, idx: int):
        self.popup.hide()
        if idx == 0:
            if self.right_panel.isVisible():
                self.right_panel.setVisible(False)
                geo = self.geometry()
                self.setGeometry(geo.x() + (geo.width() - self.BASE_WIDTH)//2, geo.y(),
                                 self.BASE_WIDTH, geo.height())
        else:
            self.right_stack.setCurrentIndex(idx)
            if not self.right_panel.isVisible():
                self.right_panel.setVisible(True)
                geo = self.geometry()
                self.setGeometry(geo.x() + (geo.width() - self.EXPANDED_WIDTH)//2, geo.y(),
                                 self.EXPANDED_WIDTH, geo.height())

    def log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def show(self):
        super().show()
        self._fade_in()

    def _fade_in(self):
        """Плавное появление окна."""
        self._fade_opacity = 0.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)  # ~60fps
        self._fade_timer.timeout.connect(self._fade_tick)
        self._fade_timer.start()

    def _fade_tick(self):
        self._fade_opacity += 0.04  # ~0.6 сек до полной непрозрачности
        if self._fade_opacity >= 1.0:
            self._fade_opacity = 1.0
            self._fade_timer.stop()
        self.setWindowOpacity(self._fade_opacity)

    def _open_folder(self):
        import os
        os.startfile(self.mc_dir)
        self.log(f"📁 {self.mc_dir}")

    def refresh_main_versions(self):
        self.version_combo.clear()
        installed = get_installed_versions_list(self.mc_dir)
        if installed:
            self.version_combo.addItems(installed)
            self.log(f"📋 Установлено: {len(installed)}")
        else:
            self.version_combo.addItem("Нет установленных")
            self.log("⚠️ Нет установленных — скачай в Менеджере версий")

    def _on_play(self):
        version = self.version_combo.currentText().strip()
        username = self.nick_edit.text().strip()
        if not version or version.startswith("Нет") or not username:
            self.log("⚠️ Заполни версию и ник")
            return

        self.log(f"📦 {version}...")
        self.play_btn.setEnabled(False)
        self.play_btn.start_loading("ЗАГРУЗКА...")

        # ─── Launch particles from button (отключено в low_perf режиме для оптимизации) ───
        if not self.settings.get("low_perf", False):
            btn_rect = self.play_btn.geometry()
            # Convert to central widget coords
            btn_pos = self.play_btn.mapTo(self.centralWidget(), QPoint(0, 0))
            source_rect = self.play_btn.rect()
            source_rect.moveTopLeft(btn_pos)
            self._particles.setGeometry(self.centralWidget().rect())
            self._particles.start(source_rect)

        self.play_progress.setVisible(True)
        self.play_progress.setMaximum(0)
        self.play_progress.setValue(0)
        self.play_progress.setFormat("Подготовка...")

        self.inst_thread = InstallThread(version, username, self.mc_dir, self.settings, self)
        self.inst_thread.log_signal.connect(self.log)
        self.inst_thread.progress_signal.connect(self._on_play_progress)
        self.inst_thread.finished_signal.connect(self._on_play_done)
        self.inst_thread.error_signal.connect(self._on_play_error)
        self.inst_thread.start()

    def _on_play_progress(self, cur: int, tot: int):
        if tot > 0:
            self.play_progress.setMaximum(tot)
            self.play_progress.setValue(cur)
            self.play_progress.setFormat(f"%p%  ({cur}/{tot})")
        else:
            self.play_progress.setFormat("Загрузка...")

    def _on_play_done(self):
        self.play_btn.setEnabled(True)
        self.play_btn.stop_loading("▶  ЗАПУСТИТЬ")
        self.play_progress.setVisible(False)
        self._particles.stop()

    def _on_play_error(self, msg: str):
        self.log(f"❌ {msg}")
        self.play_btn.setEnabled(True)
        self.play_btn.stop_loading("▶  ЗАПУСТИТЬ")
        self.play_progress.setVisible(False)
        self._particles.stop()

    def _start_version_load(self):
        """Запускает загрузку списка версий в отдельном потоке."""
        self.ver_status.setText("⏳ Загрузка списка версий...")
        self.log("⏳ Загрузка списка версий...")
        self._ver_thread = VersionListThread(self)
        self._ver_thread.finished.connect(self._on_versions_loaded)
        self._ver_thread.error.connect(lambda e: (
            self.ver_status.setText(f"❌ {e}"),
            self.log(f"❌ Ошибка: {e}")
        ))
        self._ver_thread.start()

    def _on_versions_loaded(self, versions: list):
        """Обработка загруженного списка версий."""
        self.all_versions_list.clear()
        self._all_versions_raw = []
        self._all_display_items = []

        if not versions:
            self.ver_status.setText("❌ Список пуст — проверь интернет")
            self.log("❌ Не удалось загрузить версии")
            return

        for v in versions:
            if v.get("type") == "release":
                self._all_versions_raw.append(v)
                vid = v["id"]
                self._all_display_items.append((vid, vid, "vanilla"))
                self._all_display_items.append((f"{vid} — Forge", vid, "forge"))
                self._all_display_items.append((f"{vid} — Fabric", vid, "fabric"))

        for display, _, _ in self._all_display_items:
            self.all_versions_list.addItem(display)

        self.ver_status.setText(f"✅ Загружено {len(self._all_versions_raw)} релизов")
        self.log(f"📋 Доступно {len(self._all_versions_raw)} версий")

    def _load_releases(self):
        self.all_versions_list.clear()
        self._all_versions_raw = []
        self._all_display_items = []
        try:
            versions = get_version_list()
            if not versions:
                self.ver_status.setText("❌ Не удалось загрузить список версий")
                self.log("❌ Список версий пуст — проверь интернет")
                return

            for v in versions:
                if v.get("type") == "release":
                    self._all_versions_raw.append(v)
                    vid = v["id"]
                    self._all_display_items.append((vid, vid, "vanilla"))
                    self._all_display_items.append((f"{vid} — Forge", vid, "forge"))
                    self._all_display_items.append((f"{vid} — Fabric", vid, "fabric"))

            for display, _, _ in self._all_display_items:
                self.all_versions_list.addItem(display)
            self.ver_status.setText(f"Загружено {len(self._all_versions_raw)} релизов")
            self.log(f"📋 Доступно {len(self._all_versions_raw)} версий")
        except Exception as e:
            self.ver_status.setText(f"❌ Ошибка: {e}")
            self.log(f"❌ Ошибка загрузки версий: {e}")

    def _filter_versions(self, text: str):
        self.all_versions_list.clear()
        search = text.lower().replace(".", "").replace(" ", "")
        for display, vid, mod_type in self._all_display_items:
            display_lower = display.lower()
            display_clean = display_lower.replace(".", "").replace(" ", "").replace("—", "")
            if text.lower() in display_lower or search in display_clean:
                self.all_versions_list.addItem(display)

    def _on_download_version(self):
        item = self.all_versions_list.currentItem()
        if not item:
            self.ver_status.setText("Выбери версию из списка")
            return

        display = item.text()

        # Определяем тип: vanilla / forge / fabric
        if "— Forge" in display:
            mod_type = "forge"
            mc_ver = display.replace(" — Forge", "").strip()
        elif "— Fabric" in display:
            mod_type = "fabric"
            mc_ver = display.replace(" — Fabric", "").strip()
        else:
            mod_type = "vanilla"
            mc_ver = display.strip()

        self.ver_status.setText(f"Скачивание {display}...")
        self.btn_download.setEnabled(False)
        self.ver_progress.setVisible(True)
        self.ver_progress.setMaximum(0)
        self.ver_progress.setValue(0)

        self.play_btn.setEnabled(False)
        self.play_btn.start_loading("Ожидание загрузки...")

        if mod_type == "vanilla":
            self.dl_thread = DownloadThread(mc_ver, self.mc_dir, self)
            self.dl_thread.log_signal.connect(self.log)
            self.dl_thread.progress_signal.connect(self._on_ver_progress)
            self.dl_thread.finished_signal.connect(lambda: self._on_dl_done(display))
            self.dl_thread.error_signal.connect(self._on_dl_error)
            self.dl_thread.start()
        else:
            self.mod_thread = ModInstallThread(mod_type, mc_ver, self.mc_dir, self)
            self.mod_thread.log_signal.connect(self.log)
            self.mod_thread.progress_signal.connect(self._on_ver_progress)
            self.mod_thread.finished_signal.connect(lambda vid: self._on_dl_done(display))
            self.mod_thread.error_signal.connect(self._on_dl_error)
            self.mod_thread.start()

    def _on_dl_done(self, display: str):
        self.ver_status.setText(f"✅ {display} установлена")
        self.ver_progress.setVisible(False)
        self.btn_download.setEnabled(True)
        self.play_btn.setEnabled(True)
        self.play_btn.stop_loading("▶  ЗАПУСТИТЬ")
        self.refresh_main_versions()

    def _on_dl_error(self, msg: str):
        self.ver_status.setText(f"❌ {msg}")
        self.ver_progress.setVisible(False)
        self.btn_download.setEnabled(True)
        self.play_btn.setEnabled(True)
        self.play_btn.stop_loading("▶  ЗАПУСТИТЬ")

    def _on_ver_progress(self, cur: int, tot: int):
        if tot > 0:
            self.ver_progress.setMaximum(tot)
            self.ver_progress.setValue(cur)
            self.ver_progress.setFormat(f"%p%  ({cur}/{tot})")
        else:
            self.ver_progress.setFormat("Загрузка...")

    # ─── IBE Editor install ───

    def _on_install_ibe(self):
        self.btn_ibe_install.setEnabled(False)
        self.ibe_status.setText("Начинаю установку...")
        self.ibe_progress.setVisible(True)
        self.ibe_progress.setMaximum(0)
        self.ibe_progress.setValue(0)

        self.ibe_thread = IBEInstallThread(self.mc_dir, self)
        self.ibe_thread.log_signal.connect(self.log)
        self.ibe_thread.progress_signal.connect(self._on_ibe_progress)
        self.ibe_thread.finished_signal.connect(self._on_ibe_done)
        self.ibe_thread.error_signal.connect(self._on_ibe_error)
        self.ibe_thread.start()

    def _on_ibe_progress(self, cur: int, tot: int):
        if tot > 0:
            self.ibe_progress.setMaximum(tot)
            self.ibe_progress.setValue(cur)
            self.ibe_progress.setFormat(f"Шаг {cur}/{tot}")
        else:
            self.ibe_progress.setFormat("Загрузка...")

    def _on_ibe_done(self):
        self.ibe_status.setText("✅ IBE Editor установлен! Выбери версию NeoForge в главном окне.")
        self.ibe_progress.setVisible(False)
        self.btn_ibe_install.setEnabled(True)
        self.btn_ibe_install.setText("✅ Установлено — переустановить")
        self.refresh_main_versions()
        self.log("✅ IBE Editor 1.21.4 установлен!")

    def _on_ibe_error(self, msg: str):
        self.ibe_status.setText(f"❌ {msg}")
        self.ibe_progress.setVisible(False)
        self.btn_ibe_install.setEnabled(True)
        self.log(f"❌ IBE Editor: {msg}")

    def _save_settings(self):
        self.settings["java_path"] = self.set_java.text().strip()
        self.settings["ram"] = self.set_ram.currentText()
        try:
            self.settings["window_width"] = int(self.set_w.text())
            self.settings["window_height"] = int(self.set_h.text())
        except ValueError:
            pass
        self.settings["low_perf"] = self.set_low_perf.isChecked()
        self.settings["mc_perf"] = self.set_mc_perf.isChecked()
        save_settings(self.settings)
        self.log("💾 Настройки сохранены")
        self._switch_page(0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
