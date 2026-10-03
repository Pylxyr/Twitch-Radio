#!/usr/bin/env bash
# Linux counterpart of fetch-tools.ps1: downloads a static ffmpeg into
# packaging/bin, from where the AppImage / tarball bundles it. (No JavaScript
# runtime is fetched: the app uses Electron's own, see twitch_radio/config.py.) Versions, URLs
# and SHA-256 checksums come from the "linux" section of packaging/tools.lock.json;
# a download that doesn't match is deleted and the script fails.
# Always fetches the pinned static builds: a system ffmpeg is dynamically linked
# and would not run on another distro. Use --force to fetch again.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$here/bin"
cache="$here/.cache"
lock="$here/tools.lock.json"
mkdir -p "$bin" "$cache"

if [ "${1:-}" = "--force" ]; then rm -f "$bin/ffmpeg"; fi
# Older builds put deno here; anything in bin/ is bundled, so drop the leftover.
rm -f "$bin/deno"

field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["linux"][sys.argv[2]][sys.argv[3]])' "$lock" "$1" "$2"; }

fetch() { # url sha256 -> prints the verified file path
  local url="$1" sha="$2" file="$cache/$(basename "$1")"
  if [ ! -f "$file" ]; then
    echo "Downloading $url" >&2
    curl -fL --retry 3 --retry-delay 2 -o "$file" "$url"
  fi
  local actual
  actual="$(sha256sum "$file" | cut -d' ' -f1)"
  if [ "$actual" != "$sha" ]; then
    rm -f "$file"
    echo "SHA-256 mismatch for $(basename "$file"): expected $sha, got $actual. The file was deleted." >&2
    exit 1
  fi
  echo "SHA-256 OK: $(basename "$file")" >&2
  printf '%s\n' "$file"
}

if [ ! -x "$bin/ffmpeg" ]; then
  gz="$(fetch "$(field ffmpeg url)" "$(field ffmpeg sha256)")"
  gunzip -c "$gz" > "$bin/ffmpeg"
  chmod +x "$bin/ffmpeg"
  lic="$(fetch "$(field ffmpeg license_url)" "$(field ffmpeg license_sha256)")"
  cp "$lic" "$bin/FFMPEG-LICENSE.txt"
fi

ffmpeg_line="$("$bin/ffmpeg" -hide_banner -version | head -n1)"
echo "$ffmpeg_line"
case "$ffmpeg_line" in *"$(field ffmpeg version)"*) ;; *) echo "warning: ffmpeg in packaging/bin is not the pinned $(field ffmpeg version)" >&2 ;; esac
echo "Tools ready in $bin"
