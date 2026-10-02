# Contributing

## Development

```
pip install -r requirements-dev.txt -r requirements.txt
ruff check .
ruff format --check .
mypy twitch_radio bot.py          # disallow_untyped_defs is on
pytest -q

cd gui
npm ci
npm run lint
npm run check
npm test
npm run check:build
```

CI runs the same commands. Please also run them before opening a pull request.

## Ground rules

* The architecture stays: Electron spawns the Python core as a hidden child and they speak JSON
  lines on stdin/stdout (protocol at the top of `twitch_radio/headless.py`).
* Renderer security stays strict: context isolation and sandbox on, no Node integration, strict CSP
  (`connect-src 'none'`), no `innerHTML`, and a preload that exposes named functions only. All
  network access happens in the main process or in the core, never in the page.
* Pins are exact in `requirements*.txt` and `gui/package.json`. When you bump yt-dlp, re-read its
  `pin` / `pin-curl-cffi` extras (`pip show yt-dlp`, its `pyproject.toml`) and update the companion
  pins in `requirements.txt` to match. Do not add the `yt-dlp-ejs` package.
* Version numbers: use `python scripts/bump-version.py X.Y.Z`; never edit the three places by hand.
* Comments: only for a non-obvious "why".
