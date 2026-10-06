"""The web surface is local only: loopback bind, Host allow-list, and an Origin
check on the one route that changes anything."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from twitch_radio.admin import security
from twitch_radio.admin.app import BIND_HOST, run_admin_server
from twitch_radio.admin.context import CTX_KEY
from twitch_radio.player import RadioPlayer
from twitch_radio.store import JsonStore


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


async def serve(tmp_path: Path) -> tuple[Any, int]:
    async def resolver(query: str, requester_id: int) -> None:
        return None

    port = free_port()
    runner = await run_admin_server(
        player=RadioPlayer(resolver=resolver, audio_bitrate_kbps=96),
        tunables_store=JsonStore(tmp_path / "tunables.json"),
        toggles_store=JsonStore(tmp_path / "toggles.json"),
        broadcast_info={"Audio stream": "/stream.opus"},
        port=port,
    )
    return runner, port


def test_host_allow_list() -> None:
    for host in ("127.0.0.1:8098", "localhost:8098", "[::1]:8098", "LOCALHOST:8098"):
        assert security.host_allowed(host, 8098), host
    for host in (
        None,
        "",
        "evil.example:8098",
        "127.0.0.1",
        "127.0.0.1:9999",
        "192.168.1.5:8098",
        "localhost.evil.com:8098",
    ):
        assert not security.host_allowed(host, 8098), host


def test_server_binds_to_loopback_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, port = await serve(tmp_path)
        try:
            assert BIND_HOST == "127.0.0.1"
            sockets = runner.addresses
            assert sockets and all(addr[0] == "127.0.0.1" for addr in sockets), sockets
        finally:
            await runner.cleanup()

    run(scenario())


def test_requests_for_other_hostnames_are_refused(tmp_path: Path) -> None:
    """What a DNS-rebinding page looks like: the right address, someone else's name."""

    async def scenario() -> None:
        runner, port = await serve(tmp_path)
        try:
            async with aiohttp.ClientSession() as http:
                for path in ("/healthz", "/nowplaying.json", "/settings", "/overlay"):
                    async with http.get(
                        f"http://127.0.0.1:{port}{path}", headers={"Host": f"evil.example:{port}"}
                    ) as resp:
                        assert resp.status == 421, path
                async with http.get(f"http://127.0.0.1:{port}/healthz") as resp:
                    assert resp.status == 200
                async with http.get(
                    f"http://127.0.0.1:{port}/healthz", headers={"Host": f"localhost:{port}"}
                ) as resp:
                    assert resp.status == 200
        finally:
            await runner.cleanup()

    run(scenario())


def test_stream_and_socket_refuse_cross_site_pages(tmp_path: Path) -> None:
    """A web page open in the streamer's browser must not be able to start the
    encoder or read the queue; OBS (no Origin header) and the overlay page
    itself (same origin) must keep working."""

    async def scenario() -> None:
        runner, port = await serve(tmp_path)
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as http:
                for path in ("/stream.opus", "/ws/nowplaying"):
                    for headers in (
                        {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Referer": "https://evil.example/page"},
                    ):
                        async with http.get(f"{base}{path}", headers=headers) as resp:
                            assert resp.status == 403, (path, headers)
                assert runner.app is not None
                player = runner.app[CTX_KEY].player
                assert player.listener_count == 0, "a refused request must not subscribe a listener"

                # The overlay's own socket: same origin, as a browser labels it.
                async with http.ws_connect(
                    f"{base}/ws/nowplaying", headers={"Origin": base, "Sec-Fetch-Site": "same-origin"}
                ) as ws:
                    assert (await ws.receive_json())["playing"] is False
                # No Origin at all (what OBS's media source sends): still allowed.
                async with http.ws_connect(f"{base}/ws/nowplaying") as ws:
                    assert (await ws.receive_json())["playing"] is False
        finally:
            await runner.cleanup()

    run(scenario())


def test_settings_page_needs_no_login_and_is_not_cacheable_or_frameable(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, port = await serve(tmp_path)
        try:
            async with aiohttp.ClientSession() as http, http.get(f"http://127.0.0.1:{port}/settings") as resp:
                body = await resp.text()
                assert resp.status == 200
                assert resp.headers["X-Frame-Options"] == "DENY"
                assert resp.headers["Cache-Control"] == "no-store"
                assert "password" not in body.lower()
                assert "sign out" not in body.lower()
            async with aiohttp.ClientSession() as http:
                for gone in ("/login", "/logout"):
                    async with http.get(f"http://127.0.0.1:{port}{gone}") as resp:
                        assert resp.status == 404, gone
        finally:
            await runner.cleanup()

    run(scenario())


def test_saving_settings_requires_a_same_origin_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, port = await serve(tmp_path)
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as http:
                data = {"queue_cap": "77"}
                for headers in (
                    {"Origin": "https://evil.example"},
                    {"Origin": base, "Sec-Fetch-Site": "cross-site"},
                    {"Referer": "https://evil.example/page"},
                ):
                    async with http.post(f"{base}/settings", data=data, headers=headers) as resp:
                        assert resp.status == 403, headers
                assert not (tmp_path / "tunables.json").exists(), "a refused save must change nothing"

                async with http.post(
                    f"{base}/settings", data=data, headers={"Origin": base, "Sec-Fetch-Site": "same-origin"}
                ) as resp:
                    assert resp.status == 200
                assert '"queue_cap": 77' in (tmp_path / "tunables.json").read_text(encoding="utf-8")
                # A scripted caller with no Origin header at all (curl, a Stream Deck button) still works.
                async with http.post(f"{base}/settings", data={"queue_cap": "60"}) as resp:
                    assert resp.status == 200
        finally:
            await runner.cleanup()

    run(scenario())


@pytest.mark.parametrize("name", ["passwords", "sessions"])
def test_login_machinery_is_gone(name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        __import__(f"twitch_radio.admin.{name}")
