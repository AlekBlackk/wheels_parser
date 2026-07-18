@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.10 или новее: https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist ".env" (
    echo [!] Файл .env не найден. Скопируйте .env.example в .env и заполните токен и chat ID.
)

echo Установка зависимостей...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости.
    pause
    exit /b 1
)

echo Установка браузера...
playwright install chromium
if errorlevel 1 (
    echo Не удалось установить браузер для Playwright.
    pause
    exit /b 1
)

echo Запуск WheelsParser...
python betboom_web_parser.py
pause
