@echo off
setlocal
cd /d "%~dp0"
rem Developer shortcut: runs the desktop app against your source checkout
rem (uses .venv and your existing .env/data). Needs Node.js and `npm install` once in gui\.
where npm >nul 2>nul || (echo Node.js/npm was not found on PATH. & pause & exit /b 1)
pushd gui
if not exist node_modules call npm install
call npm start
popd
