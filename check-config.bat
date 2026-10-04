@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo No .venv folder found - run setup.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python bot.py --check-config
echo.
pause
