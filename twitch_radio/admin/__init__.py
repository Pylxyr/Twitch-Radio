"""HTTP surface of the bot: audio stream, the OBS overlay and the /settings
page. Local only (binds to 127.0.0.1; see security.py for the Host and Origin
rules that protect it).

Layout:
    app.py        builds the aiohttp app (routes, middleware) — the only entry
                  point, `run_admin_server`, lives here
    context.py    the shared dependencies every handler reads
    handlers/     request handlers, grouped by concern (live data, media,
                  the settings routes)
    render/       pure functions that build the /settings HTML
    security.py   Host allow-list, CSRF, rate-limit and URL-allowlist rules (stdlib only)
    assets.py     loads static/ (overlay page, CSS, JS, HTML templates) and the logo
    static/       the front-end files themselves

Nothing is imported here on purpose: `from twitch_radio.admin.app import
run_admin_server` pulls in aiohttp, while security.py and render/ stay
importable without it.
"""
