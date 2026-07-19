@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer:
    echo https://www.python.org/downloads/
    goto :error
)

if not exist ".env" (
    echo [!] .env was not found. Copy .env.example to .env and configure the token and chat ID.
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        goto :error
    )

    echo Installing dependencies into .venv...
    "%VENV_PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        goto :error
    )
)

if not exist "%VENV_PYTHON%" (
    echo Virtual environment Python was not found: %VENV_PYTHON%
    echo Delete the .venv folder and run this script again.
    goto :error
)

echo Starting WheelsParser from the virtual environment...
"%VENV_PYTHON%" betboom_web_parser.py
if errorlevel 1 (
    echo.
    echo Parser exited with an error.
    goto :error
)

echo.
echo Parser finished.
pause
exit /b 0

:error
echo.
echo Press any key to close this window...
pause >nul
exit /b 1
