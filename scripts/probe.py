#!/usr/bin/env python3
"""Twitch Radio measurement probe.

Read-only: it measures and prints, and changes nothing in the repo. It needs
psutil, which the bot itself does not (`pip install psutil` into any Python).
Run from the repo root, for example:

    python scripts/probe.py env
    python scripts/probe.py audio
    python scripts/probe.py tree --match "bot.py" --seconds 180
    python scripts/probe.py tree --match "Twitch Radio" --seconds 180   # the desktop app
    python scripts/probe.py resolve --url URL --query "artist song"
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

MB = 1024 * 1024
TRACK_SECONDS = 240


# --------------------------------------------------------------------------- helpers
def need_psutil() -> None:
    if psutil is None:
        sys.exit("psutil is missing: pip install psutil")


def need_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        sys.exit("ffmpeg not found on PATH. Install it first (see instructions), then open a NEW terminal.")
    return path


def first_line(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return ((r.stdout or r.stderr).strip().splitlines() or ["(no output)"])[0]
    except Exception as exc:  # noqa: BLE001
        return f"(failed: {exc})"


def run_measured(cmd, stdin_path=None, stdout_path=os.devnull):
    """Run cmd, return cpu seconds, wall seconds, peak working set, peak private bytes, peak threads."""
    need_psutil()
    stdin = open(stdin_path, "rb") if stdin_path else None
    out = open(stdout_path, "wb")
    t0 = time.perf_counter()
    p = psutil.Popen(cmd, stdin=stdin, stdout=out, stderr=subprocess.DEVNULL)
    cpu = 0.0
    peak_ws = 0
    peak_priv = 0
    peak_threads = 0
    while p.poll() is None:
        try:
            ct = p.cpu_times()
            cpu = ct.user + ct.system
            mi = p.memory_info()
            peak_ws = max(peak_ws, mi.rss, getattr(mi, "peak_wset", 0) or 0)
            peak_priv = max(peak_priv, getattr(mi, "private", 0) or 0)
            peak_threads = max(peak_threads, p.num_threads())
        except psutil.Error:
            break
        time.sleep(0.02)
    wall = time.perf_counter() - t0
    if stdin:
        stdin.close()
    out.close()
    return cpu, wall, peak_ws, peak_priv, peak_threads


def ffmpeg_ok(*args: str) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, text=True)
    return r.returncode == 0


# --------------------------------------------------------------------------- env
def cmd_env(a) -> None:
    need_psutil()
    print("== ENVIRONMENT ==")
    print("platform     :", platform.platform())
    print("python       :", sys.version.split()[0], "|", sys.executable)
    print("logical cores:", psutil.cpu_count(logical=True), "| physical:", psutil.cpu_count(logical=False))
    print("RAM total    : %.1f GB" % (psutil.virtual_memory().total / 1024**3))
    print("psutil       :", psutil.__version__)
    print("ffmpeg       :", shutil.which("ffmpeg"), "|", first_line(["ffmpeg", "-version"]))
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        print("  libopus    :", "libopus" in enc)
    for tool in ("deno", "node", "git"):
        print(
            f"{tool:13s}:",
            shutil.which(tool),
            "|",
            first_line([tool, "--version"]) if shutil.which(tool) else "not installed",
        )

    print("\n== COST OF THE CURRENT PROCESS-TREE WALK (what the in-app sampler does every 2s) ==")
    me = psutil.Process()
    times = []
    for _ in range(30):
        t = time.perf_counter()
        me.children(recursive=True)
        times.append((time.perf_counter() - t) * 1000)
    print(
        "system processes: %d | children(recursive=True): median %.1f ms, max %.1f ms (30 runs)"
        % (len(psutil.pids()), statistics.median(times), max(times))
    )

    print("\n== IMPORT COST (fresh interpreter each; RSS is working set) ==")
    repo = Path(a.repo).resolve()
    env = dict(os.environ, PYTHONPATH=str(repo), TWITCH_RADIO_HOME=tempfile.mkdtemp(prefix="trprobe_"))
    snippet = (
        "import psutil,time,sys;p=psutil.Process();b=p.memory_info().rss;t=time.perf_counter();c=time.process_time();"
        "import {m};print('%+6.1f MB  wall %.2fs  cpu %.2fs' % ((p.memory_info().rss-b)/1048576,time.perf_counter()-t,time.process_time()-c))"
    )
    for mod in (
        "yt_dlp",
        "curl_cffi",
        "aiohttp",
        "twitchio",
        "psutil",
        "twitch_radio.config",
        "twitch_radio.extraction",
        "twitch_radio.bot",
    ):
        r = subprocess.run(
            [sys.executable, "-c", snippet.format(m=mod)], capture_output=True, text=True, env=env, cwd=repo
        )
        print(
            f"import {mod:26s}",
            (
                r.stdout.strip()
                or ("FAILED: " + r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "no output")
            ),
        )
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,twitch_radio.bot;print('twitch_radio.bot pulls in:',sorted(m for m in ('aiohttp','yt_dlp','twitchio','curl_cffi','psutil') if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
    )
    print(r.stdout.strip() or r.stderr.strip()[-300:])


# --------------------------------------------------------------------------- audio
def make_inputs(tmp: Path, seconds: int):
    src = tmp / "track.webm"
    music = tmp / "music.pcm"
    silence = tmp / "silence.pcm"
    print(f"preparing {seconds}s synthetic test track (about 20 s)...")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=220:d={seconds}:r=48000",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=330:d={seconds}:r=48000",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=440:d={seconds}:r=48000",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=d={seconds}:c=pink:r=48000:a=0.15",
            "-filter_complex",
            "[0][1][2][3]amix=inputs=4:normalize=0,tremolo=f=2:d=0.4,aformat=channel_layouts=stereo",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
            str(src),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(music),
        ],
        check=True,
    )
    size = music.stat().st_size
    with open(silence, "wb") as f:
        left = size
        while left > 0:
            n = min(left, 1 << 20)
            f.write(b"\0" * n)
            left -= n
    return src, music, silence


def cmd_audio(a) -> None:
    need_psutil()
    need_ffmpeg()
    secs = a.seconds
    tmp = Path(tempfile.mkdtemp(prefix="trprobe_audio_"))
    try:
        src, music, silence = make_inputs(tmp, secs)

        def dec(filt, threads1=False):
            c = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "fatal"]
            if threads1:
                c += ["-filter_threads", "1", "-threads", "1"]
            c += ["-i", str(src)]
            if filt:
                c += ["-af", filt]
            return c + ["-f", "s16le", "-ar", "48000", "-ac", "2", "-"]  # stdout is sent to the null device

        def enc(extra):
            return [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-i",
                "-",
                "-c:a",
                "libopus",
                "-b:a",
                "160k",
                "-vbr",
                "on",
                *extra,
                "-f",
                "ogg",
                "-",
            ]

        rows = [
            ("DECODE (what the app does)", dec(""), None),
            ("DECODE + -threads 1", dec("", True), None),
            ("ENCODE music   160k (current, level 10)", enc([]), music),
            ("ENCODE silence 160k (idle, 24/7)", enc([]), silence),
            ("ENCODE music   160k level 5", enc(["-compression_level", "5"]), music),
            ("ENCODE music   160k level 3", enc(["-compression_level", "3"]), music),
        ]
        print(
            f"\n{'case':44s} {'cpu-s':>7s} {'% of 1 core, realtime':>22s} {'peak WS MB':>11s} {'peak priv MB':>13s} {'threads':>8s}"
        )
        for label, cmd, stdin_path in rows:
            cpu, wall, ws, priv, th = run_measured(cmd, stdin_path)
            print(
                f"{label:44s} {cpu:7.2f} {cpu / secs * 100:21.2f}% {ws / MB:11.1f} {priv / MB:13.1f} {th:8d}"
            )
        print(
            f"\n(track length {secs}s; 'realtime' = cpu-seconds / track seconds, i.e. the average load while it plays live)"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- tree
def classify(name: str, cmd: list[str]) -> str:
    s = " ".join(cmd).lower()
    n = name.lower()
    if "ffmpeg" in n:
        if "libopus" in s:
            return "ffmpeg-encoder"
        if "-reconnect" in s or "-re " in s + " ":
            return "ffmpeg-decoder"
        return "ffmpeg-other"
    if "--extractor-worker" in s or "extractor_worker" in s:
        return "ytdlp-worker"
    for flag in (
        "--preflight",
        "--env-json",
        "--env-update-stdin",
        "--ytdlp-check",
        "--ytdlp-install",
        "--resolve-test",
    ):
        if flag in s:
            return "core-oneshot" + flag
    if "--headless" in s or "bot.py" in s or "twitchradiocore" in n:
        return "core"
    if "--type=renderer" in s:
        return "electron-renderer"
    if "--type=gpu-process" in s:
        return "electron-gpu"
    if "--type=utility" in s:
        return "electron-utility"
    if "--type=crashpad" in s:
        return "electron-crashpad"
    if "electron" in n or "twitch radio" in n or "twitchradio" in n:
        return "electron-main/node"
    if n.startswith(("deno", "node", "qjs", "quickjs")):
        return "js-runtime"
    return name


def cmd_tree(a) -> None:
    need_psutil()
    ncpu = psutil.cpu_count(logical=True) or 1
    me = os.getpid()
    needle = a.match.lower() if a.match else None
    print(
        f"watching processes matching {a.match!r} (and all their children) for {a.seconds}s, every {a.interval}s. Ctrl+C ends early."
    )
    start = time.time()
    t0 = time.perf_counter()
    known: dict[tuple, dict] = {}
    ticks = []  # (t, total_cpu_s, total_priv, total_ws, nproc, per_role_priv)
    csv_rows = []
    try:
        while time.perf_counter() - t0 < a.seconds:
            tick_start = time.perf_counter()
            roots = []
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                if p.pid == me:
                    continue
                if a.root_pid:
                    if p.pid == a.root_pid:
                        roots.append(p)
                elif needle:
                    text = ((p.info["name"] or "") + " " + " ".join(p.info["cmdline"] or [])).lower()
                    if needle in text and "probe.py" not in text:
                        roots.append(p)
            procs = {}
            for r in roots:
                try:
                    procs[r.pid] = r
                    for c in r.children(recursive=True):
                        procs[c.pid] = c
                except psutil.Error:
                    pass
            per_role_priv: dict[str, int] = {}
            tot_priv = tot_ws = 0
            for pid, p in procs.items():
                try:
                    with p.oneshot():
                        ct = p.cpu_times()
                        cpu = ct.user + ct.system
                        mi = p.memory_info()
                        created = p.create_time()
                        name = p.name()
                        cmd = p.cmdline()
                except psutil.Error:
                    continue
                priv = getattr(mi, "private", None) or mi.rss
                key = (pid, round(created, 2))
                rec = known.get(key)
                if rec is None:
                    role = classify(name, cmd)
                    base = 0.0 if created >= start - 1.0 else cpu
                    rec = known[key] = dict(
                        role=role, name=name, cpu_base=base, cpu=cpu, priv_max=0, ws_max=0, seen=0
                    )
                rec["cpu"] = cpu
                rec["priv_max"] = max(rec["priv_max"], priv)
                rec["ws_max"] = max(rec["ws_max"], mi.rss)
                rec["seen"] += 1
                per_role_priv[rec["role"]] = per_role_priv.get(rec["role"], 0) + priv
                tot_priv += priv
                tot_ws += mi.rss
            total_cpu = sum(r["cpu"] - r["cpu_base"] for r in known.values())
            t = time.perf_counter() - t0
            ticks.append((t, total_cpu, tot_priv, tot_ws, len(procs), per_role_priv))
            csv_rows.append(
                (round(t, 1), round(total_cpu, 2), round(tot_priv / MB, 1), round(tot_ws / MB, 1), len(procs))
            )
            if int(t) % 60 < a.interval and len(ticks) > 1:
                print(
                    f"  t={t:5.0f}s  procs={len(procs):2d}  private={tot_priv / MB:7.1f} MB  cpu so far={total_cpu:7.1f}s"
                )
            time.sleep(max(0.0, a.interval - (time.perf_counter() - tick_start)))
    except KeyboardInterrupt:
        pass
    if not ticks:
        print(
            "No matching process found. Is the app running? Try --match with part of the process name or command line."
        )
        return
    dur = ticks[-1][0] or 1.0
    cpu_total = ticks[-1][1]
    privs = [x[2] for x in ticks]
    wss = [x[3] for x in ticks]
    print("\n== SUMMARY ==")
    print(f"duration {dur:.0f}s | logical cores {ncpu}")
    print(
        f"CPU   : {cpu_total:.1f} cpu-s total = {cpu_total / dur * 100:.2f}% of ONE core  ({cpu_total / dur / ncpu * 100:.2f}% of the whole machine)"
    )
    print(
        f"MEMORY: private avg {statistics.mean(privs) / MB:.0f} MB, max {max(privs) / MB:.0f} MB | working set avg {statistics.mean(wss) / MB:.0f} MB, max {max(wss) / MB:.0f} MB"
    )
    print("note: short-lived processes (<interval) may be missed or under-counted.\n")
    roles = sorted({r["role"] for r in known.values()})
    print(
        f"{'role':34s} {'spawned':>8s} {'cpu-s':>8s} {'%1core':>7s} {'avg priv MB (sum)':>18s} {'max single MB':>14s}"
    )
    for role in roles:
        recs = [r for r in known.values() if r["role"] == role]
        cpu = sum(r["cpu"] - r["cpu_base"] for r in recs)
        avg_priv = statistics.mean(x[5].get(role, 0) for x in ticks) / MB
        mx = max(r["priv_max"] for r in recs) / MB
        print(f"{role:34s} {len(recs):8d} {cpu:8.1f} {cpu / dur * 100:6.2f}% {avg_priv:18.1f} {mx:14.1f}")
    if a.csv:
        with open(a.csv, "w", encoding="utf-8") as f:
            f.write("t_s,cpu_s_total,private_mb,workingset_mb,procs\n")
            for row in csv_rows:
                f.write(",".join(map(str, row)) + "\n")
        print(f"\nper-tick CSV written to {a.csv}")


# --------------------------------------------------------------------------- resolve
def cmd_resolve(a) -> None:
    need_psutil()
    import yt_dlp  # noqa: PLC0415

    cache = tempfile.mkdtemp(prefix="trprobe_ytcache_")
    proc = psutil.Process()
    base = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "socket_timeout": 20,
        "allowed_extractors": ["youtube(:.*)?"],
        "cachedir": cache,
        "remote_components": ["ejs:github"],
    }
    fast = {
        **base,
        "js_runtimes": {"quickjs": {}},
        "extractor_args": {"youtube": {"player_client": ["visionos"]}},
    }
    full = {**base, "js_runtimes": {a.js_runtime: {}}}
    meta = {
        **base,
        "js_runtimes": {"quickjs": {}},
        "ignore_no_formats_error": True,
        "format": None,
        "extractor_args": {"youtube": {"player_skip": ["js", "webpage"], "player_client": ["visionos"]}},
    }
    flat = {**base, "extract_flat": "in_playlist"}
    cases = [
        ("A  URL  fast client (current first attempt)", fast, a.url),
        ("B  URL  full / default clients (current fallback)", full, a.url),
        ("C  URL  metadata-only, skip js+webpage (HYPOTHESIS)", meta, a.url),
        ("D  SEARCH flat, no formats (HYPOTHESIS)", flat, f"ytsearch1:{a.query}"),
        ("E  SEARCH full (current !sr by name)", fast, f"ytsearch1:{a.query}"),
    ]
    print(f"yt-dlp {yt_dlp.version.__version__} | each case run {a.runs}x (1st = cold, later = warm cache)\n")
    print(f"{'case':56s} {'run':>3s} {'wall s':>7s} {'cpu s':>6s} {'RSS MB':>7s}  result")
    for label, opts, target in cases:
        for i in range(1, a.runs + 1):
            c0 = time.process_time()
            t0 = time.perf_counter()
            res = ""
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=False)
                if info and info.get("entries"):
                    info = next(iter(info["entries"]), None) or {}
                keys = {k: info.get(k) for k in ("id", "title", "duration", "live_status")}
                res = f"url={'yes' if info.get('url') else 'no'} | title={str(keys['title'])[:28]!r} dur={keys['duration']} live={keys['live_status']}"
            except Exception as exc:  # noqa: BLE001
                res = "ERROR: " + re.sub(r"\s+", " ", str(exc))[:110]
            print(
                f"{label:56s} {i:3d} {time.perf_counter() - t0:7.2f} {time.process_time() - c0:6.2f} "
                f"{proc.memory_info().rss / MB:7.0f}  {res}"
            )
    print("\n(cpu s = this Python process only; a JS runtime child process, if any, is not included)")
    shutil.rmtree(cache, ignore_errors=True)


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("env", help="environment, tool versions, import costs, process-walk cost")
    s.add_argument("--repo", default=".", help="repo root (default: current folder)")
    s.set_defaults(fn=cmd_env)

    s = sub.add_parser("audio", help="ffmpeg decode/encode CPU + memory benchmark on THIS machine")
    s.add_argument("--seconds", type=int, default=TRACK_SECONDS)
    s.set_defaults(fn=cmd_audio)

    s = sub.add_parser("tree", help="sample CPU+memory of a running app and all its children")
    s.add_argument(
        "--match", help="substring of process name or command line, e.g. 'bot.py' or 'Twitch Radio'"
    )
    s.add_argument("--root-pid", type=int)
    s.add_argument("--seconds", type=int, default=120)
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("--csv")
    s.set_defaults(fn=cmd_tree)

    s = sub.add_parser("resolve", help="yt-dlp timing: current path vs hypotheses (needs internet)")
    s.add_argument("--url", default="https://www.youtube.com/watch?v=jNQXAC9IVRw")
    s.add_argument("--query", default="daft punk one more time")
    s.add_argument("--runs", type=int, default=2)
    s.add_argument("--js-runtime", default="deno")
    s.set_defaults(fn=cmd_resolve)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
