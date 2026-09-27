# Twitch Radio Bot — Windows Setup

A local song-request bot for your Twitch stream. Viewers request YouTube songs
with `!sr <link or search>`, it plays them back-to-back (filling gaps with a
"radio mix" of similar songs if you want), and you bring it into OBS as two
sources:

- **Audio** — a continuous MP3 stream
- **Overlay** — an on-screen "Now Playing" / queue graphic

Everything runs on your own PC — nothing is uploaded anywhere, and the bot
only talks to Twitch (chat) and YouTube (to fetch songs).

This guide assumes no prior setup. It takes about 15–20 minutes.

## 1. Install the prerequisites

You need **Python**, **ffmpeg**, and **Deno** on your PC before the bot
will run. The easiest way to get the latter two is **winget**, Windows'
built-in package manager, so confirm that works first.

### First, confirm winget works

Windows 10 (version 1809 or later, fully updated) and Windows 11 come with
winget built in, but it's occasionally missing or out of date. Open
**Command Prompt** (Windows Search → type `cmd` → Enter) and run:

```
winget --version
```

- **Prints a version number** (e.g. `v1.7.10582`) → you're set, skip to
  ffmpeg below.
- **"winget is not recognized"** → open the **Microsoft Store** app,
  search for **"App Installer"**, and click **Get** (or **Update**, if it's
  already listed) — winget ships inside that package. Close and reopen
  Command Prompt afterward and try `winget --version` again.
- **Still not recognized** (older Windows 10, or the Store is disabled on
  this PC) → download the latest `.msixbundle` from
  https://github.com/microsoft/winget-cli/releases/latest, double-click it
  to install, then try again in a new Command Prompt window.

If none of that gets winget working, that's fine — every winget command
below has a manual fallback step right after it, so you can skip winget
entirely and install ffmpeg/Deno by hand instead.

One more thing worth having: an account with **administrator rights** on
this PC (most personal Windows accounts already are). If any install
command below fails with a permissions error, right-click Command Prompt
in the Start menu and choose **"Run as administrator"**, then retry it.

Once winget is sorted (or you've decided to skip it), install the other
three. After each one, **open a brand-new Command Prompt window** before
checking it worked — PATH changes don't apply to a window that was already
open.

### Python 3.11 or newer

Download from **https://www.python.org/downloads/** and run the installer.
On the very first screen, tick **"Add python.exe to PATH"** before clicking
Install — this is the most common thing people miss. (Alternatively, if
winget is working: `winget install --id=Python.Python.3.12 -e`.)

Open a new Command Prompt and run `python --version` to confirm it works.

### FFmpeg (does the actual audio decoding)

Open Command Prompt and run:

```
winget install --id=Gyan.FFmpeg -e
```

Then open a new Command Prompt and run `ffmpeg -version` to confirm it
works. If it says `ffmpeg` isn't recognized, winget's PATH setup for this
package is occasionally flaky — restart your PC once and try again, or
install manually:

1. Download a build from https://www.gyan.dev/ffmpeg/builds/ (the
   "release essentials" zip is enough).
2. Extract it somewhere permanent, e.g. `C:\ffmpeg`.
3. Add `C:\ffmpeg\bin` to your PATH (Windows Search → "Edit the system
   environment variables" → Environment Variables → select `Path` under
   your user account → Edit → New → paste the `bin` folder path).
4. Open a new Command Prompt and confirm `ffmpeg -version` works.

### Deno (lets yt-dlp resolve YouTube links reliably)

```
winget install --id=DenoLand.Deno
```

Open a new Command Prompt and confirm `deno --version` works. If winget
isn't available, run this in PowerShell instead:

```
irm https://deno.land/install.ps1 | iex
```

## 2. Get the bot onto your PC

Download/extract this project to a permanent folder, e.g. `C:\TwitchRadioBot`
(not Desktop or Downloads — you'll be leaving `data\` and `logs\` folders
here long-term).

## 3. Run the one-time setup

Double-click **`setup.bat`** in that folder. It creates an isolated Python
environment, installs everything the bot needs, and creates a `.env` file
for your settings (copied from `deploy\.env.example`). Leave the window open
until it says "Setup finished."

## 4. Register a Twitch app and fill in `.env`

1. Go to **https://dev.twitch.tv/console/apps** (sign in with the account
   you want to *own* the app — this can be your main streamer account) and
   click **Register Your Application**.
   - **OAuth Redirect URL:** `http://localhost:4343/oauth/callback`
   - **Category:** Chat Bot
2. On the app's page, copy the **Client ID**, and click **New Secret** to
   generate a **Client Secret**.
3. Open `.env` (in the project folder) in Notepad and fill in:
   ```
   TWITCH_CLIENT_ID=<paste it>
   TWITCH_CLIENT_SECRET=<paste it>
   ```
4. You also need two **numeric** Twitch user IDs (not usernames):
   - `TWITCH_BOT_ID` — the account the bot will chat *as*. Use a second,
     dedicated Twitch account (not your main one), and make it a
     **moderator** in your channel (type `/mod <botname>` in your own chat).
   - `TWITCH_OWNER_ID` — your own (the broadcaster's) account.
   - Look both up at
     https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/
     and paste in just the digits — nothing else.
5. Save `.env`.

## 5. Check everything, then authorize the bot

Double-click **`check-config.bat`**. It should print `Config OK:` with a
summary of your settings — if it says `Config check FAILED`, fix whatever
it names and run it again.

Then double-click **`run.bat`** to start the bot for the first time. The
first start needs a one-time authorization step:

1. With the bot running, open a browser **signed into the BOT account** and
   visit:
   ```
   http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
   ```
   Approve it.
2. Open a **different browser, or an incognito/private window** (this part
   matters — reusing the same signed-in session for both steps is the most
   common mistake), sign in as **your own (broadcaster) account**, and
   visit:
   ```
   http://localhost:4343/oauth?scopes=channel:bot&force_verify=true
   ```
   Approve it.

The bot saves both authorizations to `data\twitch_tokens.json` and won't
ask again on future starts (unless you delete that file). Once both steps
are done, try `!sr` in your channel's chat — the bot should respond.

## 6. Add it to OBS

In OBS, add two new sources to your scene:

- **Media Source** → uncheck **"Local File"** → set the input to
  `http://127.0.0.1:8098/stream.mp3`
- **Browser Source** → URL `http://127.0.0.1:8098/overlay`, size it to
  taste

(Unchecking "Local File" matters — it's a continuous live stream with no
fixed length, and OBS treats that differently from a file on disk.)

## 7. Day to day

- **Start:** double-click `run.bat`.
- **Stop:** click into the bot's console window and press **Ctrl+C once**,
  then wait for it to print that it's shutting down before closing the
  window. Just closing the window (the X button) can leave background
  `ffmpeg.exe` processes running — if audio keeps playing after you've
  closed it, check Task Manager for stray `ffmpeg.exe` / `python.exe`
  processes and end them.
- **Adjust settings** (cooldowns, queue size, max song length, radio
  autoplay on/off) at `http://127.0.0.1:8098/settings` while it's running.
  With no `TWITCH_SETTINGS_PASSWORD` set in `.env`, this only works from
  this same PC. Run `hash-password.bat` if you want to set one.
- **Chat commands:** `!sr <link or search>`, `!queue`, `!position`,
  `!remove`, `!nowplaying`, `!skip` (your own song, or any song if you're a
  mod), `!voteskip` (anyone), `!radio` (anyone can check on/off, only mods
  can change it), and mod-only `!pause`/`!resume` — see
  `twitch_radio/components/song_requests.py` for the full list.

## Troubleshooting

| Problem | Try this |
|---|---|
| `Config check FAILED: ffmpeg not found on PATH` | Reinstall ffmpeg (step 1), then open a **new** Command Prompt / restart the PC before running `run.bat` again. |
| Config check warns about a JS runtime not found | Same idea, for Deno. |
| `Bad Identifiers` error from Twitch | `TWITCH_BOT_ID`/`TWITCH_OWNER_ID` must be plain digits only — no username, no label. |
| Bot runs but never responds in chat | Make sure the bot account is a **moderator** in your channel, and re-check step 5 — the two OAuth links must be approved from two *different* accounts/sessions. |
| Songs fail to resolve / "Sign in to confirm you're not a bot" | Rare on a home connection. See the `YTDLP_COOKIES_FILE` notes in `.env` if it keeps happening. |
| Bot won't start at all | Run `check-config.bat` first — it explains most misconfigurations directly. |

## What this bot doesn't do

It doesn't stream anything *to* Twitch itself — it only serves the audio +
overlay locally for OBS to pick up, exactly like any other Media/Browser
Source. It also only supports YouTube links/searches for `!sr`.
