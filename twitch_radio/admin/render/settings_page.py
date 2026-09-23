"""The /settings page. Pure functions — plain values in, an HTML string out —
so the page can be rendered and tested without a running server.

The page is assembled from the same metadata the rest of the service uses
(TUNABLE_BOUNDS/TUNABLE_LABELS, TOGGLE_KEYS) rather than hand-written inputs,
so adding a tunable or a toggle shows up here automatically. The surrounding
markup, CSS and JS are static files (see admin/static/settings.*); this
module only fills in the ${placeholders}.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from twitch_radio.admin.assets import static_text, template
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TUNABLE_LABELS, TwitchTunables

# Hidden field present only in the /settings form this module renders. The POST
# handler uses it to tell a browser submitting the full form (unchecked boxes
# mean "off") from a partial scripted POST (absent keys mean "leave alone").
FORM_MARKER = "_settings_form"


# Only offered when a password is set (otherwise there is no session to end).
_SIGNOUT_FORM = '<form class="signout" method="post" action="/logout"><button type="submit">Sign out</button></form>'


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """What the header chips show on first paint; from then on the page's own
    script keeps them current over /ws/nowplaying."""

    state: str
    queue_size: int
    uptime_seconds: int
    now_playing_title: str | None


def _tunable_rows(tunables: TwitchTunables) -> str:
    rows = []
    for name, (lo, hi) in TUNABLE_BOUNDS.items():
        label, help_text = TUNABLE_LABELS.get(name, (name, ""))
        value = getattr(tunables, name)
        rows.append(
            f'<div class="field">'
            f'<label for="f-{escape(name)}">{escape(label)}</label>'
            f'<input id="f-{escape(name)}" type="number" name="{escape(name)}" '
            f'value="{value}" min="{lo}" max="{hi}" step="1" inputmode="numeric">'
            f'<p class="help">{escape(help_text)} <span class="range">{lo}\u2013{hi}</span></p>'
            f'</div>'
        )
    return "".join(rows)


def _toggle_rows(toggles: FeatureToggles) -> str:
    rows = []
    for key, desc in TOGGLE_KEYS.items():
        on = "checked" if getattr(toggles, key) else ""
        rows.append(
            f'<label class="switch-row">'
            f'<input type="checkbox" name="{escape(key)}" {on}>'
            f'<span class="switch" aria-hidden="true"></span>'
            f'<span class="switch-text"><code>{escape(key)}</code>'
            f'<span class="help">{escape(desc)}</span></span>'
            f'</label>'
        )
    return "".join(rows)


def _status_chips(status: LiveStatus) -> str:
    """chip-np always renders (possibly hidden) rather than being conditionally
    included, so the live script only ever updates an existing element's
    text/visibility instead of inserting and removing nodes."""
    hours, rem = divmod(status.uptime_seconds, 3600)
    uptime_text = f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"
    title = status.now_playing_title
    np_style = "" if title is not None else "display:none"
    np_text = f"\u25b6 {escape(title)}" if title is not None else ""
    np_title_attr = escape(title) if title is not None else ""
    return (
        f'<span class="chip state-{escape(status.state)}" id="chip-state">{escape(status.state)}</span>'
        f'<span class="chip" id="chip-queue">{status.queue_size} queued</span>'
        f'<span class="chip" id="chip-uptime" data-uptime-base="{status.uptime_seconds}">up {uptime_text}</span>'
        f'<span class="chip chip-np" id="chip-np" style="{np_style}" title="{np_title_attr}">{np_text}</span>'
    )


def render_settings_page(
    *,
    tunables: TwitchTunables,
    toggles: FeatureToggles,
    broadcast_info: dict[str, str],
    status: LiveStatus,
    has_logo: bool,
    can_sign_out: bool = False,
    message: str | None = None,
    error: bool = False,
) -> str:
    info_rows = "".join(
        f"<tr><td>{escape(k)}</td><td><code>{escape(v)}</code></td></tr>" for k, v in broadcast_info.items()
    )
    banner = ""
    if message:
        kind = "banner-error" if error else "banner-ok"
        banner = f'<div class="banner {kind}" role="status">{escape(message)}</div>'
    logo = '<img class="mark" src="/logo.png" alt="" onerror="this.remove()">' if has_logo else ""
    return template("settings.html").substitute(
        css=static_text("settings.css"),
        js=static_text("settings.js"),
        logo=logo,
        signout=_SIGNOUT_FORM if can_sign_out else "",
        status_chips=_status_chips(status),
        banner=banner,
        form_marker=FORM_MARKER,
        tunable_rows=_tunable_rows(tunables),
        toggle_rows=_toggle_rows(toggles),
        info_rows=info_rows,
    )
