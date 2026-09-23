"""The /login page."""

from __future__ import annotations

from html import escape

from twitch_radio.admin.assets import static_text, template


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def render_login_page(
    *,
    next_path: str,
    has_logo: bool,
    error: str | None = None,
    info: str | None = None,
    retry_after: int = 0,
    remember_days: int = 0,
) -> str:
    if retry_after > 0:
        notice = (
            '<div class="notice notice-error" id="lockout" role="alert">'
            "Too many failed attempts. Try again in "
            f'<strong id="retry-left">{_clock(retry_after)}</strong>.</div>'
        )
    elif error:
        notice = f'<div class="notice notice-error" role="alert">{escape(error)}</div>'
    elif info:
        notice = f'<div class="notice notice-info" role="status">{escape(info)}</div>'
    else:
        notice = ""

    remember = (
        '<label class="remember"><input type="checkbox" name="remember" value="1">'
        f"<span>Keep me signed in for {remember_days} day{'s' if remember_days != 1 else ''} "
        "on this device</span></label>"
        if remember_days > 0
        else ""
    )
    logo = '<img class="mark" src="/logo.png" alt="" onerror="this.remove()">' if has_logo else "<span></span>"
    return template("login.html").substitute(
        css=static_text("login.css"),
        js=static_text("login.js"),
        logo=logo,
        notice=notice,
        next=escape(next_path, quote=True),
        remember=remember,
        disabled="disabled" if retry_after > 0 else "",
        retry_seconds=retry_after,
    )
