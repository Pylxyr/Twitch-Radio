#!/usr/bin/env bash
# Builds the Linux AppImage and tarball (x64). Mirrors build-windows.bat.
# Needs: python 3.11+, node 22+ (with npm), curl, and on Arch / CachyOS
# `sudo pacman -S python nodejs npm curl`. Output: dist/app/
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "=== 1/4  Python environment ==="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-build.txt
.venv/bin/python -c "import yt_dlp.version as v; print('[ok] yt-dlp', v.__version__)"

echo "=== 2/4  ffmpeg (pinned, checksum-verified static build) ==="
./packaging/fetch-tools.sh

echo "=== 3/4  Bot core (PyInstaller) ==="
rm -rf dist/core
.venv/bin/pyinstaller packaging/core.spec --noconfirm --distpath dist/core --workpath build/pyinstaller
test -x dist/core/TwitchRadioCore/TwitchRadioCore

echo "=== 4/4  Desktop app (Electron) ==="
(cd gui && npm ci && npm run dist:linux)

echo "Done. AppImage and tarball are in dist/app/"
