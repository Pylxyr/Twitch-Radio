"""HTTP surface of the bot: audio stream, the OBS overlay, and the sign-in-
gated /settings page.

Layout:
    app.py        builds the aiohttp app (routes, middleware) — the only entry
                  point, `run_admin_server`, lives here
    context.py    the shared dependencies every handler reads, plus the
                  per-request helpers that need them (client address, HTTPS)
    handlers/     request handlers, grouped by concern (live data, media,
                  login/logout, the gated settings routes)
    render/       pure functions that build the /settings and /login HTML
    security.py   CSRF, redirect-safety, rate-limit and URL-allowlist rules (stdlib only)
    passwords.py  scrypt hashing and verification for the login password (stdlib only)
    sessions.py   the in-memory session store behind the login cookie (stdlib only)
    assets.py     loads static/ (overlay page, CSS, JS, HTML templates) and the logo
    static/       the front-end files themselves

Nothing is imported here on purpose: `from twitch_radio.admin.app import
run_admin_server` pulls in aiohttp, while security.py, passwords.py,
sessions.py and render/ stay importable without it.
"""
