#!/usr/bin/env python3
"""Check that a built desktop app can stand in for Node.js, which is how the packaged app runs
yt-dlp's YouTube JavaScript solver (there is no separate Deno or Node in the installer).

  python scripts/check_host_runtime.py "dist/app/win-unpacked/Twitch Radio.exe"
  python scripts/check_host_runtime.py dist/app/linux-unpacked/twitch-radio

It runs the executable the way yt-dlp does (`<exe> --permission -`, script on stdin, with
ELECTRON_RUN_AS_NODE=1) and fails if any of these is not true:

  1. `--version` reports Node 22 or newer (yt-dlp refuses older runtimes)
  2. a script sent on stdin runs and its output comes back
  3. Node's permission model is enforced, so the downloaded solver scripts cannot read files

If this fails after an Electron or electron-builder change, YouTube links would silently lose the
solver. The usual cause is the `runAsNode` Electron fuse being switched off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

MIN_NODE_MAJOR = 22  # what yt-dlp requires of a "node" runtime
TIMEOUT = 60


def run(exe: str, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the executable under test is passed in by the caller
        [exe, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
        check=False,
    )


def check(exe: str) -> list[str]:
    """Problems found; an empty list means the app works as a Node runtime."""
    problems: list[str] = []

    done = run(exe, ["--version"])
    match = re.match(r"^v(\d+)\.", done.stdout.strip())
    if not match:
        problems.append(
            f"--version did not print a Node version (stdout={done.stdout.strip()[:80]!r}, rc={done.returncode})"
        )
    elif int(match.group(1)) < MIN_NODE_MAJOR:
        problems.append(f"Node {done.stdout.strip()} is older than the {MIN_NODE_MAJOR} yt-dlp requires")

    done = run(exe, ["--permission", "-"], stdin="console.log(JSON.stringify({sum: 1 + 1}))")
    try:
        ran = json.loads(done.stdout.strip().splitlines()[-1]) == {"sum": 2}
    except (ValueError, IndexError):
        ran = False
    if not ran:
        problems.append(
            f"a script on stdin did not run (rc={done.returncode}, stderr={done.stderr.strip()[:120]!r})"
        )

    probe = 'try { require("fs").readFileSync(process.execPath); console.log("READ"); } catch (e) { console.log(e.code); }'
    done = run(exe, ["--permission", "-"], stdin=probe)
    if "ERR_ACCESS_DENIED" not in done.stdout:
        problems.append(f"--permission did not block file access (got {done.stdout.strip()[:80]!r})")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    exe = argv[1]
    if not os.path.isfile(exe):
        print(f"FAIL: {exe} does not exist")
        return 1
    problems = check(exe)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print(
            f"OK: {os.path.basename(exe)} works as a Node {MIN_NODE_MAJOR}+ runtime with the permission model enforced"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
