@echo off
chcp 65001 >nul
rem Установка mcp-1c-its через системный Python (3.10+).
rem Для установки без Python используйте uv - см. README.md.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден. Установите Python 3.10 или новее
    echo с python.org ^(отметьте "Add python.exe to PATH"^) или используйте uv - см. README.md.
    pause
    exit /b 1
)

echo Установка пакета...
python -m pip install -q --disable-pip-version-check .
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить пакет.
    pause
    exit /b 1
)

if not exist "%USERPROFILE%\.1c-its\its_credentials.txt" (
    mkdir "%USERPROFILE%\.1c-its" 2>nul
    copy /y its_credentials.example.txt "%USERPROFILE%\.1c-its\its_credentials.txt" >nul
    echo Создан %%USERPROFILE%%\.1c-its\its_credentials.txt — впишите ITS_USER и ITS_PASS.
)

echo.
echo Готово. Дальше:
echo   1. Заполните %%USERPROFILE%%\.1c-its\its_credentials.txt
echo      (логин и пароль ИТС, по строке на значение).
echo   2. Добавьте сервер в клиент:
echo        command: python
echo        args:    ["-m", "mcp_1c_its.server"]
echo   3. Перезапустите клиент. Проверка:  python -m mcp_1c_its.check
pause
