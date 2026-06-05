"""
Mirror/patch module.
- Пропускает скачивание JVM Runtime Mojang (используем Adoptium)
- Увеличивает таймауты для медленных соединений
"""

import urllib.request
import urllib.error

_enabled = False
_original_urlopen = urllib.request.urlopen


def _patched_urlopen(url, *args, **kwargs):
    """urlopen с увеличенным таймаутом по умолчанию."""
    if 'timeout' not in kwargs and len(args) < 2:
        kwargs['timeout'] = 60  # уменьшено с 120, чтобы не висело так долго на медленном соединении
    return _original_urlopen(url, *args, **kwargs)


# ─── requests patch ───

_original_requests_session_request = None


def _patch_requests():
    global _original_requests_session_request
    try:
        import requests
    except ImportError:
        return

    _original_requests_session_request = requests.Session.request

    def _patched(self, method, url, **kwargs):
        if 'timeout' not in kwargs:
            kwargs['timeout'] = 60  # уменьшено
        return _original_requests_session_request(self, method, url, **kwargs)

    requests.Session.request = _patched


# ─── Skip JVM Runtime ───

_original_install_jvm_runtime = None


def _jvm_stub(*args, **kwargs):
    print("[mirror] Пропуск JVM Runtime Mojang (используется Adoptium Java)")


def _skip_jvm_runtime_patch():
    global _original_install_jvm_runtime
    try:
        import minecraft_launcher_lib.install as inst
        if hasattr(inst, 'install_jvm_runtime'):
            _original_install_jvm_runtime = inst.install_jvm_runtime
            inst.install_jvm_runtime = _jvm_stub
        import minecraft_launcher_lib.runtime as rt
        if hasattr(rt, 'install_jvm_runtime'):
            rt.install_jvm_runtime = _jvm_stub
    except Exception:
        pass


def enable():
    global _enabled
    if not _enabled:
        urllib.request.urlopen = _patched_urlopen
        _patch_requests()
        _skip_jvm_runtime_patch()
        _enabled = True
        print("[launcher] Таймауты увеличены, JVM Runtime пропущен")


def disable():
    global _enabled
    if _enabled:
        urllib.request.urlopen = _original_urlopen
        if _original_requests_session_request:
            try:
                import requests
                requests.Session.request = _original_requests_session_request
            except ImportError:
                pass
        if _original_install_jvm_runtime:
            try:
                import minecraft_launcher_lib.install as inst
                inst.install_jvm_runtime = _original_install_jvm_runtime
            except ImportError:
                pass
        _enabled = False
