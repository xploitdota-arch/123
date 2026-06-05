import sys
import os
import json
import shutil
import subprocess
import base64
import zlib
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QCheckBox, QPushButton, QTextEdit, QLabel,
    QLineEdit, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QProgressBar,
    QStackedWidget, QListWidget, QListWidgetItem, QMessageBox, QSlider, QFileDialog
)
from PyQt6.QtCore import Qt, QPoint, QThread, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QRectF, QRect
from PyQt6.QtCore import QObject
from PyQt6.QtGui import (
    QFont, QFontDatabase, QPixmap, QPainter, QColor, QCursor, QMovie,
    QPen, QPainterPath, QLinearGradient, QRadialGradient, QRegion, QIcon
)
from PyQt6.QtWidgets import QColorDialog
import random
import math

from launcher.api import get_version_list
from launcher.launcher_core import get_offline_uuid
from launcher.java_installer import download_java, find_java_executable, is_java_installed
from launcher.neoforge_installer import install_neoforge
import minecraft_launcher_lib as mll

SETTINGS_PATH = Path(__file__).parent.parent / "launcher_settings.json"

# ─── Скрытие консольных окон дочерних процессов (Windows) ───
# Без этого каждый запуск java/minecraft открывает чёрное окно консоли.
if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    NO_WINDOW_KWARGS = {"creationflags": CREATE_NO_WINDOW, "startupinfo": _si}
else:
    NO_WINDOW_KWARGS = {}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return {"java_path": "", "window_width": 1000, "window_height": 600, "ram": "2G", "low_perf": False, "mc_perf": True, "theme_primary": "#8040c8", "theme_secondary": "#40a8e8", "custom_bg": ""}


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
    launched_signal = pyqtSignal(object)  # subprocess.Popen — процесс Minecraft
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
                    ver_out = subprocess.check_output([java_exe, "-version"], stderr=subprocess.STDOUT, text=True, timeout=10, **NO_WINDOW_KWARGS)
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
            mc_process = subprocess.Popen(cmd, cwd=str(self.mc_dir), **NO_WINDOW_KWARGS)
            self.launched_signal.emit(mc_process)
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
                    check=True, capture_output=True, text=True, **NO_WINDOW_KWARGS
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
        self.mod_type = mod_type  # "forge", "fabric" or "forgeoptifine"
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

            if self.mod_type == "forgeoptifine":
                # ─── OptiFine: сначала ванилла, потом OptiFine ───
                self.log_signal.emit(f"📦 Установка ваниллы {self.mc_version}...")
                mll.install.install_minecraft_version(self.mc_version, str(self.mc_dir), callback=callback)
                installed_id = self.mc_version
                self.log_signal.emit(f"✅ Ванилла установлена")

                if self.mod_type == "forgeoptifine":
                    # ─── ForgeOptiFine: скачиваем OptiFine через зеркала ───
                    import urllib.request
                    import json as _json

                    OF_DL_MIRRORS = [
                        "https://of-302v.zkitefly.eu.org/file/",
                        "https://of-302.zkitefly.eu.org/file/",
                        "https://of-302.burningtnt.workers.dev/file/",
                    ]
                    OF_VERSIONS_APIS = [
                        "https://bmclapi2.bangbang93.com/optifine/",
                    ]
                    OF_DL_API = "https://optifine-dl-link.vercel.app/api"

                    self.log_signal.emit(f"🔮 Поиск OptiFine для {self.mc_version}...")
                    of_filename = None

                    # Шаг 1: попробовать найти версию через API зеркал
                    for api_base in OF_VERSIONS_APIS:
                        try:
                            api_url = api_base + self.mc_version
                            self.log_signal.emit(f"📡 Запрос {api_base.split('//')[1].split('/')[0]}...")
                            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                            resp = urllib.request.urlopen(req, timeout=10)
                            versions_list = _json.loads(resp.read())
                            self.log_signal.emit(f"📋 Найдено {len(versions_list)} версий OptiFine")
                            for v in versions_list:
                                if not v.get("filename", "").startswith("preview_"):
                                    of_filename = v["filename"]
                                    break
                            if not of_filename and versions_list:
                                of_filename = versions_list[0]["filename"]
                            if of_filename:
                                break
                        except Exception as e:
                            self.log_signal.emit(f"⚠️ {api_base.split('//')[1].split('/')[0]}: {e}")

                    # Шаг 2: фоллбэк — Vercel API (нужен патч, пробуем общие)
                    if not of_filename:
                        self.log_signal.emit("📡 Пробуем альтернативный API...")
                        common_patches = ["I7", "I6", "I5", "J9", "J7", "J6", "J4", "J3", "J2", "J1"]
                        for patch in common_patches:
                            try:
                                vurl = f"{OF_DL_API}?mc={self.mc_version}&of={patch}"
                                vreq = urllib.request.Request(vurl, headers={"User-Agent": "Mozilla/5.0"})
                                vresp = urllib.request.urlopen(vreq, timeout=10)
                                vdata = _json.loads(vresp.read())
                                if vdata.get("status") and vdata.get("url"):
                                    of_filename = f"OptiFine_{self.mc_version}_HD_U_{patch}.jar"
                                    # Сразу сохраняем прямую ссылку
                                    self._of_direct_url = vdata["url"]
                                    self.log_signal.emit(f"📦 OptiFine: {of_filename} (Vercel)")
                                    break
                            except Exception:
                                continue

                    if not of_filename:
                        self.log_signal.emit(f"⚠️ OptiFine не найден для {self.mc_version}")
                        self.log_signal.emit("📥 Возможно версии ещё нет — попробуй позже или скачай вручную с optifine.net")
                    else:
                        if not getattr(self, '_of_direct_url', None):
                            self._of_direct_url = None
                        self.log_signal.emit(f"📦 OptiFine: {of_filename}")

                        # Шаг 3: скачать JAR
                        self.log_signal.emit("⬇️ Скачивание OptiFine...")
                        of_jar = self.mc_dir / "optifine_installer.jar"
                        downloaded = False

                        # Если есть прямая ссылка от Vercel — пробуем сначала
                        if self._of_direct_url:
                            try:
                                self.log_signal.emit("📡 Скачивание (Vercel)...")
                                req = urllib.request.Request(self._of_direct_url, headers={"User-Agent": "Mozilla/5.0"})
                                resp = urllib.request.urlopen(req, timeout=120)
                                with open(of_jar, "wb") as f:
                                    while True:
                                        chunk = resp.read(65536)
                                        if not chunk:
                                            break
                                        f.write(chunk)
                                size_kb = of_jar.stat().st_size // 1024
                                self.log_signal.emit(f"✅ Скачано ({size_kb} КБ)")
                                downloaded = True
                            except Exception as e:
                                self.log_signal.emit(f"⚠️ Vercel: {e}")

                        # Зеркала zkitefly
                        if not downloaded:
                            for mirror in OF_DL_MIRRORS:
                                try:
                                    dl_url = mirror + of_filename
                                    host = mirror.split("//")[1].split("/")[0]
                                    self.log_signal.emit(f"📡 {host}...")
                                    req = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0"})
                                    resp = urllib.request.urlopen(req, timeout=120)
                                    with open(of_jar, "wb") as f:
                                        while True:
                                            chunk = resp.read(65536)
                                            if not chunk:
                                                break
                                            f.write(chunk)
                                    size_kb = of_jar.stat().st_size // 1024
                                    self.log_signal.emit(f"✅ Скачано ({size_kb} КБ)")
                                    downloaded = True
                                    break
                                except Exception as e:
                                    self.log_signal.emit(f"⚠️ {host}: {e}")

                        if not downloaded:
                            self.log_signal.emit("⚠️ Не удалось скачать OptiFine — установлен только Forge")
                        else:
                            # Шаг 4: установить OptiFine
                            self.log_signal.emit("🔧 Установка OptiFine...")
                            import subprocess
                            cmd = [str(java_path), "-jar", str(of_jar), "--install", str(self.mc_dir)]
                            self.log_signal.emit(f"🔧 {' '.join(cmd)}")
                            try:
                                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                                if result.stdout.strip():
                                    self.log_signal.emit(f"🔧 {result.stdout.strip()[:300]}")
                                if result.returncode != 0 and result.stderr.strip():
                                    self.log_signal.emit(f"⚠️ {result.stderr.strip()[:200]}")
                            except subprocess.TimeoutExpired:
                                self.log_signal.emit("⚠️ OptiFine installer — таймаут")
                            except Exception as e:
                                self.log_signal.emit(f"⚠️ OptiFine installer: {e}")
                            try:
                                of_jar.unlink()
                            except Exception:
                                pass
                            # Определяем ID профиля ForgeOptiFine
                            versions_dir = self.mc_dir / "versions"
                            of_profile = None
                            if versions_dir.exists():
                                for d in versions_dir.iterdir():
                                    if d.is_dir() and "optifine" in d.name.lower():
                                        of_profile = d.name
                                        break
                            if of_profile:
                                installed_id = of_profile
                                self.log_signal.emit(f"✅ OptiFine: {of_profile}")
                            else:
                                self.log_signal.emit("✅ OptiFine установлен")

            elif self.mod_type == "forge":
                # ─── Forge ───
                self.log_signal.emit(f"🔨 Установка Forge для {self.mc_version}...")
                forge_ver = mll.forge.find_forge_version(self.mc_version)
                if not forge_ver:
                    self.error_signal.emit(f"Forge не найден для {self.mc_version}")
                    return
                self.log_signal.emit(f"📦 Forge: {forge_ver}")
                mll.forge.install_forge_version(forge_ver, str(self.mc_dir), callback=callback, java=java_path)
                installed_id = forge_ver

            else:
                # ─── Fabric ───
                self.log_signal.emit(f"🧵 Установка Fabric для {self.mc_version}...")
                if not mll.fabric.is_minecraft_version_supported(self.mc_version):
                    self.error_signal.emit(f"Fabric не поддерживает {self.mc_version}")
                    return
                mll.fabric.install_fabric(self.mc_version, str(self.mc_dir), callback=callback, java=java_path)
                loader = mll.fabric.get_latest_loader_version()
                installed_id = f"fabric-loader-{loader}-{self.mc_version}"

            type_name = {"vanilla": "Vanilla", "forge": "Forge", "forgeoptifine": "OptiFine", "fabric": "Fabric"}.get(self.mod_type, self.mod_type)
            self.log_signal.emit(f"✅ {type_name} установлен!")

            # Ванильные файлы оставляем (нужны для inheritsFor в Forge/Fabric).
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

        for idx, label in [(1, "Менеджер версий"), (3, "Моды"), (4, "Тема"), (2, "Настройки")]:
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
        # Purple to cyan colors
        self.hue = random.choice([
            random.randint(250, 290),  # purple
            random.randint(180, 210),  # cyan
        ])
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
        self._timer.setInterval(16)  # ~60fps — плавные частицы
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
            grad.setColorAt(0, QColor(100, min(255, val + 50), 255, alpha))
            grad.setColorAt(0.4, color)
            grad.setColorAt(1, QColor(hue % 256, 0, val // 2, 0))

            painter.setBrush(grad)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(p.x - p.size, p.y - p.size, p.size * 2, p.size * 2))

        painter.end()


# ─── Gradient Spinner ───

class GradientSpinner(QWidget):
    """Animated spinner with purple→cyan gradient and comet tail."""

    def __init__(self, size=52, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60fps — плавное вращение
        self._timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def start(self):
        self._timer.start()
        self.show()
        self.update()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 3.5) % 360  # плавнее вращение
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        pen_w = 3.5
        margin = pen_w + 2
        rect = QRectF(margin, margin, w - 2 * margin, h - 2 * margin)

        # Background track
        track_pen = QPen(QColor(40, 20, 70, 50), pen_w,
                         Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(rect, 0, 360 * 16)

        # Gradient arc with comet-tail fade
        segments = 18
        arc_deg = 300
        seg_deg = arc_deg / segments

        for i in range(segments):
            t = i / segments
            alpha = int(255 * (1.0 - t * t))
            r = int(80 + 100 * t)
            g = int(200 - 120 * t)
            b = int(240 - 10 * t)
            start = self._angle + i * seg_deg
            pen = QPen(QColor(r, g, b, alpha), pen_w,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, int(start * 16), int(seg_deg * 16 + 10))

        # Glow at head
        head_rad = math.radians(self._angle)
        cx = rect.center().x() + rect.width() / 2 * math.cos(head_rad)
        cy = rect.center().y() - rect.height() / 2 * math.sin(head_rad)
        glow_r = pen_w * 4
        glow = QRadialGradient(cx, cy, glow_r)
        glow.setColorAt(0, QColor(60, 210, 250, 170))
        glow.setColorAt(0.4, QColor(120, 80, 230, 80))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

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
        self._pulse_timer.setInterval(40)  # ~25fps — плавная пульсация
        self._pulse_timer.timeout.connect(self._pulse_tick)

        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(100)  # плавнее спиннер
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
            self._glow_effect.setColor(QColor(100, 80, 220, 180))
            self._glow_effect.setBlurRadius(28)

    def _pulse_tick(self):
        self._glow_phase += 0.045  # медленнее = плавнее свечение
        if self._glow_effect:
            # Pulsating glow: blur radius oscillates 10..25 (снижено для оптимизации)
            t = (math.sin(self._glow_phase) + 1) / 2  # 0..1
            blur = 10 + t * 15
            alpha = int(80 + t * 80)
            # Shift hue: purple ↔ cyan shimmer
            hue_shift = int(math.sin(self._glow_phase * 0.7) * 30)
            r = max(0, min(255, 100 + hue_shift))
            g = max(0, min(255, 60 + int(math.sin(self._glow_phase * 0.4) * 40)))
            b = max(0, min(255, 230))
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
        grad.setColorAt(0.2, QColor(100, 50, 200, 180))
        grad.setColorAt(0.35, QColor(160, 80, 240, 230))
        grad.setColorAt(0.5, QColor(60, 170, 230, 255))
        grad.setColorAt(0.65, QColor(160, 80, 240, 230))
        grad.setColorAt(0.8, QColor(100, 50, 200, 180))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), grad)
        painter.end()


# ─── Stylesheet ───

def stylesheet(mc_font: str = "Segoe UI", title_font: str = "Segoe UI",
               primary: str = "#8040c8", secondary: str = "#40a8e8") -> str:
    # Parse hex → r,g,b
    pr, pg, pb = int(primary[1:3],16), int(primary[3:5],16), int(primary[5:7],16)
    sr, sg, sb = int(secondary[1:3],16), int(secondary[3:5],16), int(secondary[5:7],16)
    # Dark panel bg derived from primary
    pdr = min(255, pr // 4); pdg = min(255, pg // 4); pdb = min(255, pb // 4)
    # Border colors
    bra = f"rgba({pr},{pg},{pb},90)"; brb = f"rgba({sr},{sg},{sb},130)"
    # Button gradient
    b1 = primary; b2 = secondary
    return f"""
    QWidget{{color:#e0d0f0;font-family:"Segoe UI";font-size:13px;}}
    #LeftPanel{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 rgba({pdr+4},{pdg+2},{pdb+8},190),
            stop:1 rgba({pdr},{pdg},{pdb},190));
        border:none;
        border-right:2px solid qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {bra},stop:0.5 {brb},stop:1 {bra});
    }}
    #RightPanel{{
        background-color:qlineargradient(x1:1,y1:0,x2:0,y2:1,
            stop:0 rgba({pdr+4},{pdg+2},{pdb+8},190),
            stop:1 rgba({pdr},{pdg},{pdb},190));
        border:none;
        border-left:2px solid qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {bra},stop:0.5 {brb},stop:1 {bra});
    }}
    #CenterBg{{
        background-color:#08040e;
    }}
    #TopBar{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 rgba({pdr+8},{pdg+4},{pdb+10},200),
            stop:0.5 rgba({pdr+2},{pdg},{pdb+4},200),
            stop:1 rgba({pdr+8},{pdg+4},{pdb+10},200));
        border-bottom:1px solid qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 rgba({pr},{pg},{pb},50),
            stop:0.5 rgba({sr},{sg},{sb},100),
            stop:1 rgba({pr},{pg},{pb},50));
    }}

    QLineEdit{{
        background-color:rgba({pdr+10},{pdg+5},{pdb+15},200);
        border:1px solid rgba({pr},{pg},{pb},100);
        border-radius:8px;
        padding:8px 12px;
        color:#e0d0f0;
        font-size:13px;
        selection-background-color:{primary};
    }}
    QLineEdit:focus{{border:1px solid {primary};}}
    QLineEdit::placeholder{{color:rgba(160,130,200,120);}}

    QComboBox{{
        background-color:rgba({pdr+10},{pdg+5},{pdb+15},200);
        border:1px solid rgba({pr},{pg},{pb},100);
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
        border-top:7px solid {primary};
        width:0;height:0;margin-top:2px;
    }}
    QComboBox QAbstractItemView{{
        background:rgba({pdr},{pdg},{pdb},240);
        border:1px solid rgba({pr},{pg},{pb},120);
        selection-background-color:rgba({pr},{pg},{pb},80);
        color:#e0d0f0;
        padding:4px;
        border-radius:6px;
    }}

    QCheckBox{{spacing:8px;color:#a090c0;font-size:12px;}}
    QCheckBox::indicator{{
        width:18px;height:18px;border-radius:4px;
        border:1px solid rgba({pr},{pg},{pb},150);
        background:rgba({pdr+8},{pdg+4},{pdb+12},200);
    }}
    QCheckBox::indicator:hover{{border:1px solid {primary};}}
    QCheckBox::indicator:checked{{
        background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {primary},stop:1 {secondary});
        border:1px solid {primary};
    }}

    QProgressBar{{
        background-color:rgba({pdr+8},{pdg+4},{pdb+12},200);
        border:1px solid rgba({pr},{pg},{pb},80);
        border-radius:6px;
        color:#e0d0f0;
        text-align:center;
        font-size:11px;
        max-height:20px;
    }}
    QProgressBar::chunk{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {primary},stop:0.5 rgba({(pr+sr)//2},{(pg+sg)//2},{(pb+sb)//2},255),stop:1 {secondary});
        border-radius:5px;
    }}

    #LaunchBtn{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {primary},stop:0.5 rgba({(pr+sr)//2},{(pg+sg)//2},{(pb+sb)//2},255),stop:1 {secondary});
        border:1px solid rgba({pr},{pg},{pb},180);
        border-radius:12px;
        padding:14px;
        font-family:"{mc_font}";
        font-size:16px;
        font-weight:bold;
        color:#fff;
        margin:6px 0;
    }}
    #LaunchBtn:hover{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 rgba({min(255,pr+30)},{min(255,pg+20)},{min(255,pb+20)},255),
            stop:1 rgba({min(255,sr+30)},{min(255,sg+20)},{min(255,sb+20)},255));
        border:1px solid rgba({min(255,pr+40)},{min(255,pg+30)},{min(255,pb+30)},255);
    }}
    #LaunchBtn:pressed{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 rgba({pdr},{pdg},{pdb},255),stop:1 rgba({pdr+20},{pdg+10},{pdb+20},255));
        padding-top:15px;padding-bottom:13px;
    }}
    #LaunchBtn:disabled{{
        background-color:rgba({pdr},{pdg},{pdb},200);
        color:rgba({pr},{pg},{pb},120);
        border:1px solid rgba({pr},{pg},{pb},50);
    }}

    QTextEdit#Log{{
        background-color:rgba({pdr},{pdg},{pdb},220);
        border:1px solid rgba({pr},{pg},{pb},50);
        border-radius:8px;
        color:#c0a0e0;
        font-family:Consolas,"Courier New",monospace;
        font-size:11px;
        padding:8px;
        selection-background-color:rgba({pr},{pg},{pb},80);
    }}

    #WinBtn{{
        background:transparent;border:none;
        color:#8070a0;font-size:16px;
        padding:2px 10px;border-radius:6px;
    }}
    #WinBtn:hover{{background-color:rgba({pr},{pg},{pb},40);color:#e0d0f0;}}

    #CloseBtn{{
        background:transparent;border:none;
        color:#8070a0;font-size:16px;
        padding:2px 10px;border-radius:6px;
    }}
    #CloseBtn:hover{{background-color:{primary};color:#fff;}}

    QListWidget{{
        background-color:rgba({pdr+4},{pdg+2},{pdb+6},220);
        border:1px solid rgba({pr},{pg},{pb},60);
        border-radius:8px;
        color:#e0d0f0;
        outline:none;
    }}
    QListWidget::item{{
        padding:8px 12px;
        border-bottom:1px solid rgba({pr},{pg},{pb},20);
    }}
    QListWidget::item:selected{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 rgba({pr},{pg},{pb},60),stop:1 rgba({sr},{sg},{sb},40));
        color:#fff;
        border-radius:6px;
    }}
    QListWidget::item:hover{{background-color:rgba({pr},{pg},{pb},30);}}

    #ActBtn{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 rgba({pdr+10},{pdg+5},{pdb+15},255),stop:1 rgba({pdr},{pdg},{pdb},255));
        border:1px solid rgba({pr},{pg},{pb},120);
        border-radius:8px;
        padding:9px 18px;
        color:#d0c0e0;
        font-family:"{mc_font}";
        font-size:13px;
    }}
    #ActBtn:checked{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {primary},stop:1 {secondary});
        border:1px solid {primary};
        color:#fff;
    }}
    #ActBtn:hover{{
        background-color:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 rgba({pr},{pg},{pb},80),stop:1 rgba({sr},{sg},{sb},60));
        border:1px solid rgba({min(255,pr+30)},{min(255,pg+20)},{min(255,pb+20)},200);
        color:#fff;
    }}

    #PageTitle{{
        font-family:"{mc_font}";
        font-size:20px;
        font-weight:bold;
        color:{primary};
        padding-bottom:8px;
    }}
    #McLabel{{
        font-family:"{mc_font}";
        font-size:12px;
        color:rgba({(pr+sr)//2},{(pg+sg)//2},{(pb+sb)//2},180);
    }}
    #TitleBarLabel{{
        font-family:"{title_font}";
        font-size:22px;
        font-weight:normal;
        color:#ffffff;
        letter-spacing:4px;
    }}
    #VersionLabel{{
        color:rgba({pr},{pg},{pb},120);
        font-size:11px;
    }}
    #ConsoleLabel{{
        color:rgba({pr},{pg},{pb},100);
        font-size:11px;
        font-family:"{mc_font}";
    }}

    QSlider::groove:horizontal{{
        background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 rgba({pdr+8},{pdg+4},{pdb+12},220),stop:1 rgba({pdr},{pdg},{pdb},200));
        height:8px;
        border-radius:4px;
        border:1px solid rgba({pr},{pg},{pb},80);
    }}
    QSlider::handle:horizontal{{
        background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {primary},stop:1 rgba({max(0,pr-40)},{max(0,pg-20)},{max(0,pb-20)},255));
        border:2px solid rgba({min(255,pr+40)},{min(255,pg+30)},{min(255,pb+30)},255);
        width:20px;
        height:20px;
        margin:-7px 0;
        border-radius:10px;
    }}
    QSlider::handle:horizontal:hover{{
        background:{primary};
        border:2px solid rgba({min(255,pr+60)},{min(255,pg+50)},{min(255,pb+50)},255);
    }}
    QSlider::sub-page:horizontal{{
        background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {primary},stop:0.5 rgba({(pr+sr)//2},{(pg+sg)//2},{(pb+sb)//2},255),stop:1 {secondary});
        border-radius:4px;
    }}
    QSlider::tick-mark:horizontal{{
        background:rgba({pr},{pg},{pb},60);
        height:4px;
        width:1px;
    }}

    QScrollBar:vertical{{
        background:rgba({pdr},{pdg},{pdb},150);
        width:8px;
        border-radius:4px;
        margin:2px;
    }}
    QScrollBar::handle:vertical{{
        background:rgba({pr},{pg},{pb},120);
        border-radius:4px;
        min-height:30px;
    }}
    QScrollBar::handle:vertical:hover{{background:rgba({pr},{pg},{pb},180);}}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
    QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:none;}}
    """


# ─── Launch Overlay (full-screen progress on GIF) ───

class LaunchOverlay(QWidget):
    """Overlay on GIF area: shows launch progress, version name, spinner."""

    mc_closed_signal = pyqtSignal()  # Minecraft process exited

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.hide()

        self._progress = 0.0       # 0..1
        self._target = 0.0
        self._phase = 0.0
        self._status = ""
        self._version = ""
        self._alpha = 0.0
        self._active = False
        self._particles: list[Particle] = []
        self._waiting_mode = False  # "Закройте Minecraft" screen
        self._mc_process = None     # subprocess.Popen

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        # Таймер проверки процесса Minecraft
        self._mc_check_timer = QTimer(self)
        self._mc_check_timer.setInterval(1500)
        self._mc_check_timer.timeout.connect(self._check_mc_process)

    def start(self, version: str):
        self._version = version
        self._status = "Подготовка..."
        self._progress = 0.0
        self._target = 0.0
        self._phase = 0.0
        self._alpha = 0.0
        self._active = True
        self._particles.clear()
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self):
        self._active = False

    def start_waiting(self, mc_process):
        """Переход в режим ожидания закрытия Minecraft."""
        self._waiting_mode = True
        self._mc_process = mc_process
        self._status = "Закройте Minecraft чтобы вернуться"
        self._target = 1.0
        self._progress = 1.0
        self._particles.clear()
        self._mc_check_timer.start()

    def _check_mc_process(self):
        """Проверяет, закрыт ли Minecraft."""
        if self._mc_process is not None:
            ret = self._mc_process.poll()
            if ret is not None:
                # Minecraft закрыт
                self._mc_check_timer.stop()
                self._mc_process = None
                self._waiting_mode = False
                self.mc_closed_signal.emit()

    def set_status(self, text: str):
        self._status = text

    def set_progress(self, cur: int, tot: int):
        if tot > 0:
            self._target = cur / tot

    def _tick(self):
        self._phase += 0.03

        # Keep overlay sized to parent (fixes stretching during animations)
        parent = self.parentWidget()
        if parent:
            self.setGeometry(parent.rect())

        # Fade in — плавнее
        if self._active and self._alpha < 1.0:
            self._alpha = min(1.0, self._alpha + 0.035)

        # Smooth progress — плавнее интерполяция
        diff = self._target - self._progress
        self._progress += diff * 0.04

        # Spawn edge particles (not in waiting mode)
        if self._active and not self._waiting_mode and self._progress > 0.01:
            bar_y = self.height() // 2 + 40
            bar_x = 60 + self._progress * (self.width() - 120)
            if random.random() < 0.4:
                self._particles.append(Particle(
                    bar_x + random.randint(-8, 8),
                    bar_y + random.randint(-6, 6)
                ))

        # Update particles
        for p in self._particles:
            p.update()
        self._particles = [p for p in self._particles if p.is_alive()]
        if len(self._particles) > 25:
            self._particles = self._particles[-25:]

        # Fade out when stopped — плавнее
        if not self._active:
            self._alpha = max(0.0, self._alpha - 0.025)
            if self._alpha <= 0:
                self._timer.stop()
                self.hide()
                self._particles.clear()
                return

        self.update()

    def paintEvent(self, event):
        if self._alpha <= 0:
            return

        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._alpha)

        # Semi-transparent dark overlay
        painter.fillRect(QRectF(0, 0, w, h), QColor(6, 3, 12, 190))

        cx = w / 2
        cy = h / 2

        # ─── Animated glow orb ───
        orb_r = 90 + math.sin(self._phase) * 25
        orb_grad = QRadialGradient(cx, cy - 20, orb_r)
        orb_grad.setColorAt(0, QColor(100, 60, 220, 35))
        orb_grad.setColorAt(0.5, QColor(50, 140, 230, 15))
        orb_grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, w, h), orb_grad)

        if self._waiting_mode:
            self._paint_waiting(painter, w, h, cx, cy)
        else:
            self._paint_loading(painter, w, h, cx, cy - 40)

        painter.end()

    def _paint_waiting(self, painter, w, h, cx, cy):
        """Отрисовка экрана 'Закройте Minecraft'."""
        # ─── Pulsing circle ───
        pulse = (math.sin(self._phase * 1.5) + 1) / 2  # 0..1
        ring_r = 40 + pulse * 8
        ring_alpha = int(60 + pulse * 40)
        painter.setPen(QPen(QColor(100, 180, 240, ring_alpha), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(cx - ring_r, cy - 50 - ring_r, ring_r * 2, ring_r * 2))

        # ─── Inner glow ───
        inner = QRadialGradient(cx, cy - 50, ring_r * 0.7)
        inner.setColorAt(0, QColor(80, 160, 230, int(20 + pulse * 15)))
        inner.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(inner)
        painter.drawEllipse(QRectF(cx - ring_r, cy - 50 - ring_r, ring_r * 2, ring_r * 2))

        # ─── Main text ───
        painter.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        # Gentle color pulsing
        text_alpha = int(200 + pulse * 55)
        painter.setPen(QColor(180, 210, 240, text_alpha))
        painter.drawText(QRectF(0, cy - 5, w, 40),
                         Qt.AlignmentFlag.AlignCenter, "Закройте Minecraft чтобы вернуться")

        # ─── Subtitle ───
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(120, 110, 160, 160))
        painter.drawText(QRectF(0, cy + 40, w, 25),
                         Qt.AlignmentFlag.AlignCenter, "Игра запущена • Amaterasu")

        # ─── Decorative line ───
        line_y = cy + 30
        line_w = 200
        line_grad = QLinearGradient(cx - line_w / 2, 0, cx + line_w / 2, 0)
        line_grad.setColorAt(0, QColor(0, 0, 0, 0))
        line_grad.setColorAt(0.3, QColor(80, 50, 180, int(80 + pulse * 40)))
        line_grad.setColorAt(0.5, QColor(60, 160, 230, int(120 + pulse * 60)))
        line_grad.setColorAt(0.7, QColor(80, 50, 180, int(80 + pulse * 40)))
        line_grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(line_grad)
        painter.drawRect(QRectF(cx - line_w / 2, line_y, line_w, 2))

        # ─── Version text ───
        painter.setFont(QFont("Segoe UI", 10))
        painter.setPen(QColor(90, 80, 130, 120))
        painter.drawText(QRectF(0, cy + 70, w, 20),
                         Qt.AlignmentFlag.AlignCenter, self._version)

    def _paint_loading(self, painter, w, h, cx, cy):
        """Отрисовка экрана загрузки (прогресс-бар, спиннер)."""
        # ─── Version text ───
        painter.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        painter.setPen(QColor(200, 170, 240, 220))
        painter.drawText(QRectF(0, cy - 70, w, 30), Qt.AlignmentFlag.AlignCenter, self._version)

        # ─── Spinning icon ───
        spinner_r = 18
        spinner_cx = cx
        spinner_cy = cy - 15
        for i in range(12):
            angle = math.radians(self._phase * 60 + i * 30)
            alpha_dot = int(255 * (1.0 - i / 12))
            dot_x = spinner_cx + spinner_r * math.cos(angle)
            dot_y = spinner_cy + spinner_r * math.sin(angle)
            t = i / 12
            painter.setBrush(QColor(
                int(80 + 120 * t),
                int(200 - 80 * t),
                int(240),
                alpha_dot
            ))
            painter.setPen(Qt.PenStyle.NoPen)
            size = 3.5 - i * 0.15
            painter.drawEllipse(QRectF(dot_x - size, dot_y - size, size * 2, size * 2))

        # ─── Status text ───
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(160, 140, 200, 200))
        painter.drawText(QRectF(0, cy + 15, w, 25), Qt.AlignmentFlag.AlignCenter, self._status)

        # ─── Progress bar ───
        bar_x = 60
        bar_y = cy + 50
        bar_w = w - 120
        bar_h = 10

        # Bar background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 10, 35, 200))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 5, 5)

        # Bar border
        painter.setPen(QPen(QColor(80, 50, 140, 80), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 5, 5)

        # Bar fill
        if self._progress > 0.005:
            fill_w = max(10, self._progress * bar_w)
            fill_grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
            fill_grad.setColorAt(0, QColor(80, 40, 180))
            fill_grad.setColorAt(0.4, QColor(60, 140, 230))
            fill_grad.setColorAt(0.8, QColor(80, 200, 240))
            fill_grad.setColorAt(1, QColor(140, 100, 240))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_grad)
            painter.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 5, 5)

            # Glow on leading edge
            edge_x = bar_x + fill_w
            edge_glow = QRadialGradient(edge_x, bar_y + bar_h / 2, 25)
            edge_glow.setColorAt(0, QColor(60, 180, 240, 120))
            edge_glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setBrush(edge_glow)
            painter.drawRect(QRectF(edge_x - 25, bar_y - 15, 50, bar_h + 30))

        # ─── Percentage ───
        pct = int(self._progress * 100)
        painter.setFont(QFont("Segoe UI", 10))
        painter.setPen(QColor(180, 160, 220, 180))
        painter.drawText(QRectF(0, bar_y + bar_h + 8, w, 20),
                         Qt.AlignmentFlag.AlignCenter, f"{pct}%")

        # ─── Particles ───
        for p in self._particles:
            alpha_p = max(0, min(255, int(p.life * 200)))
            color = QColor(60, 170, 240, alpha_p)
            glow = QRadialGradient(p.x, p.y, p.size * 2.5)
            glow.setColorAt(0, QColor(120, 200, 255, alpha_p))
            glow.setColorAt(0.5, color)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setBrush(glow)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(
                p.x - p.size, p.y - p.size,
                p.size * 2, p.size * 2
            ))

        painter.end()


# ─── Main Window ───

class MainWindow(QMainWindow):
    BASE_WIDTH = 960
    EXPANDED_WIDTH = 1420

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)  # для drag & drop фона
        self.setWindowTitle("Amaterasu")  # Чтобы в таскбаре и hover показывалось Amaterasu
        self.resize(self.BASE_WIDTH, 580)
        self.setMinimumSize(820, 480)
        self._drag_pos = None
        self._panel_animating = False
        self._panel_opacity = None
        self._launch_animating = False
        self._launch_version = ""
        self._launch_username = ""

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
        self.left_panel = QFrame()
        self.left_panel.setObjectName("LeftPanel")
        self.left_panel.setFixedWidth(300)
        v = QVBoxLayout(self.left_panel)
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

        main_h.addWidget(self.left_panel)

        # CENTER (GIF)
        bg_path = Path(__file__).parent.parent / "assets" / "bg_amaterasu"
        self.center_gif = GifBg(bg_path)
        self.center_gif.setObjectName("CenterBg")
        main_h.addWidget(self.center_gif, 1)

        # RIGHT PANEL (initially hidden)
        self.right_panel = QFrame()
        self.right_panel.setObjectName("RightPanel")
        self.right_panel.setFixedWidth(440)
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

        # ─── Launch overlay (progress on GIF area) ───
        self._launch_overlay = LaunchOverlay(central)
        self._launch_overlay.hide()
        self._launch_animating = False

        self._apply_theme()
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

        # Spinner overlay на список версий
        self.ver_spinner = GradientSpinner(52, self.all_versions_list)
        self.ver_spinner.hide()

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

        ram_title = QLabel("RAM:")
        ram_title.setObjectName("McLabel")
        sv.addWidget(ram_title)
        ram_h = QHBoxLayout()
        ram_h.setSpacing(12)
        self._ram_values = ["1G","2G","3G","4G","6G","8G","12G","16G"]
        self.set_ram = QSlider(Qt.Orientation.Horizontal)
        self.set_ram.setRange(0, len(self._ram_values) - 1)
        self.set_ram.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.set_ram.setTickInterval(1)
        self.set_ram.setSingleStep(1)
        self.set_ram.setPageStep(1)
        current_ram = self.settings.get("ram", "2G")
        if current_ram in self._ram_values:
            self.set_ram.setValue(self._ram_values.index(current_ram))
        self.ram_label = QLabel(current_ram)
        self.ram_label.setStyleSheet("color:#a080e0;font-size:18px;font-weight:bold;min-width:44px;")
        self.ram_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_ram.valueChanged.connect(lambda i: self.ram_label.setText(self._ram_values[i]))
        ram_h.addWidget(self.set_ram, 1)
        ram_h.addWidget(self.ram_label)
        sv.addLayout(ram_h)

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
        ibe_card.setObjectName("IbeCard")
        ibe_card.setStyleSheet("""
            QFrame#IbeCard {
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

        # 4: Theme page — с scroll area
        from PyQt6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        theme_inner = QWidget()
        tv = QVBoxLayout(theme_inner)
        tv.setContentsMargins(16, 12, 16, 12)
        tv.setSpacing(0)

        title4 = QLabel("🎨 Тема")
        title4.setObjectName("PageTitle")
        tv.addWidget(title4)
        tv.addSpacing(16)

        # ─── Colors ───
        colors_header = QLabel("Цвета")
        colors_header.setStyleSheet("font-size:15px;font-weight:bold;color:rgba(255,255,255,200);")
        tv.addWidget(colors_header)
        tv.addSpacing(10)

        p1_h = QHBoxLayout()
        p1_h.setSpacing(12)
        self._primary_color = self.settings.get("theme_primary", "#8040c8")
        self.btn_color_primary = QPushButton()
        self.btn_color_primary.setFixedSize(40, 40)
        self.btn_color_primary.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_color_primary.setToolTip("Основной цвет — нажми чтобы изменить")
        self._update_color_btn(self.btn_color_primary, self._primary_color)
        self.btn_color_primary.clicked.connect(self._pick_primary_color)
        p1_lbl = QLabel("Основной")
        p1_lbl.setFixedWidth(100)
        self._primary_hex = QLabel(self._primary_color)
        self._primary_hex.setFixedWidth(70)
        p1_h.addWidget(self.btn_color_primary)
        p1_h.addWidget(p1_lbl)
        p1_h.addWidget(self._primary_hex)
        p1_h.addStretch(1)
        tv.addLayout(p1_h)
        tv.addSpacing(8)

        p2_h = QHBoxLayout()
        p2_h.setSpacing(12)
        self._secondary_color = self.settings.get("theme_secondary", "#40a8e8")
        self.btn_color_secondary = QPushButton()
        self.btn_color_secondary.setFixedSize(40, 40)
        self.btn_color_secondary.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_color_secondary.setToolTip("Дополнительный цвет — нажми чтобы изменить")
        self._update_color_btn(self.btn_color_secondary, self._secondary_color)
        self.btn_color_secondary.clicked.connect(self._pick_secondary_color)
        p2_lbl = QLabel("Дополнительный")
        p2_lbl.setFixedWidth(100)
        self._secondary_hex = QLabel(self._secondary_color)
        self._secondary_hex.setFixedWidth(70)
        p2_h.addWidget(self.btn_color_secondary)
        p2_h.addWidget(p2_lbl)
        p2_h.addWidget(self._secondary_hex)
        p2_h.addStretch(1)
        tv.addLayout(p2_h)
        tv.addSpacing(12)

        self._gradient_preview = QLabel()
        self._gradient_preview.setFixedHeight(24)
        self._gradient_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_gradient_preview()
        tv.addWidget(self._gradient_preview)
        tv.addSpacing(12)

        btn_reset_theme = QPushButton("🔄 Сбросить цвета")
        btn_reset_theme.setObjectName("ActBtn")
        btn_reset_theme.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_reset_theme.clicked.connect(self._reset_theme)
        tv.addWidget(btn_reset_theme)
        tv.addSpacing(20)

        tv.addWidget(GlowSeparator())
        tv.addSpacing(20)

        # ─── Background ───
        bg_header = QLabel("🖼 Фон")
        bg_header.setStyleSheet("font-size:15px;font-weight:bold;color:rgba(255,255,255,200);")
        tv.addWidget(bg_header)
        tv.addSpacing(6)

        bg_lbl = QLabel("Перетащи GIF / PNG / JPG на окно\nили выбери файл вручную")
        bg_lbl.setWordWrap(True)
        bg_lbl.setStyleSheet("color:rgba(255,255,255,160);font-size:12px;")
        tv.addWidget(bg_lbl)
        tv.addSpacing(8)

        self._bg_path_label = QLabel("")
        custom_bg = self.settings.get("custom_bg", "")
        if custom_bg and Path(custom_bg).exists():
            self._bg_path_label.setText(f"📂 {Path(custom_bg).name}")
        tv.addWidget(self._bg_path_label)
        tv.addSpacing(8)

        bg_btn_h = QHBoxLayout()
        bg_btn_h.setSpacing(10)
        self.btn_pick_bg = QPushButton("📁 Выбрать файл")
        self.btn_pick_bg.setObjectName("ActBtn")
        self.btn_pick_bg.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_pick_bg.clicked.connect(self._pick_background)
        bg_btn_h.addWidget(self.btn_pick_bg)
        btn_reset_bg = QPushButton("🔄 Сбросить фон")
        btn_reset_bg.setObjectName("ActBtn")
        btn_reset_bg.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_reset_bg.clicked.connect(self._reset_background)
        bg_btn_h.addWidget(btn_reset_bg)
        bg_btn_h.addStretch(1)
        tv.addLayout(bg_btn_h)
        tv.addSpacing(20)

        tv.addWidget(GlowSeparator())
        tv.addSpacing(20)

        # ─── Share ───
        share_header = QLabel("📤 Поделиться темой")
        share_header.setStyleSheet("font-size:15px;font-weight:bold;color:rgba(255,255,255,200);")
        tv.addWidget(share_header)
        tv.addSpacing(10)

        btn_export = QPushButton("💾 Сохранить тему в файл")
        btn_export.setObjectName("ActBtn")
        btn_export.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_export.clicked.connect(self._export_theme)
        tv.addWidget(btn_export)

        self._share_status = QLabel("")
        self._share_status.setObjectName("VersionLabel")
        tv.addWidget(self._share_status)

        tv.addStretch(1)
        tv.addSpacing(12)

        # Back
        btn_back_t = QPushButton("← Назад")
        btn_back_t.setObjectName("ActBtn")
        btn_back_t.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_back_t.clicked.connect(lambda: self._switch_page(0))
        tv.addWidget(btn_back_t)

        scroll.setWidget(theme_inner)
        self.right_stack.addWidget(scroll)

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
        title_glow.setColor(QColor(100, 110, 240, 150))
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
        """Draw rounded window background with blur-through."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        painter.setClipPath(path)
        alpha = 230 if getattr(self, '_blur_enabled', False) else 255
        painter.fillRect(self.rect(), QColor(10, 4, 18, alpha))
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
        if hasattr(self, "_launch_overlay") and self._launch_overlay:
            self._launch_overlay.setGeometry(self.centralWidget().rect())

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

    # ─── Drag & Drop background ───

    def dragEnterEvent(self, event):
        """Разрешаем перетаскивание файлов-изображений, .amts тем и OptiFine .jar."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    ext = Path(url.toLocalFile()).suffix.lower()
                    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".amts"):
                        event.acceptProposedAction()
                        return

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Обрабатываем перетаскивание файла — фон, тема или OptiFine .jar."""
        for url in event.mimeData().urls():
            if url.isLocalFile():
                file_path = url.toLocalFile()
                ext = Path(file_path).suffix.lower()
                if ext == ".amts":
                    self._apply_theme_from_file(file_path)
                    return

                elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    self._set_background_from_file(file_path)
                    return

    def _show_menu(self):
        pos = self.btn_menu.mapToGlobal(QPoint(-170, self.btn_menu.height() + 4))
        self.popup.move(pos)
        self.popup.show()

    def _switch_page(self, idx: int):
        self.popup.hide()
        if idx == 0:
            if self.right_panel.isVisible() or self._panel_animating:
                self._animate_panel_close()
        else:
            self.right_stack.setCurrentIndex(idx)
            if not self.right_panel.isVisible() and not self._panel_animating:
                self._animate_panel_open()

    # ─── Panel slide animation ───

    def _animate_panel_open(self):
        """Плавное выезжание правой панели."""
        target_width = 440
        duration = 500

        # Останавливаем предыдущую анимацию если есть
        self._stop_panel_anims()

        self._panel_animating = True
        self.right_panel.setVisible(True)
        self.right_panel.setFixedWidth(1)

        # Fade-in эффект для содержимого панели
        if not hasattr(self, '_panel_opacity') or self._panel_opacity is None:
            self._panel_opacity = QGraphicsOpacityEffect(self.right_stack)
            self.right_stack.setGraphicsEffect(self._panel_opacity)
        self._panel_opacity.setOpacity(0.0)

        # Анимация ширины панели (minimumWidth)
        self._anim_panel_min = QPropertyAnimation(self.right_panel, b"minimumWidth")
        self._anim_panel_min.setDuration(duration)
        self._anim_panel_min.setStartValue(1)
        self._anim_panel_min.setEndValue(target_width)
        self._anim_panel_min.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Анимация ширины панели (maximumWidth)
        self._anim_panel_max = QPropertyAnimation(self.right_panel, b"maximumWidth")
        self._anim_panel_max.setDuration(duration)
        self._anim_panel_max.setStartValue(1)
        self._anim_panel_max.setEndValue(target_width)
        self._anim_panel_max.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Анимация размера окна
        geo = self.geometry()
        target_x = geo.x() + (geo.width() - self.EXPANDED_WIDTH) // 2
        target_geo = QRect(target_x, geo.y(), self.EXPANDED_WIDTH, geo.height())

        self._anim_window = QPropertyAnimation(self, b"geometry")
        self._anim_window.setDuration(duration)
        self._anim_window.setStartValue(geo)
        self._anim_window.setEndValue(target_geo)
        self._anim_window.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Анимация прозрачности содержимого
        self._anim_fade = QPropertyAnimation(self._panel_opacity, b"opacity")
        self._anim_fade.setDuration(int(duration * 0.65))
        self._anim_fade.setStartValue(0.0)
        self._anim_fade.setEndValue(1.0)
        self._anim_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        # Задержка перед появлением контента
        self._anim_fade.setStartValue(0.0)
        QTimer.singleShot(int(duration * 0.25), self._anim_fade.start)

        # Все завершения
        self._anim_panel_min.finished.connect(self._on_panel_open_done)

        self._anim_panel_min.start()
        self._anim_panel_max.start()
        self._anim_window.start()

    def _on_panel_open_done(self):
        self._panel_animating = False
        self.right_panel.setFixedWidth(440)

    def _animate_panel_close(self):
        """Плавное уезжание правой панели."""
        duration = 400

        self._stop_panel_anims()
        self._panel_animating = True

        current_width = self.right_panel.width()

        # Fade-out для содержимого
        if hasattr(self, '_panel_opacity') and self._panel_opacity:
            self._anim_fade_out = QPropertyAnimation(self._panel_opacity, b"opacity")
            self._anim_fade_out.setDuration(int(duration * 0.45))
            self._anim_fade_out.setStartValue(self._panel_opacity.opacity())
            self._anim_fade_out.setEndValue(0.0)
            self._anim_fade_out.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim_fade_out.start()

        # Анимация ширины панели
        self._anim_panel_min = QPropertyAnimation(self.right_panel, b"minimumWidth")
        self._anim_panel_min.setDuration(duration)
        self._anim_panel_min.setStartValue(current_width)
        self._anim_panel_min.setEndValue(0)
        self._anim_panel_min.setEasingCurve(QEasingCurve.Type.InOutQuart)

        self._anim_panel_max = QPropertyAnimation(self.right_panel, b"maximumWidth")
        self._anim_panel_max.setDuration(duration)
        self._anim_panel_max.setStartValue(current_width)
        self._anim_panel_max.setEndValue(0)
        self._anim_panel_max.setEasingCurve(QEasingCurve.Type.InOutQuart)

        # Анимация размера окна
        geo = self.geometry()
        target_x = geo.x() + (geo.width() - self.BASE_WIDTH) // 2
        target_geo = QRect(target_x, geo.y(), self.BASE_WIDTH, geo.height())

        self._anim_window = QPropertyAnimation(self, b"geometry")
        self._anim_window.setDuration(duration)
        self._anim_window.setStartValue(geo)
        self._anim_window.setEndValue(target_geo)
        self._anim_window.setEasingCurve(QEasingCurve.Type.InOutQuart)

        self._anim_panel_min.finished.connect(self._on_panel_close_done)

        self._anim_panel_min.start()
        self._anim_panel_max.start()
        self._anim_window.start()

    def _on_panel_close_done(self):
        self._panel_animating = False
        self.right_panel.setVisible(False)
        self.right_panel.setFixedWidth(440)

    def _stop_panel_anims(self):
        """Останавливает все текущие анимации панели."""
        for attr in ('_anim_panel_min', '_anim_panel_max', '_anim_window',
                      '_anim_fade', '_anim_fade_out'):
            anim = getattr(self, attr, None)
            if anim is not None:
                anim.stop()
                anim.deleteLater()
                setattr(self, attr, None)

    # ─── Blur-behind effect (Windows DWM) ───

    def _enable_blur(self):
        """Включает blur-behind эффект (Windows Acrylic / BlurBehind)."""
        self._blur_enabled = False
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import Structure, c_int, c_uint, c_size_t, POINTER, byref, sizeof

            class ACCENT_POLICY(Structure):
                _fields_ = [
                    ("AccentState", c_int),
                    ("AccentFlags", c_int),
                    ("GradientColor", c_uint),
                    ("AnimationId", c_int),
                ]

            class WINCOMPATTR_DATA(Structure):
                _fields_ = [
                    ("Attribute", c_int),
                    ("Data", POINTER(ACCENT_POLICY)),
                    ("SizeOfData", c_size_t),
                ]

            hwnd = int(self.winId())

            # Try Acrylic (4) then BlurBehind (3)
            for state in (4, 3):
                accent = ACCENT_POLICY()
                accent.AccentState = state
                accent.AccentFlags = 2  # ACCENT_FLAG_DRAW_ALL
                # ABGR: subtle dark-purple tint
                accent.GradientColor = 0x01100814

                data = WINCOMPATTR_DATA()
                data.Attribute = 19  # WCA_ACCENT_POLICY
                data.Data = ctypes.pointer(accent)
                data.SizeOfData = sizeof(accent)

                if ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, byref(data)):
                    self._blur_enabled = True
                    self.log("🔮 Blur-эффект включён")
                    return
        except Exception:
            pass

    def log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def show(self):
        super().show()
        self._fade_in()

    def _fade_in(self):
        """Плавное появление окна + включение blur."""
        self._enable_blur()
        self._fade_opacity = 0.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)  # ~60fps
        self._fade_timer.timeout.connect(self._fade_tick)
        self._fade_timer.start()

    def _fade_tick(self):
        self._fade_opacity += 0.025  # ~1.1 сек до полной непрозрачности — плавнее
        if self._fade_opacity >= 1.0:
            self._fade_opacity = 1.0
            self._fade_timer.stop()
        self.setWindowOpacity(self._fade_opacity)

    def _open_folder(self):
        import os
        os.startfile(self.mc_dir)
        self.log(f"📁 {self.mc_dir}")

    def _apply_theme(self):
        """Применяет текущую тему (цвета + фон)."""
        primary = self.settings.get("theme_primary", "#8040c8")
        secondary = self.settings.get("theme_secondary", "#40a8e8")
        self.setStyleSheet(stylesheet(self.mc_font, self.title_font, primary, secondary))
        # Custom background
        custom_bg = self.settings.get("custom_bg", "")
        if custom_bg and Path(custom_bg).exists():
            bg_path = Path(custom_bg)
        else:
            bg_path = Path(__file__).parent.parent / "assets" / "bg_amaterasu"
        # Update center GIF if needed
        if hasattr(self, 'center_gif'):
            # Recreate GifBg
            old_gif = self.center_gif
            new_gif = GifBg(bg_path)
            new_gif.setObjectName("CenterBg")
            layout = self.centralWidget().layout()
            idx = layout.indexOf(old_gif)
            layout.removeWidget(old_gif)
            old_gif.deleteLater()
            layout.insertWidget(idx, new_gif, 1)
            self.center_gif = new_gif
            # Move launch overlay to new parent
            if hasattr(self, '_launch_overlay'):
                self._launch_overlay.setParent(self.centralWidget())

    def refresh_main_versions(self):
        self.version_combo.clear()
        installed = get_installed_versions_list(self.mc_dir)
        if installed:
            for v in installed:
                vl = v.lower()
                if "optifine" in vl and ("forge" in vl or "neoforge" in vl):
                    self.version_combo.addItem("🟠  " + v)
                elif "forge" in vl or "neoforge" in vl or "ite" in vl:
                    self.version_combo.addItem("🟡  " + v)
                elif "fabric" in vl:
                    self.version_combo.addItem("🔵  " + v)
                elif "snapshot" in vl:
                    self.version_combo.addItem("🔴  " + v)
                else:
                    self.version_combo.addItem("🟢  " + v)
            self.log(f"📋 Установлено: {len(installed)}")
        else:
            self.version_combo.addItem("Нет установленных")
            self.log("⚠️ Нет установленных — скачай в Менеджере версий")

    def _on_play(self):
        version = self.version_combo.currentText().strip()
        # Strip emoji prefix from version
        for prefix in ["🟢  ", "🟡  ", "🟠  ", "🔵  ", "🔴  "]:
            if version.startswith(prefix):
                version = version[len(prefix):]
                break
        username = self.nick_edit.text().strip()
        if not version or version.startswith("Нет") or not username:
            self.log("⚠️ Заполни версию и ник")
            return

        # Close right panel INSTANTLY if open (no animation — avoid conflicts)
        if self.right_panel.isVisible():
            self._stop_panel_anims()
            self.right_panel.setVisible(False)
            self.right_panel.setFixedWidth(440)
            # Resize window to BASE_WIDTH immediately
            geo = self.geometry()
            offset = (geo.width() - self.BASE_WIDTH) // 2
            self.setGeometry(geo.x() + offset, geo.y(),
                             self.BASE_WIDTH, geo.height())

        self.log(f"📦 {version}...")
        self.play_btn.setEnabled(False)
        self.play_btn.start_loading("ЗАГРУЗКА...")
        self._launch_version = version
        self._launch_username = username

        # ─── Animate left panel out + show launch overlay ───
        self._animate_left_out()

    # ─── Left panel slide animations for launch ───

    def _animate_left_out(self):
        """Убирает левую панель влево с анимацией."""
        if self._launch_animating:
            return
        self._launch_animating = True

        # Stop particles
        self._particles.stop()

        duration = 550
        current_w = self.left_panel.width()

        # Animate minimumWidth 300 → 0
        self._anim_left_min = QPropertyAnimation(self.left_panel, b"minimumWidth")
        self._anim_left_min.setDuration(duration)
        self._anim_left_min.setStartValue(current_w)
        self._anim_left_min.setEndValue(0)
        self._anim_left_min.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Animate maximumWidth 300 → 0
        self._anim_left_max = QPropertyAnimation(self.left_panel, b"maximumWidth")
        self._anim_left_max.setDuration(duration)
        self._anim_left_max.setStartValue(current_w)
        self._anim_left_max.setEndValue(0)
        self._anim_left_max.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Resize window smaller
        geo = self.geometry()
        target_x = geo.x() + current_w
        target_geo = QRect(target_x, geo.y(), geo.width() - current_w, geo.height())
        self._anim_left_win = QPropertyAnimation(self, b"geometry")
        self._anim_left_win.setDuration(duration)
        self._anim_left_win.setStartValue(geo)
        self._anim_left_win.setEndValue(target_geo)
        self._anim_left_win.setEasingCurve(QEasingCurve.Type.OutQuint)

        self._anim_left_out_done = False
        self._anim_left_min.finished.connect(self._on_left_out_done)

        self._anim_left_min.start()
        self._anim_left_max.start()
        self._anim_left_win.start()

        # Show launch overlay with delay
        QTimer.singleShot(int(duration * 0.4), self._show_launch_overlay)

    def _show_launch_overlay(self):
        """Показывает overlay с прогрессом на всём central widget."""
        central = self.centralWidget()
        self._launch_overlay.setParent(central)
        self._launch_overlay.setGeometry(central.rect())
        self._launch_overlay.start(self._launch_version)
        # Держим title bar поверх overlay чтобы кнопки свернуть/закрыть работали
        if hasattr(self, 'title_bar'):
            self.title_bar.raise_()
        # Start install thread
        self._start_install()

    def _on_left_out_done(self):
        self._launch_animating = False
        self.left_panel.setVisible(False)

    def _animate_left_in(self):
        """Возвращает левую панель с анимацией."""
        duration = 550
        target_w = 300

        self.left_panel.setVisible(True)

        # Animate minimumWidth 0 → 300
        self._anim_left_min = QPropertyAnimation(self.left_panel, b"minimumWidth")
        self._anim_left_min.setDuration(duration)
        self._anim_left_min.setStartValue(0)
        self._anim_left_min.setEndValue(target_w)
        self._anim_left_min.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Animate maximumWidth 0 → 300
        self._anim_left_max = QPropertyAnimation(self.left_panel, b"maximumWidth")
        self._anim_left_max.setDuration(duration)
        self._anim_left_max.setStartValue(0)
        self._anim_left_max.setEndValue(target_w)
        self._anim_left_max.setEasingCurve(QEasingCurve.Type.OutQuint)

        # Resize window bigger
        geo = self.geometry()
        target_x = geo.x() - target_w
        target_geo = QRect(target_x, geo.y(), geo.width() + target_w, geo.height())
        self._anim_left_win = QPropertyAnimation(self, b"geometry")
        self._anim_left_win.setDuration(duration)
        self._anim_left_win.setStartValue(geo)
        self._anim_left_win.setEndValue(target_geo)
        self._anim_left_win.setEasingCurve(QEasingCurve.Type.OutQuint)

        self._anim_left_min.start()
        self._anim_left_max.start()
        self._anim_left_win.start()

    def _start_install(self):
        """Запускает поток установки Minecraft."""
        version = self._launch_version
        username = self._launch_username

        self.play_progress.setVisible(True)
        self.play_progress.setMaximum(0)
        self.play_progress.setValue(0)
        self.play_progress.setFormat("Подготовка...")

        self._launch_overlay.set_status("Подготовка...")

        self.inst_thread = InstallThread(version, username, self.mc_dir, self.settings, self)
        self.inst_thread.log_signal.connect(self._on_launch_log)
        self.inst_thread.progress_signal.connect(self._on_play_progress)
        self.inst_thread.finished_signal.connect(self._on_play_done)
        self.inst_thread.launched_signal.connect(self._on_mc_launched)
        self.inst_thread.error_signal.connect(self._on_play_error)
        self.inst_thread.start()

    def _on_mc_launched(self, mc_process):
        """Minecraft процесс запущен — переключаем overlay в waiting mode."""
        self._launch_overlay.mc_closed_signal.connect(self._on_mc_closed)
        self._launch_overlay.start_waiting(mc_process)
        self.log("🎮 Minecraft запущен — ожидание закрытия...")

    def _on_mc_closed(self):
        """Minecraft закрыт — возвращаем панель."""
        self.log("🎮 Minecraft закрыт — возврат интерфейса")
        self._after_launch_done()

    def _on_launch_log(self, text: str):
        """Логирует в консоль и обновляет статус overlay."""
        self.log(text)
        # Update overlay status from log messages
        if "..." in text or "📦" in text or "☕" in text or "🚀" in text:
            clean = text.replace("📦", "").replace("☕", "").replace("🚀", "").replace("✅", "").strip()
            if clean:
                self._launch_overlay.set_status(clean)

    def _on_play_progress(self, cur: int, tot: int):
        self._launch_overlay.set_progress(cur, tot)
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
        # Minecraft launched — stay on overlay with "close to return" message
        self._launch_overlay.set_status("✅ Minecraft запущен!")
        self._launch_overlay._target = 1.0
        self._launch_overlay._progress = 1.0

    def _after_launch_done(self):
        """Скрывает overlay и возвращает панель."""
        self._launch_overlay.stop()
        self._animate_left_in()

    def _on_play_error(self, msg: str):
        self.log(f"❌ {msg}")
        self.play_btn.setEnabled(True)
        self.play_btn.stop_loading("▶  ЗАПУСТИТЬ")
        self.play_progress.setVisible(False)
        self._particles.stop()
        # Fade out overlay and bring panel back
        self._launch_overlay.set_status(f"❌ Ошибка: {msg}")
        QTimer.singleShot(2000, self._after_launch_done)

    def _start_version_load(self):
        """Запускает загрузку списка версий в отдельном потоке."""
        self.ver_status.setText("⏳ Загрузка списка версий...")
        self.log("⏳ Загрузка списка версий...")
        self._show_ver_spinner()
        self._ver_thread = VersionListThread(self)
        self._ver_thread.finished.connect(self._on_versions_loaded)
        self._ver_thread.error.connect(lambda e: (
            self.ver_status.setText(f"❌ {e}"),
            self.log(f"❌ Ошибка: {e}"),
            self._hide_ver_spinner()
        ))
        self._ver_thread.start()

    def _show_ver_spinner(self):
        """Центрирует и показывает спиннер на списке версий."""
        lw = self.all_versions_list
        self.ver_spinner.setParent(lw)
        # Позиционируем после того как виджет получит размер
        QTimer.singleShot(50, self._center_ver_spinner)
        self.ver_spinner.start()

    def _center_ver_spinner(self):
        """Центрирует спиннер в списке версий."""
        lw = self.all_versions_list
        vp = lw.viewport()
        self.ver_spinner.move(
            (vp.width() - self.ver_spinner.width()) // 2,
            (vp.height() - self.ver_spinner.height()) // 2
        )
        self.ver_spinner.raise_()

    def _hide_ver_spinner(self):
        """Скрывает спиннер."""
        self.ver_spinner.stop()

    def _on_versions_loaded(self, versions: list):
        """Обработка загруженного списка версий."""
        self._hide_ver_spinner()
        self.all_versions_list.clear()
        self._all_versions_raw = []
        self._all_display_items = []

        if not versions:
            self.ver_status.setText("❌ Список пуст — проверь интернет")
            self.log("❌ Не удалось загрузить версии")
            self._hide_ver_spinner()
            return

        # Получаем список MC-версий, для которых есть OptiFine
        of_versions = {
            "1.7.2", "1.7.10", "1.8.0", "1.8.8", "1.8.9",
            "1.9.0", "1.9.2", "1.9.4", "1.10", "1.10.2",
            "1.11", "1.11.2", "1.12", "1.12.1", "1.12.2",
            "1.13", "1.13.1", "1.13.2", "1.14.2", "1.14.3", "1.14.4",
            "1.15.2", "1.16.1", "1.16.2", "1.16.3", "1.16.4", "1.16.5",
            "1.17.1", "1.18", "1.18.1", "1.18.2",
            "1.19", "1.19.1", "1.19.2", "1.19.3", "1.19.4",
            "1.20.1", "1.20.4", "1.20.6",
            "1.21", "1.21.1", "1.21.3", "1.21.4",
            "1.21.6", "1.21.7", "1.21.8", "1.21.9", "1.21.10", "1.21.11",
        }
        try:
            import urllib.request
            api_url = "https://bmclapi2.bangbang93.com/optifine/versionlist"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=8)
            data = json.loads(resp.read())
            api_versions = set()
            for item in data:
                mc_ver = item.get("mcversion", "")
                if mc_ver and not item.get("filename", "").startswith("preview_"):
                    api_versions.add(mc_ver)
            if api_versions:
                of_versions = api_versions  # API актуальнее
            self.log(f"📋 OptiFine доступен для {len(of_versions)} версий")
        except Exception:
            self.log(f"📋 OptiFine: встроенный список ({len(of_versions)} версий)")

        for v in versions:
            if v.get("type") == "release":
                self._all_versions_raw.append(v)
                vid = v["id"]
                self._all_display_items.append((vid, vid, "vanilla"))
                self._all_display_items.append((f"{vid} — Forge", vid, "forge"))
                if vid in of_versions:
                    self._all_display_items.append((f"{vid} — OptiFine", vid, "forgeoptifine"))
                self._all_display_items.append((f"{vid} — Fabric", vid, "fabric"))

        for display, _, mod_type in self._all_display_items:
            item = QListWidgetItem()
            if mod_type == "vanilla":
                item.setText("🟢  " + display)
                item.setForeground(QColor("#50e878"))
            elif mod_type == "forge":
                item.setText("🟡  " + display)
                item.setForeground(QColor("#f0b830"))
            elif mod_type == "forgeoptifine":
                item.setText("🟠  " + display)
                item.setForeground(QColor("#ff8c00"))
            elif mod_type == "fabric":
                item.setText("🔵  " + display)
                item.setForeground(QColor("#40a8f0"))
            self.all_versions_list.addItem(item)

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

            # Получаем список MC-версий с OptiFine
            of_versions = {
                "1.7.2", "1.7.10", "1.8.0", "1.8.8", "1.8.9",
                "1.9.0", "1.9.2", "1.9.4", "1.10", "1.10.2",
                "1.11", "1.11.2", "1.12", "1.12.1", "1.12.2",
                "1.13", "1.13.1", "1.13.2", "1.14.2", "1.14.3", "1.14.4",
                "1.15.2", "1.16.1", "1.16.2", "1.16.3", "1.16.4", "1.16.5",
                "1.17.1", "1.18", "1.18.1", "1.18.2",
                "1.19", "1.19.1", "1.19.2", "1.19.3", "1.19.4",
                "1.20.1", "1.20.4", "1.20.6",
                "1.21", "1.21.1", "1.21.3", "1.21.4",
                "1.21.6", "1.21.7", "1.21.8", "1.21.9", "1.21.10", "1.21.11",
            }
            try:
                import urllib.request
                api_url = "https://bmclapi2.bangbang93.com/optifine/versionlist"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=8)
                data = json.loads(resp.read())
                api_versions = set()
                for item in data:
                    mc_ver = item.get("mcversion", "")
                    if mc_ver and not item.get("filename", "").startswith("preview_"):
                        api_versions.add(mc_ver)
                if api_versions:
                    of_versions = api_versions
            except Exception:
                pass

            for v in versions:
                if v.get("type") == "release":
                    self._all_versions_raw.append(v)
                    vid = v["id"]
                    self._all_display_items.append((vid, vid, "vanilla"))
                    self._all_display_items.append((f"{vid} — Forge", vid, "forge"))
                    if vid in of_versions:
                        self._all_display_items.append((f"{vid} — OptiFine", vid, "forgeoptifine"))
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
                item = QListWidgetItem()
                if mod_type == "vanilla":
                    item.setText("🟢  " + display)
                    item.setForeground(QColor("#50e878"))
                elif mod_type == "forge":
                    item.setText("🟡  " + display)
                    item.setForeground(QColor("#f0b830"))
                elif mod_type == "forgeoptifine":
                    item.setText("🟠  " + display)
                    item.setForeground(QColor("#ff8c00"))
                elif mod_type == "fabric":
                    item.setText("🔵  " + display)
                    item.setForeground(QColor("#40a8f0"))
                self.all_versions_list.addItem(item)

    def _on_download_version(self):
        item = self.all_versions_list.currentItem()
        if not item:
            self.ver_status.setText("Выбери версию из списка")
            return

        display = item.text()

        # Strip emoji prefix
        for prefix in ["🟢  ", "🟡  ", "🟠  ", "🔵  ", "🔴  "]:
            if display.startswith(prefix):
                display = display[len(prefix):]
                break

        # Определяем тип: vanilla / forge / forgeoptifine / fabric
        if "— OptiFine" in display:
            mod_type = "forgeoptifine"
            mc_ver = display.replace(" — OptiFine", "").strip()
        elif "— Forge" in display:
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
        self.settings["ram"] = self._ram_values[self.set_ram.value()]
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

    # ─── Theme methods ───

    def _update_color_btn(self, btn: QPushButton, hex_color: str):
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {hex_color};
                border: 2px solid rgba(255,255,255,60);
                border-radius: 8px;
            }}
            QPushButton:hover {{
                border: 2px solid rgba(255,255,255,120);
            }}
        """)

    def _gradient_ss(self) -> str:
        return (f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 {self._primary_color}, stop:1 {self._secondary_color});"
                f"border-radius:6px;border:1px solid rgba(255,255,255,30);")

    def _update_gradient_preview(self):
        if hasattr(self, '_gradient_preview'):
            self._gradient_preview.setStyleSheet(
                self._gradient_ss() + "font-size:10px;color:rgba(255,255,255,160);")

    def _pick_primary_color(self):
        color = QColorDialog.getColor(QColor(self._primary_color), self, "Основной цвет")
        if color.isValid():
            self._primary_color = color.name()
            self._update_color_btn(self.btn_color_primary, self._primary_color)
            self._primary_hex.setText(self._primary_color)
            self._update_gradient_preview()
            self.settings["theme_primary"] = self._primary_color
            save_settings(self.settings)
            self._apply_theme()

    def _pick_secondary_color(self):
        color = QColorDialog.getColor(QColor(self._secondary_color), self, "Дополнительный цвет")
        if color.isValid():
            self._secondary_color = color.name()
            self._update_color_btn(self.btn_color_secondary, self._secondary_color)
            self._secondary_hex.setText(self._secondary_color)
            self._update_gradient_preview()
            self.settings["theme_secondary"] = self._secondary_color
            save_settings(self.settings)
            self._apply_theme()

    def _reset_theme(self):
        self._primary_color = "#8040c8"
        self._secondary_color = "#40a8e8"
        self._update_color_btn(self.btn_color_primary, self._primary_color)
        self._update_color_btn(self.btn_color_secondary, self._secondary_color)
        self._primary_hex.setText(self._primary_color)
        self._secondary_hex.setText(self._secondary_color)
        self._update_gradient_preview()
        self.settings["theme_primary"] = self._primary_color
        self.settings["theme_secondary"] = self._secondary_color
        save_settings(self.settings)
        self._apply_theme()
        self.log("🎨 Тема сброшена")

    def _set_background_from_file(self, file_path: str):
        """Устанавливает файл как фон лаунчера (из drag&drop или файлового диалога)."""
        assets_dir = Path(__file__).parent.parent / "assets"
        ext = Path(file_path).suffix
        dest = assets_dir / f"custom_bg{ext}"
        import shutil
        shutil.copy2(file_path, str(dest))
        self.settings["custom_bg"] = str(dest)
        save_settings(self.settings)
        if hasattr(self, '_bg_path_label'):
            self._bg_path_label.setText(f"📂 {dest.name}")
        self._apply_theme()
        self.log(f"🖼 Фон установлен: {dest.name}")

    def _pick_background(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать фон лаунчера", "",
            "Изображения (*.png *.jpg *.jpeg *.gif *.webp);;Все файлы (*)"
        )
        if file_path:
            self._set_background_from_file(file_path)

    def _reset_background(self):
        self.settings["custom_bg"] = ""
        save_settings(self.settings)
        self._bg_path_label.setText("")
        self._apply_theme()
        self.log("🖼 Фон сброшен")

    # ─── Theme share system (short codes) ───

    _B62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    @staticmethod
    def _b62_encode(num: int) -> str:
        if num == 0:
            return "A"
        s = ""
        while num > 0:
            s = MainWindow._B62[num % 62] + s
            num //= 62
        return s

    @staticmethod
    def _b62_decode(s: str) -> int:
        num = 0
        for c in s:
            num = num * 62 + MainWindow._B62.index(c)
        return num

    @staticmethod
    def _hex_to_int(h: str) -> int:
        return int(h.lstrip("#"), 16)

    @staticmethod
    def _int_to_hex(n: int) -> str:
        return f"#{n:06x}"

    def _export_theme(self):
        """Сохраняет тему в файл .amts и открывает папку."""
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "💾 Название темы",
            "Как назвать тему?",
            text="Моя тема"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        # Убираем запрещённые символы в имени файла
        safe_name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
        if not safe_name:
            safe_name = "AmaterasuTheme"
        try:
            theme_data = {
                "format": "amaterasu-theme",
                "version": 1,
                "name": name,
                "primary": self.settings.get("theme_primary", "#8040c8"),
                "secondary": self.settings.get("theme_secondary", "#40a8e8"),
            }

            # Фон
            custom_bg = self.settings.get("custom_bg", "")
            if custom_bg and Path(custom_bg).exists():
                bg_path = Path(custom_bg)
                file_size = bg_path.stat().st_size
                if file_size > 8 * 1024 * 1024:
                    self._share_status.setText("⚠️ Фон >8МБ — только цвета")
                    self.log("⚠️ Фон слишком большой, сохранены только цвета")
                else:
                    with open(bg_path, "rb") as f:
                        img_raw = f.read()
                    compressed = zlib.compress(img_raw, 9)
                    theme_data["bg_ext"] = bg_path.suffix[1:]  # gif/png/jpg
                    theme_data["bg_data"] = base64.b64encode(compressed).decode("ascii")

            # Сохраняем файл на рабочий стол
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                desktop = Path.home()
            filename = f"{safe_name}.amts"
            dest = desktop / filename

            json_out = json.dumps(theme_data, indent=2, ensure_ascii=False)
            dest.write_text(json_out, encoding="utf-8")

            self._share_status.setText(f"✅ Сохранено: {dest.name}")
            self.log(f"📤 Тема сохранена: {dest}")

            # Открыть папку с файлом
            if os.name == "nt":
                os.startfile(str(desktop))
            else:
                import subprocess as sp
                sp.Popen(["xdg-open", str(desktop)])

            # Показать инструкцию
            QMessageBox.information(
                self, "📤 Тема сохранена!",
                f"Файл сохранён на рабочий стол:\n\n"
                f"📄 {filename}\n\n"
                f"━━━ Как поделиться ━━━\n\n"
                f"1. Отправь файл {filename} другу\n"
                f"   (Discord, Telegram, email — как угодно)\n\n"
                f"2. Друг перетаскивает файл на окно лаунчера\n\n"
                f"3. Тема применяется автоматически!"
            )

        except Exception as e:
            self._share_status.setText(f"❌ {e}")
            self.log(f"❌ Ошибка: {e}")

    def _import_theme_file(self):
        """Выбирает .amts файл и применяет тему."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать файл темы", "",
            "Тема Amaterasu (*.amts);;Все файлы (*)"
        )
        if file_path:
            self._apply_theme_from_file(file_path)

    def _apply_theme_from_file(self, file_path: str):
        """Читает .amts файл и применяет тему."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                theme_data = json.loads(f.read())

            if theme_data.get("format") != "amaterasu-theme":
                self._share_status.setText("❌ Это не файл темы Amaterasu")
                self.log("❌ Неверный формат файла")
                return

            # Применяем цвета
            if "primary" in theme_data:
                self._primary_color = theme_data["primary"]
                self.settings["theme_primary"] = self._primary_color
                self._update_color_btn(self.btn_color_primary, self._primary_color)
                self._primary_hex.setText(self._primary_color)

            if "secondary" in theme_data:
                self._secondary_color = theme_data["secondary"]
                self.settings["theme_secondary"] = self._secondary_color
                self._update_color_btn(self.btn_color_secondary, self._secondary_color)
                self._secondary_hex.setText(self._secondary_color)

            self._update_gradient_preview()

            # Применяем фон
            if "bg_data" in theme_data and "bg_ext" in theme_data:
                compressed = base64.b64decode(theme_data["bg_data"])
                img_data = zlib.decompress(compressed)
                assets_dir = Path(__file__).parent.parent / "assets"
                ext = theme_data["bg_ext"]
                dest = assets_dir / f"custom_bg.{ext}"
                with open(dest, "wb") as f:
                    f.write(img_data)
                self.settings["custom_bg"] = str(dest)
                self._bg_path_label.setText(f"📂 {dest.name}")
                self.log(f"🖼 Фон: {dest.name}")

            save_settings(self.settings)
            self._apply_theme()

            fname = Path(file_path).name
            has_bg = "bg_data" in theme_data
            self._share_status.setText(f"✅ Тема применена из {fname}")
            self.log(f"📥 Тема применена из файла {fname}")

            QMessageBox.information(
                self, "📥 Тема применена!",
                f"Файл: {fname}\n\n"
                f"🎨 Основной: {self._primary_color}\n"
                f"🎨 Дополнительный: {self._secondary_color}\n"
                f"🖼 Фон: {'✅ установлен' if has_bg else '❌ нет'}"
            )

        except json.JSONDecodeError:
            self._share_status.setText("❌ Файл повреждён")
            self.log("❌ Файл темы повреждён")
        except Exception as e:
            self._share_status.setText(f"❌ {e}")
            self.log(f"❌ Ошибка: {e}")
            self.log(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
