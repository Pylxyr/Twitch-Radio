@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem Builds the installer: dist\app\Twitch Radio Setup x.y.z.exe
rem Needs: Python 3.11+, Node.js 22+ (npm). ffmpeg is reused from PATH if present (so it is NOT the pinned version), else the pinned one is downloaded and checksum-verified. See BUILD.md.

set "BIN=%~dp0packaging\bin"

echo.
echo === Checking prerequisites ===

where python >nul 2>nul || (echo [missing] Python was not found on PATH. Install 3.11 or newer from https://www.python.org/downloads/ & goto :fail)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || (echo [problem] Python 3.11 or newer is required. If python is the Microsoft Store shortcut, install the real one. & goto :fail)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo [ok] %%v

where npm >nul 2>nul || (echo [missing] Node.js/npm was not found on PATH. Install Node.js LTS from https://nodejs.org & goto :fail)
node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)" >nul 2>nul || (echo [problem] Node.js 22 or newer is required. & goto :fail)
for /f "delims=" %%v in ('node --version 2^>^&1') do echo [ok] Node.js %%v

echo.
echo === 1/4  Python environment ===
if exist ".venv\Scripts\python.exe" (echo [ok] .venv already exists) else (python -m venv .venv || goto :fail)
call .venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check -r requirements-build.txt || goto :fail
python -c "import yt_dlp.version as v; print('[ok] yt-dlp', v.__version__)" || goto :fail

echo.
echo === 2/4  ffmpeg ===
if not exist "%BIN%" mkdir "%BIN%"
rem Older builds put deno.exe here; anything in bin\ is bundled, so drop the leftover.
if exist "%BIN%\deno.exe" del /q "%BIN%\deno.exe"
call :reuse_tool ffmpeg -version
if exist "%BIN%\ffmpeg.exe" goto :tools_ready
echo Downloading ffmpeg...
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch-tools.ps1 || goto :fail
:tools_ready

echo.
echo === 3/4  Bot core (PyInstaller) ===
if exist dist\core rmdir /s /q dist\core
pyinstaller packaging\core.spec --noconfirm --distpath dist\core --workpath build\pyinstaller || goto :fail
if not exist dist\core\TwitchRadioCore\TwitchRadioCore.exe (echo Core build did not produce TwitchRadioCore.exe & goto :fail)

echo.
echo === 4/4  Desktop app (Electron) ===
pushd gui
if exist node_modules\electron\package.json if exist node_modules\electron-builder\package.json (echo [ok] node_modules already installed & goto :npm_ready)
call npm ci || (popd & goto :fail)
:npm_ready
call npm run dist || (popd & goto :fail)
popd

echo.
echo Done. Installer is in dist\app\
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1

:reuse_tool
set "FOUND="
if exist "%BIN%\%~1.exe" goto :rt_have
for /f "delims=" %%p in ('where %~1 2^>nul') do if not defined FOUND set "FOUND=%%p"
if not defined FOUND goto :rt_none
if /i not "%FOUND:~-4%"==".exe" goto :rt_none
"%FOUND%" %~2 >nul 2>nul || goto :rt_none
for %%f in ("%FOUND%") do set "FDIR=%%~dpf"
if /i "%~1"=="ffmpeg" dir /b "%FDIR%avcodec*.dll" >nul 2>nul && goto :rt_shared
copy /y "%FOUND%" "%BIN%\%~1.exe" >nul 2>nul || goto :rt_none
"%BIN%\%~1.exe" %~2 >nul 2>nul || goto :rt_bad
if /i "%~1"=="ffmpeg" "%BIN%\ffmpeg.exe" -hide_banner -encoders 2>nul | findstr /c:libopus >nul || goto :rt_bad
echo [ok] %~1 found on PATH, copied from %FOUND%
exit /b 0

:rt_have
echo [ok] %~1 already in packaging\bin
exit /b 0

:rt_shared
echo [skip] %~1 on PATH is a shared build that needs separate DLLs - will download a standalone one
exit /b 0

:rt_bad
del "%BIN%\%~1.exe" >nul 2>nul
echo [skip] %~1 on PATH failed the check when copied - will download
exit /b 0

:rt_none
echo [missing] %~1 not usable from PATH - will download
exit /b 0
