@echo off
setlocal
cd /d "%~dp0"

echo Checking for Python...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python was not found on PATH.
    echo Install it from https://www.python.org/downloads/ ^(Python 3.11 or newer^)
    echo and make sure "Add python.exe to PATH" is checked during install.
    echo Then close this window and run setup.bat again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create the virtual environment - see the error above.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo Installing dependencies ^(this can take a minute^)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install dependencies - see the error above.
    pause
    exit /b 1
)

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created .env from the template.
    echo Open .env in Notepad, fill in your Twitch app details, then run run.bat.
) else (
    echo.
    echo .env already exists - leaving it as-is.
)

echo.
echo Setup finished. See README.md for what to fill in and how to start the bot.
pause
