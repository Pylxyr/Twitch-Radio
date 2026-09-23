"""The sign-in-gated routes: /settings (view and save)."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from twitch_radio.admin.context import AdminContext, client_ip, get_ctx
from twitch_radio.admin.handlers.auth import authorize, origin_ok, protect
from twitch_radio.admin.render.settings_page import FORM_MARKER, LiveStatus, render_settings_page
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str) -> bool:
    """For a toggle named explicitly in a partial (non-browser) POST. A bare
    HTML checkbox submits the literal string "on", so that has to count as
    true, but an explicit `radio_autoplay_enabled=false` from a script should
    mean what it says rather than "present, therefore on"."""
    return value.strip().lower() in _TRUTHY


def _parse_tunables(form: Mapping[str, Any]) -> tuple[dict[str, int], list[str]]:
    """Validate every submitted tunable against its bounds. Fields absent from
    the form are simply not part of the update."""
    submitted: dict[str, int] = {}
    errors: list[str] = []
    for name, (lo, hi) in TUNABLE_BOUNDS.items():
        raw = form.get(name)
        if raw is None:
            continue
        try:
            value = int(str(raw))
        except ValueError:
            errors.append(f"{name}: not a number")
            continue
        if value < lo or value > hi:
            errors.append(f"{name}: must be between {lo} and {hi}")
            continue
        submitted[name] = value
    return submitted, errors


def _live_status(ctx: AdminContext) -> LiveStatus:
    np = ctx.player.now_playing
    return LiveStatus(
        state=ctx.player.state.value,
        queue_size=ctx.player.queue_size(),
        uptime_seconds=int(time.monotonic() - ctx.started_at),
        now_playing_title=np.title if np is not None else None,
    )


async def _page_response(
    ctx: AdminContext,
    *,
    tunables: TwitchTunables,
    toggles: FeatureToggles,
    message: str | None = None,
    error: bool = False,
    status: int = 200,
) -> web.Response:
    html = render_settings_page(
        tunables=tunables,
        toggles=toggles,
        broadcast_info=ctx.broadcast_info,
        status=_live_status(ctx),
        has_logo=ctx.logo is not None,
        can_sign_out=ctx.settings_password is not None,
        message=message,
        error=error,
    )
    return protect(web.Response(text=html, content_type="text/html", status=status))


async def handle_settings_get(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    denied = authorize(ctx, request)
    if denied is not None:
        return denied
    return await _page_response(
        ctx,
        tunables=TwitchTunables.from_dict(await ctx.tunables_store.read()),
        toggles=FeatureToggles.from_dict(await ctx.toggles_store.read()),
    )


async def handle_settings_post(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    denied = authorize(ctx, request)
    if denied is not None:
        return denied
    if not origin_ok(request):
        return protect(web.Response(status=403, text="Origin check failed — refusing to save."))
    form = await request.post()

    # Validate every tunable before writing anything, so one bad number
    # can't leave the two stores disagreeing, or a 400 come back after
    # something already changed.
    submitted, errors = _parse_tunables(form)
    if errors:
        current = TwitchTunables.from_dict(await ctx.tunables_store.read())
        return await _page_response(
            ctx,
            tunables=TwitchTunables.from_dict({**current.to_dict(), **submitted}),
            toggles=FeatureToggles.from_dict(await ctx.toggles_store.read()),
            message="Nothing was saved — " + "; ".join(errors),
            error=True,
            status=400,
        )

    def _mutate_tunables(current: dict[str, Any]) -> dict[str, Any]:
        return {**TwitchTunables.from_dict(current).to_dict(), **submitted}

    # Absent-means-unchecked is right for this page's own form, but
    # catastrophic for a non-browser caller (curl, a Stream Deck script,
    # allowed through by origin_ok) — POSTing just `queue_cap=100` would
    # silently switch off every toggle it didn't mention. FORM_MARKER is a
    # hidden field only this page's form carries, so a partial POST updates
    # only the toggles it actually names.
    full_form = form.get(FORM_MARKER) is not None

    def _mutate_toggles(current: dict[str, Any]) -> dict[str, Any]:
        toggles = FeatureToggles.from_dict(current)
        for key in TOGGLE_KEYS:
            present = form.get(key) is not None
            if full_form:
                setattr(toggles, key, present)
            elif present:
                setattr(toggles, key, _is_truthy(str(form.get(key))))
        return toggles.to_dict()

    tunables_result = await ctx.tunables_store.update(_mutate_tunables)
    toggles_result = await ctx.toggles_store.update(_mutate_toggles)
    log.info(
        "Settings updated via /settings from %s: tunables=%s toggles=%s",
        client_ip(request),
        tunables_result,
        toggles_result,
    )
    return await _page_response(
        ctx,
        tunables=TwitchTunables.from_dict(tunables_result),
        toggles=FeatureToggles.from_dict(toggles_result),
        message="Saved.",
    )
