# Установщик Amaterasu Launcher (в стиле TLauncher)

Готовый мастер установки: **Приветствие → Лицензия → Готово → Установка (прогресс) → Завершение**.

Что делает установщик:
- ✅ Скачивает файлы лаунчера с GitHub Releases (или копирует из локального zip)
- ✅ Устанавливает лаунчер в `%LOCALAPPDATA%\Amaterasu`
- ✅ Создаёт ярлык на рабочем столе и в меню «Пуск»
- ✅ Папка данных игры — `%APPDATA%\.amaterasu` (а не `.minecraft`) ✔️ как ты просил
- ✅ Создаёт `uninstall.bat` для удаления
- ✅ Опция «Запустить лаунчер сейчас» в конце

---

## Файлы

| Файл | Назначение |
|------|-----------|
| `installer.py` | Сам мастер установки (GUI на PyQt6) |
| `build_launcher.py` | Собирает **лаунчер** в `.exe` + `Amaterasu.zip` |
| `build_installer.py` | Собирает **установщик** в `AmaterasuSetup.exe` |

---

## Как собрать (на Windows)

> Всё делается на компьютере с Windows и установленным Python 3.10+.
> В песочнице/Linux собрать Windows-.exe нельзя — нужен именно Windows.

### Шаг 0. Установить зависимости
```bat
cd my_mc_launcher
pip install -r requirements.txt
pip install pyinstaller pillow
```

### Шаг 1. Собрать лаунчер
```bat
python build_launcher.py
```
Получишь:
- `dist\Amaterasu\Amaterasu.exe` — сам лаунчер
- `Amaterasu.zip` — архив для публикации

### Шаг 2. Опубликовать лаунчер на GitHub
1. Зайди в свой репозиторий → **Releases** → **Draft a new release**
2. Тег: `latest` (или любой; см. ниже про ссылку)
3. Перетащи `Amaterasu.zip` в **Attach binaries**
4. **Publish release**

Прямая ссылка на ассет будет такой:
```
https://github.com/xploitdota-arch/123/releases/latest/download/Amaterasu.zip
```
Она уже прописана в `installer.py` (переменная `GITHUB_ZIP_URL`). Если у тебя
другой репозиторий/имя — поправь её.

### Шаг 3. Собрать установщик
```bat
python build_installer.py
```
Получишь `dist\AmaterasuSetup.exe` — **это и есть файл, который раздаёшь людям.**
При запуске он скачает `Amaterasu.zip` с GitHub и установит лаунчер.

---

## Вариант «всё в одном файле» (оффлайн)

Если не хочешь зависеть от интернета/Releases — вшей лаунчер прямо в установщик:

```bat
python build_launcher.py          REM создаст Amaterasu.zip
python build_installer.py --offline
```
Тогда `AmaterasuSetup.exe` будет самодостаточным (большой, но без скачивания).

---

## Быстрый тест без сборки .exe

Можно проверить мастер прямо через Python (нужен установленный PyQt6):
```bat
python installer.py
```
Чтобы при тесте он не лез в интернет — положи готовый `Amaterasu.zip`
рядом с `installer.py`: установщик возьмёт локальный файл автоматически.

---

## Что настроить под себя (в `installer.py`)

```python
APP_NAME       = "Amaterasu"          # имя приложения/папок/ярлыков
APP_VERSION    = "1.0.0"
GITHUB_ZIP_URL = "https://github.com/.../releases/latest/download/Amaterasu.zip"
LAUNCHER_EXE   = "Amaterasu.exe"      # имя exe лаунчера внутри архива
```

---

## Важно про .exe лаунчера

Раньше старый `installer.py` создавал ярлык на `pythonw main.py`. Это работает
только если у пользователя установлен Python. Чтобы было **как у TLauncher**
(пользователю Python не нужен), лаунчер компилируется в `Amaterasu.exe`
(шаг 1). Установщик создаёт ярлык именно на него.

Если же ты хочешь оставить запуск через Python (для своих), установщик это
тоже поддержит: если в архиве нет `Amaterasu.exe`, но есть `main.py`, ярлык
будет создан на `pythonw main.py`.
