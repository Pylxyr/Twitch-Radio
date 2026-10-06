#!/usr/bin/env bash
# Builds the small ffmpeg that Twitch Radio ships, from pinned, checksum-verified source.
#
#   packaging/ffmpeg/build.sh linux   <out-dir>   # native build on Linux
#   packaging/ffmpeg/build.sh windows <out-dir>   # cross-compile on Linux with mingw-w64
#
# Needs (Debian/Ubuntu): build-essential nasm pkg-config cmake make curl xz-utils python3,
#   plus  libssl-dev            for the Linux build (static OpenSSL is linked in)
#   plus  mingw-w64             for the Windows build (TLS comes from Windows itself: Schannel)
#
# Why not a prebuilt ffmpeg: the general-purpose builds are 80-105 MB because they carry every
# codec, filter and library. The app only decodes YouTube audio (untouched: no volume or loudness
# processing) and encodes it as Opus in Ogg, so this builds just that - under 10 MB - and nothing GPL.
# The result is LGPL (v3 on Linux because OpenSSL 3 is Apache-2.0, v2.1 on Windows), and ships as
# a separate ffmpeg executable the app starts, so there is nothing to relink. The exact sources are
# the tarballs pinned in packaging/tools.lock.json; this script and its configure flags are the
# source offer for anything built from them.
#
# What the app needs from ffmpeg is spelled out in scripts/check_ffmpeg.py, which fails the build
# when a piece goes missing. Add a component here and there together.
set -euo pipefail

target="${1:-}"; out="${2:-}"
case "$target" in linux|windows) ;; *) echo "usage: $0 <linux|windows> <out-dir>" >&2; exit 2 ;; esac
[ -n "$out" ] || { echo "usage: $0 <linux|windows> <out-dir>" >&2; exit 2; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="$here/../tools.lock.json"
cache="${FFMPEG_CACHE:-$here/../.cache}"
work="${FFMPEG_WORK:-$here/../../build/ffmpeg-$target}"
jobs="${JOBS:-$(nproc)}"
mkdir -p "$cache" "$work" "$out"
out="$(cd "$out" && pwd)"; work="$(cd "$work" && pwd)"; cache="$(cd "$cache" && pwd)"

field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ffmpeg"][sys.argv[2]])' "$lock" "$1"; }

fetch() { # url sha256 file -> verified path on stdout
  local url="$1" sha="$2" file="$cache/$3"
  [ -f "$file" ] || { echo "Downloading $url" >&2; curl -fsSL --retry 3 --retry-delay 2 -o "$file" "$url"; }
  local actual; actual="$(sha256sum "$file" | cut -d' ' -f1)"
  if [ "$actual" != "$sha" ]; then
    rm -f "$file"; echo "SHA-256 mismatch for $3: expected $sha, got $actual. The file was deleted." >&2; exit 1
  fi
  echo "SHA-256 OK: $3" >&2; printf '%s\n' "$file"
}

ff_ver="$(field version)"; opus_ver="$(field opus_version)"
ff_tar="$(fetch "$(field source_url)" "$(field source_sha256)" "ffmpeg-$ff_ver.tar.gz")"
opus_tar="$(fetch "$(field opus_url)" "$(field opus_sha256)" "opus-$opus_ver.tar.gz")"

rm -rf "$work/src" "$work/prefix" "$work/staticlibs"; mkdir -p "$work/src" "$work/prefix"
# The sources are unpacked inside this repository's checkout: without a ceiling git would walk up into
# it and ffmpeg would stamp the binary with this repo's tag ("ffmpeg version v1.0.2").
export GIT_CEILING_DIRECTORIES="$work"
tar -xzf "$ff_tar" -C "$work/src"; tar -xzf "$opus_tar" -C "$work/src"
ffsrc="$(echo "$work"/src/FFmpeg-*)"; opsrc="$(echo "$work"/src/opus-*)"
prefix="$work/prefix"

# ---- libopus (static) ----------------------------------------------------------------------
cmake_args=(-S "$opsrc" -B "$work/opus-build" -G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_INSTALL_PREFIX="$prefix" -DBUILD_SHARED_LIBS=OFF -DOPUS_BUILD_PROGRAMS=OFF
  -DOPUS_BUILD_TESTING=OFF -DOPUS_INSTALL_PKG_CONFIG_MODULE=ON -DOPUS_INSTALL_CMAKE_CONFIG_MODULE=OFF
  -DCMAKE_POSITION_INDEPENDENT_CODE=OFF -DOPUS_STACK_PROTECTOR=OFF -DCMAKE_INSTALL_LIBDIR=lib)
# The tag archive has no generated package_version file; opus.pc's version comes from it.
printf 'PACKAGE_VERSION="%s"\n' "$opus_ver" > "$opsrc/package_version"
if [ "$target" = windows ]; then
  cmake_args+=(-DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc
    -DCMAKE_RC_COMPILER=x86_64-w64-mingw32-windres -DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER)
fi
cmake "${cmake_args[@]}" >"$work/opus-cmake.log" 2>&1 || { tail -30 "$work/opus-cmake.log"; exit 1; }
cmake --build "$work/opus-build" -j "$jobs" >"$work/opus-make.log" 2>&1 || { tail -30 "$work/opus-make.log"; exit 1; }
cmake --install "$work/opus-build" >/dev/null

# ---- ffmpeg ----------------------------------------------------------------------------------
# Everything is off, then exactly what the app uses is switched on (see scripts/check_ffmpeg.py).
common=(
  --disable-everything --disable-autodetect --disable-doc --disable-debug
  --disable-ffplay --disable-ffprobe --disable-avdevice --disable-swscale
  --enable-ffmpeg --enable-static --disable-shared --enable-libopus
  # Where it reads from: YouTube's stream URLs (https, plain http for tests, HLS playlists with
  # optional AES-128 segments), and local files / pipes.
  --enable-protocol=file,pipe,fd,http,https,httpproxy,tcp,tls,crypto,subfile
  # What YouTube's audio comes in: WebM/Opus and Vorbis (matroska), M4A/AAC (mov), HLS (hls + mpegts),
  # plus plain ogg, mp3, adts aac, flac and wav so other sources and test files work.
  --enable-demuxer=matroska,mov,hls,mpegts,ogg,mp3,aac,flac,wav,pcm_s16le
  --enable-decoder=opus,vorbis,aac,mp3float,flac,pcm_s16le
  --enable-parser=opus,vorbis,aac,aac_latm,mpegaudio,flac
  # What it writes: raw PCM to the pipe (the muxer ffmpeg calls "s16le" is built as pcm_s16le) and
  # Opus in Ogg to OBS.
  --enable-muxer=ogg,pcm_s16le
  --enable-encoder=libopus,pcm_s16le
  # Audio filters: only the plumbing ffmpeg inserts on its own to convert whatever the song is
  # (sample rate, sample format, channel layout) to 48 kHz 16-bit stereo. Songs are not otherwise touched.
  --enable-filter=aresample,aformat,anull,abuffer,abuffersink
)
case "$target" in
  linux)
    # Static OpenSSL (the distro's own libssl.a): put only the .a files first in the library
    # search path, otherwise the linker prefers libssl.so and the binary would need it installed.
    mkdir -p "$work/staticlibs"
    for lib in libssl.a libcrypto.a; do
      p="$(gcc -print-file-name=$lib)"; [ -f "$p" ] || p="$(find /usr/lib /usr/lib64 /usr/local/lib -name "$lib" 2>/dev/null | head -n1)"
      [ -f "$p" ] || { echo "$lib not found: install libssl-dev" >&2; exit 1; }
      ln -sf "$p" "$work/staticlibs/$lib"
    done
    platform=(--enable-openssl --enable-version3 --extra-ldflags="-L$work/staticlibs"
              --pkg-config-flags=--static)
    ;;
  windows)
    platform=(--arch=x86_64 --target-os=mingw32 --cross-prefix=x86_64-w64-mingw32- --pkg-config=pkg-config
              --enable-schannel --extra-ldflags=-static --pkg-config-flags=--static)
    ;;
esac

cd "$ffsrc"
if [ "$target" = windows ]; then
  export PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig"   # only what we built: never the host's libraries
else
  export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"     # ours first, then the system's OpenSSL
fi
./configure --prefix="$work/ffmpeg-install" --extra-cflags="-I$prefix/include" \
  --extra-ldflags="-L$prefix/lib" "${common[@]}" "${platform[@]}" >"$work/ffmpeg-configure.log" 2>&1 \
  || { tail -40 "$work/ffmpeg-configure.log"; tail -40 ffbuild/config.log; exit 1; }
make -j "$jobs" >"$work/ffmpeg-make.log" 2>&1 || { tail -40 "$work/ffmpeg-make.log"; exit 1; }

exe=ffmpeg; [ "$target" = windows ] && exe=ffmpeg.exe
install -m 0755 "$exe" "$out/$exe"
if [ "$target" = windows ]; then x86_64-w64-mingw32-strip --strip-all "$out/$exe"; else strip --strip-all "$out/$exe"; fi

# Licence texts that must travel with the binary.
{
  echo "FFmpeg $ff_ver as built by packaging/ffmpeg/build.sh ($target) is licensed under the"
  if [ "$target" = linux ]; then echo "GNU Lesser General Public License v3.0 or later."; else echo "GNU Lesser General Public License v2.1 or later."; fi
  echo "Source: https://ffmpeg.org/ ($(field source_url))"
  echo "Configuration: run \"ffmpeg -version\"; this folder's build script is the full recipe."
  echo; echo "It includes libopus $opus_ver (BSD-3-Clause), whose licence follows."
  [ "$target" = linux ] && echo "On Linux it also includes OpenSSL (Apache-2.0, https://www.openssl.org/source/license.html)."
  echo; echo "================ LGPL ================"
  if [ "$target" = linux ]; then cat COPYING.LGPLv3; else cat COPYING.LGPLv2.1; fi
  echo; echo "================ libopus (BSD-3-Clause) ================"; cat "$opsrc/COPYING"
} > "$out/FFMPEG-LICENSE.txt"

echo "Built $out/$exe ($(du -h "$out/$exe" | cut -f1))"
"$out/$exe" -hide_banner -version 2>/dev/null | head -n2 || true
