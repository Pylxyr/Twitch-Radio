#!/usr/bin/env bash
# Builds the Linux AppImage and tarball (x64). Mirrors build-windows.bat.
# Needs: python 3.11+, node 22+ (with npm), and a C toolchain to build ffmpeg (about 5 minutes the
# first time): on Arch / CachyOS `sudo pacman -S python nodejs npm base-devel nasm cmake openssl curl`,
# on Debian / Ubuntu `sudo apt install build-essential nasm cmake pkg-config libssl-dev curl python3-venv`.
# Output: dist/app/
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "=== 1/4  Python environment ==="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r requirements-build.lock
.venv/bin/python -c "import yt_dlp.version as v; print('[ok] yt-dlp', v.__version__)"

echo "=== 2/4  ffmpeg (small build from pinned, checksum-verified source) ==="
if [ -x packaging/bin/ffmpeg ]; then
  echo "[ok] packaging/bin/ffmpeg already exists (delete packaging/bin to rebuild it)"
else
  ./packaging/ffmpeg/build.sh linux packaging/bin
fi
.venv/bin/python scripts/check_ffmpeg.py packaging/bin/ffmpeg

echo "=== 3/4  Bot core (PyInstaller) ==="
rm -rf dist/core
.venv/bin/pyinstaller packaging/core.spec --noconfirm --distpath dist/core --workpath build/pyinstaller
test -x dist/core/TwitchRadioCore/TwitchRadioCore

echo "=== 4/4  Desktop app (Electron) ==="
(cd gui && npm ci && npm run dist:linux)

echo "Done. AppImage and tarball are in dist/app/"
