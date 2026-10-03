Drag everything here (except this README) onto the root of your Twitch-Radio repo and replace.
On github.com: Add file > Upload files, keeping the folder structure.

SMALLER INSTALLER
  The packaged app no longer ships Deno: it uses Electron's built-in Node as yt-dlp's JavaScript
  runtime. Also trims Chromium's 55 language files to en-US.
  gui/main.js, gui/package.json, twitch_radio/config.py, twitch_radio/preflight.py,
  packaging/*, build-windows.bat, scripts/build-linux.sh, scripts/smoke_core.py,
  scripts/check_host_runtime.py (new), tests, docs.
  Source installs / the server (systemd) are unchanged and still use deno or node from PATH.

FREE AI RELEASE NOTES
  scripts/release_notes.py + .github/workflows/release.yml + cut-release.yml
  Uses GitHub Models with the built-in token: no secret to create.
  Optional upgrades: secret ANTHROPIC_API_KEY, or secret AI_API_KEY + variables AI_BASE_URL and AI_MODEL.

AFTER YOUR NEXT RELEASE
  1. Install it and play one YouTube link (CI cannot test YouTube, which blocks GitHub's runners).
  2. Release page: the changes section should end "Summarised automatically by AI".
