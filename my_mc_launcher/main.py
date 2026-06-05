import sys
import traceback
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

if __name__ == "__main__":
    # ─── Патчи: увеличенные таймауты + пропуск JVM Runtime ───
    from launcher.mirror import enable as enable_mirror
    enable_mirror()

    # Windows AppUserModelID — вызываем ПЕРЕД QApplication
    # Нужно, чтобы при наведении на ярлык в панели задач показывалось "Amaterasu", а не "python"
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("amaterasu.launcher.1.0")
    except Exception:
        pass

    app = QApplication(sys.argv)

    # Название приложения — чтобы при наведении на ярлык в панели задач было "Amaterasu", а не "python"
    app.setApplicationName("Amaterasu")
    app.setApplicationDisplayName("Amaterasu")
    app.setOrganizationName("Amaterasu Launcher")

    # Иконка для панели задач
    icon_path = Path(__file__).parent / "assets" / "icon_amaterasu.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # ─── Splash Screen → Main Window ───
    from gui.splash import SplashScreen

    main_window = None

    def show_main():
        global main_window
        try:
            from gui.main_window import MainWindow
            main_window = MainWindow()
            main_window.show()
        except Exception as e:
            print(f"ОШИБКА при запуске главного окна:")
            traceback.print_exc()
            # Запускаем без splash
            sys.exit(1)

    # Попробуем сначала импортировать, чтобы ошибки вылезли сразу
    try:
        from gui.main_window import MainWindow
    except Exception as e:
        print(f"ОШИБКА импорта: {e}")
        traceback.print_exc()
        sys.exit(1)

    splash = SplashScreen(on_finished=show_main)
    splash.show()

    sys.exit(app.exec())
