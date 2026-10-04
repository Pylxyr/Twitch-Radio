# Twitch Radio

A song-request radio for your Twitch stream. Viewers type `!sr <song or YouTube link>` in chat, the
bot queues it, and it plays through OBS together with an on-screen "now playing" overlay.

It runs entirely on your own PC and is built to stay out of the way of your stream: it is light on
CPU and memory, serves everything on `127.0.0.1` only, and has no account, cloud service or
telemetry.

| | |
|---|---|
| **Runs on** | Windows 10/11 (desktop app or from source); Linux from source or AppImage |
| **Listens on** | `127.0.0.1` only. Nothing is reachable from your network |
| **Connects out to** | Twitch (chat), YouTube (songs), GitHub (update checks). Details [below](#privacy-and-network-use) |
| **Needs** | A free Twitch developer app, a second Twitch account for the bot to chat as, and OBS |

---

## Contents

1. [Quick start (desktop app)](#quick-start-desktop-app)
2. [Connect it to Twitch](#connect-it-to-twitch)
3. [Add it to OBS](#add-it-to-obs)
4. [Chat commands](#chat-commands)
5. [Settings](#settings)
6. [Resource use](#resource-use)
7. [Running from source](#running-from-source)
8. [Where your data lives](#where-your-data-lives)
9. [Privacy and network use](#privacy-and-network-use)
10. [Troubleshooting](#troubleshooting)
11. [Development](#development)

---

## Quick start (desktop app)

1. Download the installer from the [Releases page](https://github.com/Pylxyr/Twitch-Radio/releases/latest)
   (`Twitch Radio Setup x.y.z.exe`) and run it. Nothing else needs to be installed: ffmpeg is
   bundled and the app brings what it needs for YouTube.
2. Open **Twitch Radio**, go to **Settings**, and fill in the Twitch section (next chapter).
3. Press **Start** on the dashboard.

Closing the window while the radio is running can keep it going in the system tray. The window is
actually closed in that case, which frees its memory; click the tray icon to bring it back.

---

## Connect it to Twitch

You need two Twitch accounts: **yours** (the channel) and a **bot account** the bot chats as. A
dedicated bot account is best; make it a moderator in your channel (`/mod <botname>`).

1. Go to <https://dev.twitch.tv/console/apps> and click **Register Your Application**.
   - OAuth Redirect URL: `http://localhost:4343/oauth/callback`
   - Category: `Chat Bot`
2. Copy the **Client ID** and click **New Secret** to get a **Client Secret**.
3. Find the two numeric user IDs (not names) with
   <https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/>.
4. In the app's **Settings** tab enter: Client ID, Client Secret, **Bot account ID** (the bot's
   account) and **Broadcaster ID** (yours). Press **Save**.
5. Press **Start**, then authorize both accounts. The dashboard's setup checklist has buttons for
   these two links:
   - **As the bot account:**
     `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true`
   - **As your own account**, in a *different* browser or a private window:
     `http://localhost:4343/oauth?scopes=channel:bot&force_verify=true`

> **Common mistake:** approving both links in the same signed-in browser. Use two browsers, or one
> normal and one private window.

Then type `!sr` in your chat. Authorization is a one-time step; the tokens are stored locally and
refreshed automatically.

---

## Add it to OBS

Add two sources to your scene (the dashboard shows the exact addresses with copy buttons):

| Source type | Value |
|---|---|
| **Media Source** (untick *Local File*) | `http://127.0.0.1:8098/stream.opus` |
| **Browser Source** | `http://127.0.0.1:8098/overlay` |

Notes:
- The audio is one continuous live stream. While OBS is connected and nothing is playing you hear
  silence rather than a disconnect.
- The overlay page has a transparent background and is about 420 px wide; size it in OBS as you like.

The port can be changed in **Settings > Network** if `8098` is taken.

---

## Chat commands

| Command | Who | What it does |
|---|---|---|
| `!sr <song or link>` (`!songrequest`) | Everyone | Queue a YouTube song or search by name |
| `!queue` | Everyone | Show what's coming up |
| `!position` (`!pos`) | Everyone | Where your own request is in the queue |
| `!remove` (`!cancel`, `!unqueue`) | Everyone | Remove your own queued request |
| `!nowplaying` (`!np`) | Everyone | What's playing now |
| `!voteskip` (`!vs`) | Everyone | Vote to skip; needs a few unique voters |
| `!skip` | Mods, or whoever requested the current song | Skip the current song |
| `!radio` | Everyone to check, mods to change (`!radio on` / `!radio off`) | Auto-radio: when the queue runs dry, queue a related song instead of going quiet |
| `!pause`, `!resume` | Mods | Pause or resume playback |
| `!block`, `!unblock`, `!blocklist` | Mods | Keep a video out of the radio |

---

## Settings

Everything is under the **Settings** tab of the app. If you run from source, edit `.env` (every
option is documented in [`.env.example`](.env.example)) or use the web page at
`http://127.0.0.1:8098/settings` for the live limits.

**Limits that apply immediately, with no restart** (Settings > Live limits, or the web page):
maximum pending requests per viewer, request cooldown, queue size cap, maximum track length,
vote-skip threshold, and auto-radio on/off.

**Settings that need a restart**

| Setting | Default | What it does |
|---|---|---|
| Audio bitrate | `160` kbps | Opus quality of the stream sent to OBS. 160 is already transparent for nearly everything. |
| Volume levelling (`LOUDNESS_MODE`) | `static` | `static`: each song is measured once and given a fixed gain plus a limiter (lightest on CPU and memory, typically within 1 dB of dynamic). `dynamic`: ffmpeg's `loudnorm` filter on the whole stream (most exact, about ten times the CPU). `off`: songs play at their own volume. A song that can't be measured falls back to dynamic. |
| Pause when nobody is listening | off | Hold the queue while OBS isn't connected, so songs don't quietly play through while you're offline. |
| Port | `8098` | The local web server's port. |
| Parallel lookups | `2` | The most YouTube lookups that may run at once. |
| Lookup process idle exit | `120` s | How long an unused lookup process stays loaded before it exits and frees its memory. |
| Lookup timeout / result cache | `45` s / `900` s | How long a lookup may take; how long a resolved song is reused (`0` also turns off next-song prefetching). |
| YouTube cookies | none | Only for age-restricted or members-only videos, or if YouTube insists on a sign-in. Import a Netscape-format `cookies.txt` in Settings. |

---

## Resource use

Twitch Radio is designed to cost almost nothing while you stream.

- **Nobody listening, nothing playing:** the audio encoder is not running and the bot wakes only
  when something happens. The Python process is well under 100 MB.
- **Playing:** one small decoder process per song plus one Opus encoder (only while OBS or a
  browser is connected to `/stream.opus`). The next song is prepared ahead of time so the hand-over
  is gapless.
- **Looking up songs:** YouTube lookups run in separate processes that are started when a request
  needs them and exit after two idle minutes, so they use memory only while `!sr` is active.
- **Desktop app window:** when the window is closed to the tray it is really closed (no hidden
  browser processes), and the bot reports to the app only when something changes. While the
  dashboard is open it adds one update every few seconds.

The dashboard has no CPU/memory graphs: Task Manager shows the same numbers without the app
spending resources to draw them. It does show the event-loop delay, the one number that predicts
audio stutter. To measure the whole app (including the helper processes), use Task Manager's
*Details* tab or `scripts/probe.py` (see [Development](#development)).

---

## Running from source

For Windows, with the batch files:

1. Install the prerequisites (a *new* Command Prompt is needed after each):
   ```
   winget install --id=Gyan.FFmpeg -e
   winget install --id=DenoLand.Deno -e
   ```
   and Python 3.11 or newer from <https://www.python.org/downloads/>. **Tick "Add python.exe to
   PATH"** on the first installer screen.
2. Extract or clone the project to a permanent folder such as `C:\TwitchRadioBot`.
3. Double-click **`setup.bat`**. It creates a virtual environment, installs the dependencies and
   creates `.env` from `.env.example`.
4. Fill in the four Twitch values in `.env` (see [Connect it to Twitch](#connect-it-to-twitch)).
5. Double-click **`check-config.bat`**. It should print `Config OK:`; otherwise it names what to fix.
6. Double-click **`run.bat`** to start the bot in a console, or **`run-gui.bat`** for the desktop app
   (needs Node.js 22 or newer). Stop with **Ctrl+C** and wait for the console to finish before
   closing it.

On Linux or macOS, the same steps apply by hand: `python3 -m venv .venv`, `pip install -r
requirements.txt`, `cp .env.example .env`, then `python3 bot.py`. ffmpeg must be on your `PATH`.

To build the installer yourself, see [BUILD.md](BUILD.md).

---

## Where your data lives

| | Desktop app | From source |
|---|---|---|
| Settings (`.env`) | `%APPDATA%\TwitchRadio\.env` | `.env` in the project folder |
| Tokens, queue, limits, block list, yt-dlp cache | `%APPDATA%\TwitchRadio\data\` | `data\` in the project folder |
| Logs | `%APPDATA%\TwitchRadio\logs\` | `logs\` in the project folder |
| Window preferences | `%APPDATA%\TwitchRadio\gui\` | same |

Settings > This app has buttons that open the data and log folders and show the `.env` file. Uninstalling the app leaves them in place
so a reinstall keeps your setup; delete the folder to remove everything. The Twitch tokens in
`data\twitch_tokens.json` grant access to your bot account, so treat that file like a password.

---

## Privacy and network use

There is no telemetry, no account and no server of ours. The bot makes these connections, and
nothing else:

| To | Why |
|---|---|
| `*.twitch.tv` | Chat (EventSub websocket and the Helix API) and the one-time sign-in |
| `*.youtube.com`, `*.googlevideo.com`, `*.ytimg.com` | Finding songs, streaming the audio, thumbnails for the overlay |
| `api.github.com`, `github.com` | The desktop app's daily check for a new version (switch off in Settings > Updates); the optional yt-dlp update check and install; and yt-dlp's JavaScript solver component, which it downloads once from the yt-dlp project |

The local web server accepts connections from this computer only. It rejects any request whose
`Host` header is not `127.0.0.1`, `localhost` or `[::1]`, which blocks a web page from reaching it
through DNS rebinding, and it refuses cross-site requests to change settings. Port `4343` is also
open on loopback while the bot runs: that is Twitch's sign-in redirect target.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "ffmpeg not found" | From source: install it (see above) and open a *new* Command Prompt. The desktop app bundles its own. |
| "JS runtime / Deno not found" | From source: `winget install --id=DenoLand.Deno -e`, then a new Command Prompt. |
| "Bad Identifiers" from Twitch | `TWITCH_BOT_ID` and `TWITCH_OWNER_ID` must be digits only. |
| The bot doesn't answer in chat | The bot account must be a moderator in your channel, and both sign-in links must have been approved, on two different accounts. |
| Nothing is heard in OBS | The Media Source must use the `stream.opus` address with *Local File* unticked. Open the same address in a browser to check that the bot is producing sound. |
| The first `!sr` after a quiet spell takes a moment longer | The lookup process was idle and has to start again (about a second). That is the memory saving at work. |
| Songs won't resolve | Update yt-dlp from Settings > Updates. If YouTube asks for a sign-in, see *YouTube cookies* above. |
| Port in use at startup | Change the port in Settings > Network, and update the OBS sources to match. |
| Nothing starts | Run `check-config.bat` (source) or look at the **Logs** tab (app). |
| Audio keeps playing after closing the console | Close stray `ffmpeg.exe` / `python.exe` processes in Task Manager; always stop with Ctrl+C. |

---

## Development

```
pip install -r requirements-dev.txt -r requirements.txt
ruff check .
ruff format --check .
mypy twitch_radio bot.py
pytest -q

cd gui && npm ci && npm run lint && npm run check && npm test && npm run check:build
```

The player tests run the real pipeline against a local HTTP server and need `ffmpeg` on `PATH`
(they are skipped without it).

`scripts/probe.py` measures a running install (CPU and memory of the whole process tree, ffmpeg
cost on your machine, yt-dlp timings, loudness accuracy) without changing anything. It needs
`pip install psutil` (the bot itself does not): `python scripts/probe.py --help`.

See [BUILD.md](BUILD.md) for building the installer, the CI workflows, cutting a release, and how
the yt-dlp and app update checks work, and [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules.

## What this doesn't do

It doesn't stream to Twitch by itself: it feeds OBS locally, like any Media or Browser Source. Song
requests support YouTube only.

## License

Public domain under the [Unlicense](LICENSE). Bundled third-party software keeps its own licences;
see `THIRD_PARTY_NOTICES.txt`.
