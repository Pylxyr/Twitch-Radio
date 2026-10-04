@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo No .venv folder found - run setup.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python bot.py

echo.
echo The bot has stopped. If that wasn't expected, scroll up for the error.
pause
