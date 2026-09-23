"""Sign-in (/login) and sign-out (/logout)."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from twitch_radio.admin.context import AdminContext, client_ip, get_ctx
from twitch_radio.admin.handlers.auth import (
    authorize,
    clear_session_cookie,
    is_signed_in,
    origin_ok,
    protect,
    redirect,
    session_token,
    set_session_cookie,
)
from twitch_radio.admin.passwords import verify_password
from twitch_radio.admin.render.login_page import render_login_page
from twitch_radio.admin.security import safe_next_path

log = logging.getLogger(__name__)

_LOGIN_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "script-src 'unsafe-inline'; "
    "img-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _remember_days(ctx: AdminContext) -> int:
    return int(ctx.sessions.remember_seconds // 86400)


def _page(
    ctx: AdminContext,
    request: web.Request,
    *,
    next_path: str,
    status: int = 200,
    error: str | None = None,
    info: str | None = None,
) -> web.Response:
    retry_after = ctx.auth_limiter.retry_after(client_ip(request))
    html = render_login_page(
        next_path=next_path,
        has_logo=ctx.logo is not None,
        error=error,
        info=info,
        retry_after=retry_after,
        remember_days=_remember_days(ctx),
    )
    response = protect(web.Response(text=html, content_type="text/html", status=status))
    response.headers["Content-Security-Policy"] = _LOGIN_CSP
    if retry_after > 0:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _nothing_to_sign_in_to(ctx: AdminContext, request: web.Request, next_path: str) -> web.Response:
    denied = authorize(ctx, request)
    return denied if denied is not None else redirect(next_path)


async def handle_login_get(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    next_path = safe_next_path(request.query.get("next"))
    if ctx.settings_password is None:
        return _nothing_to_sign_in_to(ctx, request, next_path)
    if is_signed_in(ctx, request):
        return redirect(next_path)
    info = "You've been signed out." if request.query.get("signed_out") else None
    return _page(ctx, request, next_path=next_path, info=info)


async def handle_login_post(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    form = await request.post()
    next_path = safe_next_path(str(form.get("next", "")))
    if ctx.settings_password is None:
        return _nothing_to_sign_in_to(ctx, request, next_path)
    if not origin_ok(request):
        return protect(web.Response(status=403, text="Origin check failed — refusing to sign in."))

    ip = client_ip(request)
    if ctx.auth_limiter.is_blocked(ip):
        return _page(ctx, request, next_path=next_path, status=429)

    password = form.get("password")
    # Worker thread: a scrypt check would otherwise stall the audio feed.
    if not isinstance(password, str) or not await asyncio.to_thread(
        verify_password, password, ctx.settings_password
    ):
        ctx.auth_limiter.record_failure(ip)
        log.warning("Failed /login attempt from %s", ip)
        return _page(ctx, request, next_path=next_path, status=401, error="Incorrect password.")

    ctx.auth_limiter.record_success(ip)
    ctx.sessions.destroy(session_token(request))
    remember = bool(form.get("remember")) and ctx.sessions.remember_seconds > 0
    token = ctx.sessions.create(remember=remember)
    response = redirect(next_path)
    set_session_cookie(
        response, request, token, max_age=int(ctx.sessions.remember_seconds) if remember else None
    )
    log.info("Signed in to /settings from %s (%s)", ip, "remembered" if remember else "this session")
    return response


async def handle_logout(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    if not origin_ok(request):
        return protect(web.Response(status=403, text="Origin check failed — refusing to sign out."))
    ctx.sessions.destroy(session_token(request))
    response = redirect("/login?signed_out=1")
    clear_session_cookie(response)
    return response

