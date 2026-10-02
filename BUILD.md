# Building the Twitch Radio desktop app (Windows)

The installer is made in four automatic steps: Python environment, ffmpeg + Deno download,
the bot core (PyInstaller), and the desktop app (Electron + electron-builder).

## Prerequisites (build machine only)

| Tool | Version | Get it |
|---|---|---|
| Python | 3.11 or newer, 64-bit, "Add python.exe to PATH" ticked | https://www.python.org/downloads/ |
| Node.js | 22 LTS or newer (includes npm; Electron 42 needs 22.12+) | https://nodejs.org |
| Internet | for pip, npm, ffmpeg and Deno downloads | |

Users of the finished installer need **nothing** installed.

## Build

1. Open Command Prompt in the project folder.
2. Run:

   ```
   build-windows.bat
   ```

3. Wait (the first build takes several minutes and downloads a few hundred MB).
4. The installer appears at `dist\app\Twitch Radio Setup 1.0.0.exe`. An unpacked copy you can
   run without installing is produced by `cd gui && npm run dist:dir` (`dist\app\win-unpacked\`).

If a step fails the script stops and says which one. Re-running resumes quickly: the Python
environment, downloaded tools (`packaging\.cache`) and `gui\node_modules` are reused.

## Try it before building

```
setup.bat          (once - creates .venv and installs requirements)
run-gui.bat        (starts the desktop app against your source checkout)
```

In this mode the app uses your existing `.env` and `data\` folder in the project directory.
Source installs need Deno for YouTube too: put `deno` (2.3 or newer) on PATH, or set
`YTDLP_JS_RUNTIME_PATH` in `.env`.

## What ends up where (installed app)

| Item | Location |
|---|---|
| Program | `%LOCALAPPDATA%\Programs\Twitch Radio\` |
| Your settings, tokens, queue | `%APPDATA%\TwitchRadio\` (`.env`, `data\`, `logs\`) - kept on uninstall |
| App window preferences | `%APPDATA%\TwitchRadio\gui\` |
| Updated yt-dlp (if you installed one) | `%APPDATA%\TwitchRadio\yt-dlp\` (`current\`, `previous\`, `state.json`) |
| Downloaded JS solver scripts | `%APPDATA%\TwitchRadio\data\yt-dlp-cache\challenge-solver\` |

To move an existing install: copy your old `.env` and `data\` folder into `%APPDATA%\TwitchRadio\`.

## Pinned tools

`packaging\tools.lock.json` pins ffmpeg and Deno (version, URL, SHA-256). `packaging\fetch-tools.ps1`
verifies every download against it and fails the build on a mismatch (the bad file is deleted).
To bump a tool: change `version` and `url`, download the file once, put its SHA-256 in `sha256`
(`Get-FileHash <file>`). For Deno, compare with the `.sha256sum` file next to the release asset.
Deno must stay at 2.3 or newer (what the yt-dlp JS solver requires).

`build-windows.bat` still reuses `ffmpeg` / `deno` found on your PATH instead of downloading, which
bypasses the pins. For a reproducible build, delete `packaging\bin` and remove them from PATH, or
run `powershell -File packaging\fetch-tools.ps1 -Force`. The release workflow always uses the pins.

`THIRD_PARTY_NOTICES.txt` (repo root) is copied into the installer. Keep it current when you add
a dependency or change a tool.

## Checks you can run locally

```
pip install -r requirements-dev.txt -r requirements.txt
ruff check .
mypy twitch_radio bot.py
pytest -q

cd gui
npm ci
npm run lint
npm run check          # node --check on every GUI file
npm test               # node:test unit tests (update-check logic)
npm run check:build    # validates the electron-builder config and the files it references
```

After a core build, smoke-test the frozen exe (catches hidden-import and data-file problems that
only appear when frozen):

```
python scripts\smoke_core.py --exe dist\core\TwitchRadioCore\TwitchRadioCore.exe --bin packaging\bin
python scripts\smoke_core.py --exe dist\core\TwitchRadioCore\TwitchRadioCore.exe --bin packaging\bin --resolve-only
```

The second command resolves a real YouTube video and needs YouTube and GitHub reachable.

`ruff format --check .` runs in CI; fix a failure with `ruff format .`.

## Cutting a release

```
python scripts\bump-version.py 1.1.0
git add -A
git commit -m "Release 1.1.0"
git tag v1.1.0
git push origin main v1.1.0
```

`bump-version.py` sets the version in `gui/package.json`, `gui/package-lock.json` and
`twitch_radio/version.py`. A tag with a `-` suffix (`v1.1.0-rc1`) is published as a pre-release.
For a dry run, start the **Release** workflow by hand (Actions > Release > Run workflow) with the
version: it builds everything and uploads the files as a workflow artifact without publishing.

## How CI works

* **ci.yml** runs on pushes and pull requests to `main`; a newer push cancels the older run.
  * `python`: Python 3.11 and 3.12 - `ruff check`, `mypy`, `pytest`.
  * `electron`: Node 22 on Windows - `npm ci`, lint, syntax check, unit tests, build-config check.
  * `windows-build-smoke`: only on pull requests that touch `twitch_radio/`, `packaging/`, `gui/` or
    `requirements*.txt`. Builds the core with PyInstaller and runs `scripts/smoke_core.py`. The last
    step (real YouTube resolve, needs the network) may fail without failing the PR.
* **release.yml** runs on `v*.*.*` tags. It first checks that the tag equals the version in
  `gui/package.json`, `gui/package-lock.json` and `twitch_radio/version.py`, then builds on
  Windows (pinned tools, PyInstaller core, smoke tests including the real YouTube resolve, which
  is required here, then the NSIS installer), writes `SHA256SUMS.txt`, and publishes a GitHub
  release with the installer, `SHA256SUMS.txt` and `THIRD_PARTY_NOTICES.txt`. Release notes are the
  commit subjects since the previous tag under a fixed install / SmartScreen note.
* Third-party actions are pinned to full commit SHAs with the version in a comment.
  `.github/dependabot.yml` opens weekly pull requests for GitHub Actions, pip and npm, which is how
  they stay fresh. Review those PRs (Dependabot updates the SHA and the comment together).

Code signing is optional: set the repository secrets `WIN_CSC_LINK` (a base64 `.pfx`, or a URL) and
`WIN_CSC_KEY_PASSWORD`. Without them the installer is built unsigned and the release notes say so.
Build provenance (`actions/attest-build-provenance`) needs a public repository, or GitHub Enterprise
Cloud for a private one; where it isn't available the step is skipped without failing the release.

## How the update mechanisms behave

**YouTube JS solver.** yt-dlp needs a JavaScript runtime (the bundled Deno) and "solver" scripts to
read some YouTube links. The `yt-dlp-ejs` pip package is deliberately **not** bundled. yt-dlp is
started with `remote_components: ["ejs:github"]`, downloads the scripts that match its own version
from github.com/yt-dlp/ejs over HTTPS the first time it needs them, and caches them in
`data\yt-dlp-cache`, so they survive restarts. If the download is blocked, the Logs tab shows an
error after start-up, the setup checklist keeps "Download the YouTube JS solver" open, and
Settings > App shows "JS solver: not downloaded yet". Allow github.com and restart the bot.
yt-dlp runs the scripts in `deno run` with no `--allow-*` permissions, so they get no file,
network, environment or process access, and it checks the downloaded script against a hash it
ships with.

**yt-dlp updates.** yt-dlp is frozen into the core, but Settings > App can install a newer release
without a new installer. The core downloads the release's `yt-dlp` zipimport file and
`SHA2-256SUMS` from github.com (HTTPS only, every redirect checked), verifies the SHA-256 before
unpacking anything, unpacks only the `yt_dlp/` package into `%APPDATA%\TwitchRadio\yt-dlp\staging`,
imports it once in a child process, then swaps it in as `current` and keeps the old one as
`previous` ("Roll back" in Settings). At start-up the core and every extractor worker put
`current` ahead of the bundled copy on `sys.path`, but only if it is newer than the bundled one;
if it fails to import, the bundled copy is used and the failure is remembered in `state.json`.
Updating restarts the bot. With "Check for yt-dlp updates on startup" on (default), the app only
checks, never installs on its own. Everything is logged in the Logs tab.
Limit: the checksum file comes from the same release, so it detects corruption and tampering in
transit, not a compromised release (yt-dlp's GPG signature is not checked).

**App updates.** Settings > App > "Check for app updates" asks the GitHub Releases API for the
latest release of `Pylxyr/Twitch-Radio` (constant `UPDATE_REPO` in `gui/updates.js`), compares it
with the running version and shows a download link, opened through the app's safe external-link
handler. Nothing is installed automatically. Automatic checks run at most once a day and can be
turned off in the same card.

## Things to check on first build

* **Antivirus / SmartScreen.** An unsigned installer shows "Windows protected your PC" and
  PyInstaller executables are sometimes flagged by antivirus. Both are normal for unsigned apps; a
  code-signing certificate fixes SmartScreen. Add the install folder to your antivirus exclusions if
  it quarantines `TwitchRadioCore.exe`.
* **ffmpeg licence.** The pinned ffmpeg is a GPL v3 build. If you distribute the installer, keep
  `bin\FFMPEG-LICENSE.txt` and `THIRD_PARTY_NOTICES.txt` in it and see https://ffmpeg.org/legal.html.
