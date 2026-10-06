#!/usr/bin/env python3
"""Does this ffmpeg do everything Twitch Radio asks of it?

    python scripts/check_ffmpeg.py packaging/bin/ffmpeg            # or ffmpeg.exe
    python scripts/check_ffmpeg.py <ffmpeg> --https-url https://.../tone.webm

The release ships a small ffmpeg built by packaging/ffmpeg/build.sh with only the parts below
switched on. This is the other half of that contract: it fails when a part is missing, and it
runs the app's own command lines (twitch_radio.player._decoder_cmd / _encoder_cmd) against small sample files in every container YouTube hands out - served over
local HTTP like the CDN would, including HLS with video and AES-128 encryption - and over local
HTTPS when `trustme` is installed. When you add a codec, filter or protocol to the app, add it
to REQUIRED here and to build.sh in the same change.

Exit status 0 = all good, 1 = something is missing or broken (every problem is printed).
"""

from __future__ import annotations

import argparse
import array
import http.server
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures" / "audio"

# What the app uses, by kind. Names are ffmpeg's own (see `ffmpeg -decoders` etc.).
REQUIRED: dict[str, set[str]] = {
    "decoders": {"opus", "vorbis", "aac", "mp3float", "flac", "pcm_s16le"},
    "encoders": {"libopus", "pcm_s16le"},
    # webm = matroska, m4a = mov, HLS = hls + mpegts; the rest cover other sources and test files.
    "demuxers": {"matroska", "mov", "hls", "mpegts", "ogg", "mp3", "aac", "flac", "wav", "s16le"},
    "muxers": {"ogg", "s16le"},
    "filters": {"aresample", "aformat"},  # the converters ffmpeg inserts; songs are not otherwise processed
    "protocols": {"file", "pipe", "fd", "http", "https", "tcp", "tls", "crypto", "subfile"},
}

FAILS: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"[{'ok' if ok else 'FAIL'}] {what}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(what)
    return ok


def run(
    cmd: list[str], data: bytes | None = None, timeout: float = 90, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        env={**os.environ, **env} if env else None,
        input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


# ---- a CDN stand-in: HTTP(S) with Range support ---------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    root = FIXTURES

    def log_message(self, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _serve(self, send_body: bool) -> None:
        path = (self.root / self.path.split("?")[0].lstrip("/")).resolve()
        if not path.is_file() or self.root.resolve() not in path.parents:
            self.send_error(404)
            return
        data = path.read_bytes()
        start, end, status = 0, len(data) - 1, 200
        match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:  # suffix range: the last N bytes
                start = max(0, len(data) - int(match.group(2)))
            end = min(end, len(data) - 1)
            if start > end:
                self.send_error(416)
                return
            status = 206
        chunk = data[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        if send_body:
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # ffmpeg closed early (it had what it needed)


@contextmanager
def serve(tls: ssl.SSLContext | None = None) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ---- helpers on decoded audio ----------------------------------------------------------------


def pcm_stats(pcm: bytes, rate: int = 48000, channels: int = 2) -> tuple[float, float]:
    """(seconds, RMS on a 0..32768 scale) of 16-bit little-endian PCM."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0, 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return len(samples) / channels / rate, rms


def decode(
    ffmpeg_cmd: list[str],
    real_time: bool = False,
    before_input: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> tuple[int, float, float, str]:
    """Runs the app's decoder command; returns (exit code, seconds, rms, stderr)."""
    cmd = list(ffmpeg_cmd)
    if not real_time and "-re" in cmd:
        cmd.remove(
            "-re"
        )  # a 3 s sample would otherwise take 3 s to read; -re is core fftools, not a component
    if before_input:
        index = cmd.index("-i")
        cmd[index:index] = before_input
    # Same command as the app, but with errors visible instead of "fatal".
    cmd[cmd.index("-loglevel") + 1] = "error"
    proc = run(cmd, env=env)
    seconds, rms = pcm_stats(proc.stdout)
    return proc.returncode, seconds, rms, proc.stderr.decode("utf-8", "replace").strip()


def listing(ffmpeg: str, kind: str) -> set[str]:
    """Component names from `ffmpeg -decoders|-encoders|-demuxers|-muxers|-filters|-protocols`."""
    text = run([ffmpeg, "-hide_banner", f"-{kind}"]).stdout.decode("utf-8", "replace")
    names: set[str] = set()
    started = False
    for line in text.splitlines():
        if kind == "protocols":
            token = line.strip()
            if token and not token.endswith(":") and "Supported" not in token:
                names.add(token)
            continue
        if line.strip().startswith("---"):
            started = True
            continue
        parts = line.split()
        if started and len(parts) >= 2:
            names.update(parts[1].split(","))
    return names


# ---- the checks ---------------------------------------------------------------------------------


def check_components(ffmpeg: str) -> str:
    version = run([ffmpeg, "-hide_banner", "-version"]).stdout.decode("utf-8", "replace")
    config = next((line for line in version.splitlines() if line.startswith("configuration:")), "")
    check(
        version.startswith("ffmpeg version"),
        "ffmpeg runs",
        version.splitlines()[0] if version else "no output",
    )
    check(
        "--enable-gpl" not in config and "--enable-nonfree" not in config,
        "the build is LGPL (no --enable-gpl / --enable-nonfree)",
        config,
    )
    for kind, wanted in REQUIRED.items():
        have = listing(ffmpeg, kind)
        missing = sorted(wanted - have)
        check(not missing, f"{kind}: all {len(wanted)} the app uses are present", f"missing {missing}")
    return config


def check_decoding(ffmpeg: str) -> None:
    from twitch_radio import player

    samples = {
        # file -> expected seconds
        "tone.webm": 3.0,  # YouTube itag 251: Opus in WebM
        "tone-vorbis.webm": 3.0,
        "tone.m4a": 3.0,  # YouTube itag 140: AAC in MP4
        "tone.mp3": 3.0,
        "tone.ogg": 3.0,
        "tone.flac": 2.0,
        "tone.wav": 2.0,
        "hls-audio/index.m3u8": 3.0,  # live streams: HLS, MPEG-TS, AAC
        "hls-video/index.m3u8": 3.0,  # ... which may carry video the app throws away
        "hls-aes/index.m3u8": 3.0,  # ... and may be AES-128 encrypted
    }
    with serve() as base:
        for name, expected in samples.items():
            code, seconds, rms, err = decode(player._decoder_cmd(f"{base}/{name}"))
            ok = code == 0 and abs(seconds - expected) <= 0.35 and rms > 100
            check(
                ok,
                f"decodes {name} over HTTP",
                f"exit {code}, {seconds:.2f}s (want {expected}), rms {rms:.0f}, {err[:200]}",
            )


def check_encoding(ffmpeg: str) -> None:
    from twitch_radio import player

    with serve() as base:
        code, seconds, rms, err = decode(player._decoder_cmd(f"{base}/tone.webm"))
        pcm_proc = run(
            [c for c in player._decoder_cmd(f"{base}/tone.webm") if c != "-re"],
        )
    cmd = player._encoder_cmd(160)
    proc = run(cmd, data=pcm_proc.stdout)
    ogg = proc.stdout
    check(
        proc.returncode == 0 and ogg.startswith(b"OggS") and b"OpusHead" in ogg[:200],
        "encoder: PCM in, Opus-in-Ogg out",
        f"exit {proc.returncode}, {len(ogg)} bytes, {proc.stderr.decode('utf-8', 'replace')[:200]}",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.ogg"
        path.write_bytes(ogg)
        back = run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-",
            ]
        )
    seconds, rms = pcm_stats(back.stdout)
    check(
        abs(seconds - 3.0) < 0.2 and rms > 100,
        "the encoded stream decodes back to the same length",
        f"{seconds:.2f}s rms {rms:.0f}",
    )
    check(code == 0, "decoder output feeds the encoder", err[:200])


def check_stdin_is_left_alone(ffmpeg: str) -> None:
    """The decoder must not read the app's control pipe (see player._decoder_cmd)."""
    from twitch_radio import player

    check("-nostdin" in player._decoder_cmd("x"), "decoder command carries -nostdin")


def check_tls(ffmpeg: str, config: str, require: bool) -> None:
    from twitch_radio import player

    try:
        import trustme  # type: ignore[import-not-found]
    except ImportError:
        check(not require, "local HTTPS tests (needs `pip install trustme`)", "trustme is not installed")
        print("[skip] local HTTPS tests: trustme is not installed")
        return
    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1", "localhost")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(context)
    uses_schannel = "--enable-schannel" in config
    with tempfile.TemporaryDirectory() as tmp, serve(tls=context) as base:
        ca_file = Path(tmp) / "ca.pem"
        ca.cert_pem.write_to_path(str(ca_file))
        cmd = player._decoder_cmd(f"{base}/tone.webm")

        code, seconds, rms, err = decode(cmd, before_input=("-tls_verify", "0"))
        check(
            code == 0 and seconds > 2.5,
            "HTTPS works (TLS handshake and a ranged download)",
            f"exit {code}, {err[:200]}",
        )
        code, seconds, _, err = decode(cmd, before_input=("-tls_verify", "1"))
        refused = code != 0 or seconds < 0.5
        if uses_schannel:
            # The app does not turn verification on (ffmpeg's default is off), so this is a finding,
            # not a requirement: it says whether Schannel would enforce it if the app asked.
            print(
                f"[info] with -tls_verify 1 an untrusted certificate is {'refused' if refused else 'STILL ACCEPTED'} (Schannel)"
            )
        else:
            check(
                refused,
                "with -tls_verify 1 an untrusted certificate is refused",
                f"exit {code}, {seconds:.2f}s",
            )
        if uses_schannel:
            print("[skip] -ca_file check: Schannel uses the Windows certificate store, not a file")
        else:
            code, seconds, _, err = decode(cmd, before_input=("-tls_verify", "1", "-ca_file", str(ca_file)))
            check(
                code == 0 and seconds > 2.5,
                "with -tls_verify 1 -ca_file a trusted certificate is accepted",
                f"exit {code}, {err[:200]}",
            )
            code, seconds, _, err = decode(cmd, env={"SSL_CERT_FILE": str(ca_file)})
            check(
                code == 0 and seconds > 2.5,
                "the app's own command trusts the certificates named by SSL_CERT_FILE (what paths.py sets on Linux)",
                f"exit {code}, {err[:200]}",
            )
        code, seconds, _, _ = decode(cmd)
        print(
            f"[info] with no TLS flags at all the app's command {'DOES' if code == 0 and seconds > 2.5 else 'does NOT'} accept an untrusted certificate"
        )


def check_real_https(ffmpeg: str, url: str, config: str) -> None:
    """A real server, real certificate chain, the platform's real trust store."""
    from twitch_radio import player

    # Certificate checking explicitly on; roots come from the Windows store (Schannel) or, on Linux,
    # from SSL_CERT_FILE as set by paths.point_tls_at_system_certificates() in main().
    code, seconds, rms, err = decode(player._decoder_cmd(url), before_input=("-tls_verify", "1"))
    check(
        code == 0 and seconds > 2.5 and rms > 100,
        f"decodes a real HTTPS URL with certificate checking on ({url})",
        f"exit {code}, {seconds:.2f}s, {err[:300]}",
    )


def check_youtube(ffmpeg: str, url: str, config: str, require: bool) -> None:
    """The real thing: resolve a real YouTube video the way the app does, then pull 12 s of the
    audio from Google's CDN through the app's decoder command. Informational unless `require`,
    because YouTube changes under everyone's feet and that is not ffmpeg's fault."""
    from twitch_radio import player

    try:
        import yt_dlp
    except ImportError:
        check(not require, "YouTube test (needs yt-dlp installed)", "yt_dlp is not importable")
        return
    options: dict[str, object] = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "remote_components": ["ejs:github"],  # the app's setting: yt-dlp fetches its solver scripts
    }
    node = shutil.which("node")
    if node:
        options["js_runtimes"] = {"node": {"path": node}}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
        stream_url = info["url"]
        print(
            f"[info] YouTube resolved: {info.get('title')!r}, {info.get('ext')} / {info.get('acodec')}, {info.get('protocol')}"
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is reported, not raised
        check(not require, "YouTube test: resolving the video", f"{type(exc).__name__}: {str(exc)[:300]}")
        print(
            f"[skip] YouTube test: could not resolve ({type(exc).__name__}); try again later or pass another --youtube-url"
        )
        return

    cmd = player._decoder_cmd(stream_url)
    cmd[cmd.index("-f") : cmd.index("-f")] = ["-t", "12"]  # first 12 seconds only
    code, seconds, rms, err = decode(cmd, real_time=False)
    check(
        code == 0 and seconds > 10 and rms > 50,
        "YouTube audio decodes through the app's decoder command (as the app runs it)",
        f"exit {code}, {seconds:.1f}s, rms {rms:.0f}, {err[:300]}",
    )
    code, seconds, rms, err = decode(cmd, before_input=("-tls_verify", "1"))
    ok = code == 0 and seconds > 10
    print(
        f"[info] same stream with certificate checking on (-tls_verify 1): {'works' if ok else 'FAILS'}"
        + ("" if ok else f" (exit {code}, {err[:200]})")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ffmpeg", help="path to the ffmpeg executable to check")
    parser.add_argument(
        "--https-url",
        help="a real https:// link to a 3 s sample, e.g. tests/fixtures/audio/tone.webm on raw.githubusercontent.com",
    )
    parser.add_argument(
        "--youtube-url",
        help="also resolve this YouTube video and decode 12 s of its real stream (needs internet and yt-dlp)",
    )
    parser.add_argument(
        "--require-youtube", action="store_true", help="count a failed YouTube test as a failure"
    )
    parser.add_argument(
        "--require-tls",
        action="store_true",
        help="fail (instead of skipping) when the local HTTPS tests cannot run",
    )
    args = parser.parse_args()

    ffmpeg = str(Path(args.ffmpeg).resolve())
    if not Path(ffmpeg).is_file():
        print(f"not found: {ffmpeg}")
        return 1
    # The app does this for its children at startup; do the same so real-world HTTPS checks behave like the app.
    from twitch_radio import paths

    paths.point_tls_at_system_certificates()
    # The app finds ffmpeg through PATH (player._ffmpeg); point that at the build being checked.
    os.environ["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("ffmpeg") is None or Path(shutil.which("ffmpeg") or "").resolve() != Path(ffmpeg):
        # e.g. the file is called something else: link it under the name the app looks for
        tmp = Path(tempfile.mkdtemp(prefix="check-ffmpeg-"))
        link = tmp / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        shutil.copy2(ffmpeg, link)
        os.environ["PATH"] = str(tmp) + os.pathsep + os.environ["PATH"]
    print(f"Checking {ffmpeg} ({Path(ffmpeg).stat().st_size / 1048576:.1f} MB)\n")

    config = check_components(ffmpeg)
    check_decoding(ffmpeg)
    check_encoding(ffmpeg)
    check_stdin_is_left_alone(ffmpeg)
    check_tls(ffmpeg, config, args.require_tls)
    if args.https_url:
        check_real_https(ffmpeg, args.https_url, config)
    if args.youtube_url:
        check_youtube(ffmpeg, args.youtube_url, config, args.require_youtube)

    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed:")
        for what in FAILS:
            print(f"  - {what}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
