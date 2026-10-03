# Twitch Radio — Windows Setup

> **Desktop app available.** This project can also be built into a double-click Windows app (and a
> Linux AppImage, see BUILD.md) with a
> dashboard, live log viewer, Start/Stop buttons and a Settings screen, with ffmpeg bundled
> (no terminal, no manual installs for end users). See [BUILD.md](BUILD.md). The steps below still
> work if you prefer running from source.

Twitch Radio is a song-request radio for your Twitch stream. Viewers type `!sr <song>` in
chat, it queues the song, and it feeds audio + an on-screen overlay into
OBS. Everything runs on your own PC.

| | |
|---|---|
| **Time needed** | About 15 minutes |
| **Runs on** | This PC only — nothing is uploaded anywhere |
| **Talks to** | Twitch (chat) and YouTube (songs) |

---

## What you'll install

| Tool | What it's for |
|---|---|
| **winget** | Windows' built-in installer — gets you the next two |
| **ffmpeg** | Decodes the audio |
| **Deno** | Lets the bot resolve YouTube links |
| **Python 3.11+** | Runs the bot itself |

---

## Step 1 — Install the prerequisites

**Check winget first.** Open Command Prompt and run:

```
winget --version
```

- Prints a version number → move on to ffmpeg below.
- Says "not recognized" → open **Microsoft Store**, search **App
  Installer**, click **Get** (or **Update**). Reopen Command Prompt and
  try again.
- Still not working → download the installer from
  https://github.com/microsoft/winget-cli/releases/latest and run it.

> If winget just won't cooperate, that's fine — every step below also has
> a manual install option.

**Install ffmpeg:**

```
winget install --id=Gyan.FFmpeg -e
```

Open a *new* Command Prompt and run `ffmpeg -version` to confirm. If it's
not found, restart your PC and try again — or install manually:
download from https://www.gyan.dev/ffmpeg/builds/, extract to `C:\ffmpeg`,
and add `C:\ffmpeg\bin` to your PATH.

**Install Deno:**

```
winget install --id=DenoLand.Deno
```

Confirm with `deno --version` in a new Command Prompt. No winget? Run this
in PowerShell instead: `irm https://deno.land/install.ps1 | iex`

**Install Python:**

Download from https://www.python.org/downloads/ and run the installer.

> **Don't skip this:** tick **"Add python.exe to PATH"** on the very first
> install screen. It's the single most common setup mistake.

Confirm with `python --version` in a new Command Prompt.

---

## Step 2 — Get the project onto your PC

Extract this project to a permanent folder — for example `C:\TwitchRadioBot`.
Not Desktop, not Downloads: the bot keeps its data here long-term.

---

## Step 3 — Run setup

Double-click **`setup.bat`**. It installs everything the bot needs and
creates a `.env` file for your settings. Wait for **"Setup finished."**

---

## Step 4 — Register a Twitch app

1. Go to https://dev.twitch.tv/console/apps and click **Register Your
   Application**.
   - Redirect URL: `http://localhost:4343/oauth/callback`
   - Category: `Chat Bot`
2. Copy the **Client ID**, then click **New Secret** for a **Client
   Secret**.
3. Open `.env` in Notepad and fill in:
   ```
   TWITCH_CLIENT_ID=<paste it>
   TWITCH_CLIENT_SECRET=<paste it>
   ```
4. Get two numeric Twitch IDs from
   https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/
   and add them to `.env`:
   - `TWITCH_BOT_ID` — a second Twitch account the bot chats *as*. Make it
     a moderator in your channel first (`/mod <botname>`).
   - `TWITCH_OWNER_ID` — your own account.
5. Save the file.

---

## Step 5 — Connect the bot

Double-click **`check-config.bat`**. It should say `Config OK:`. If not,
fix whatever it names and run it again.

Then double-click **`run.bat`**. First time only, authorize two accounts:

1. **As the bot account**, in a browser, visit:
   ```
   http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
   ```
2. **As your own account**, in a *different* browser or an incognito
   window, visit:
   ```
   http://localhost:4343/oauth?scopes=channel:bot&force_verify=true
   ```

> **Common mistake:** using the same signed-in browser for both. Use two
> separate browsers, or one normal + one incognito window.

That's it — try `!sr` in your chat.

---

## Step 6 — Add it to OBS

Add two sources to your scene:

| Source type | Value |
|---|---|
| **Media Source** (uncheck "Local File") | `http://127.0.0.1:8098/stream.opus` |
| **Browser Source** | `http://127.0.0.1:8098/overlay` |

---

## Using it

- **Start:** double-click `run.bat`.
- **Stop:** click the console window, press **Ctrl+C**, and wait for it
  to exit before closing the window.

  > Closing the window directly can leave `ffmpeg.exe` running in the
  > background. If audio keeps playing after you've stopped the bot,
  > check Task Manager for stray `ffmpeg.exe` / `python.exe` processes.

- **Settings** (cooldowns, queue size, radio autoplay): visit
  `http://127.0.0.1:8098/settings` while it's running — see below for
  whether you need a password for it.
- **Chat commands:** `!sr`, `!queue`, `!position`, `!remove`,
  `!nowplaying`, `!skip`, `!voteskip`, `!radio` — mods also get
  `!pause`/`!resume`.

---

## Optional: password-protect `/settings`

By default `/settings` has no password, and that's fine as long as the
bot, OBS, and the browser you use to open `/settings` all stay on this
one PC — the server only accepts connections from itself.

| If you want to... | Do this in `.env` |
|---|---|
| Keep things as they are (recommended) | Leave `TWITCH_SETTINGS_PASSWORD` and `TWITCH_NOWPLAYING_HOST` blank |
| Reach `/settings` from your phone or another PC on the same network | Set `TWITCH_NOWPLAYING_HOST=0.0.0.0`, **and** set a password (below) |

The moment `/settings` is reachable off this PC, the bot requires a
password and simply refuses to serve the page without one — it won't
silently run unprotected.

**To set a password:** double-click **`hash-password.bat`**, type a
password when asked, and copy the `scrypt:...` line it prints into `.env`:

```
TWITCH_SETTINGS_PASSWORD=scrypt:...
```

This stores a hash, not the password itself — safer than pasting the
plain password into `.env` directly (which also works, just isn't as
safe if someone else can open that file).

There's also `TWITCH_SETTINGS_ALLOW_OPEN=true`, which lets `/settings`
run with no password even when reachable off this PC. Only use that on a
network you fully trust — anyone on it could change your settings.

---

## Optional: a few other `.env` settings

| Setting | What it does |
|---|---|
| `AUDIO_BITRATE_KBPS` | Stream quality (Opus), default `160`. That's already comfortably transparent — no real reason to go above `192`. |
| `PAUSE_QUEUE_WHEN_NO_LISTENERS` | Set to `true` so the queue stops advancing while OBS isn't connected — songs won't quietly play through while you're offline. |
| `TWITCH_NOWPLAYING_PORT` | Change if `8098` is already used by something else on this PC. |

Everything else in `.env` has a comment above it explaining what it's
for — safe to leave alone unless you have a specific reason to change it.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| ffmpeg not found | Reinstall it (Step 1), open a *new* Command Prompt, try again |
| JS runtime / Deno not found | Same idea, for Deno |
| "Bad Identifiers" error | `TWITCH_BOT_ID` / `TWITCH_OWNER_ID` must be numbers only |
| Bot doesn't respond in chat | Confirm the bot account is a moderator, and that both OAuth links (Step 5) were approved on separate accounts |
| Songs won't resolve | Rare on a home connection — see `YTDLP_COOKIES_FILE` in `.env` |
| Nothing starts | Run `check-config.bat` — it explains most problems directly |

---

## What this bot doesn't do

It doesn't stream to Twitch by itself — it only feeds OBS locally, like
any Media/Browser Source. Song requests support YouTube only.

## Development

```
pip install -r requirements-dev.txt -r requirements.txt
ruff check .
ruff format --check .
mypy twitch_radio bot.py
pytest -q

cd gui && npm ci && npm run lint && npm run check && npm test && npm run check:build
```

See `BUILD.md` for building the installer, the CI workflows, cutting a release and how the yt-dlp
and app update checks work, and `CONTRIBUTING.md` for the ground rules.

## License

Public domain under the [Unlicense](LICENSE). Bundled third-party software keeps its own licences; see `THIRD_PARTY_NOTICES.txt`.
