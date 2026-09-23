"""Address helpers shared by config.py and the admin server. Stdlib only."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"

_PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")


def is_loopback_host(host: str) -> bool:
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def parse_networks(raw: str) -> tuple[tuple[IPNetwork, ...], list[str]]:
    """Comma/space separated IPs and CIDRs -> (networks, rejected tokens)."""
    networks: list[IPNetwork] = []
    rejected: list[str] = []
    for token in raw.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            rejected.append(token)
    return tuple(networks), rejected


def _parse_ip(raw: str) -> IPAddress | None:
    text = raw.strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: IPAddress, trusted: Sequence[IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in trusted)


def is_trusted_peer(peer: str | None, trusted: Sequence[IPNetwork]) -> bool:
    ip = _parse_ip(peer) if peer else None
    return ip is not None and _is_trusted(ip, trusted)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted: Sequence[IPNetwork]) -> str:
    """The peer's address, or behind a trusted proxy the first X-Forwarded-For
    entry (read right to left) that isn't one of our own proxies."""
    peer_ip = _parse_ip(peer) if peer else None
    if peer_ip is None:
        return peer or "unknown"
    if not forwarded_for or not _is_trusted(peer_ip, trusted):
        return str(peer_ip)
    verified = peer_ip
    for entry in reversed(forwarded_for.split(",")):
        hop = _parse_ip(entry)
        if hop is None:
            return str(verified)
        if not _is_trusted(hop, trusted):
            return str(hop)
        verified = hop
    return str(verified)


def looks_proxied(headers: Mapping[str, str]) -> bool:
    return any(name in headers for name in _PROXY_HEADERS)
