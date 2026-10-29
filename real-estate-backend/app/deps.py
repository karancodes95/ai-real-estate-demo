"""Shared FastAPI dependencies for the JSON API routes."""

from __future__ import annotations

import ipaddress
from secrets import compare_digest

from fastapi import Header, HTTPException, Request

from .config import settings


def _peer_is_trusted(request: Request) -> bool:
    """True when the immediate socket peer is our own reverse proxy, so the
    headers it sets (X-Real-IP, CF-IPCountry) can be believed: TRUST_PROXY is
    on and the peer is loopback (nginx on the same host, the clinic demo's
    rule). A directly connected client is never trusted, so it cannot forge
    those headers to dodge the per-IP cap or claim another IP's session."""
    if not settings.trust_proxy:
        return False
    peer = request.client.host if request.client is not None else None
    if peer is None:
        return False
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Resolve the real visitor IP (the clinic demo's client_ip). Production
    topology:

        client -> Cloudflare -> nginx -> uvicorn (127.0.0.1, --proxy-headers)

    X-Real-IP is trusted ONLY when the socket peer is a trusted proxy (see
    _peer_is_trusted); a direct client cannot forge it. X-Forwarded-For is
    never read (its leftmost entry is attacker-controlled). Non-parseable
    hosts normalize to 0.0.0.0 so Postgres `inet` accepts the value."""
    if _peer_is_trusted(request):
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            try:
                ipaddress.ip_address(real_ip)
                return real_ip
            except ValueError:
                # Header present but garbage: fall through to the socket peer
                # rather than "0.0.0.0" so malformed clients don't share a bucket.
                pass

    socket_host = request.client.host if request.client is not None else "0.0.0.0"
    try:
        ipaddress.ip_address(socket_host)
        return socket_host
    except ValueError:
        return "0.0.0.0"


def proxy_country(request: Request) -> str | None:
    """The visitor's country as the edge proxy geolocated it (Cloudflare's
    CF-IPCountry, ISO 3166-1 alpha-2), only from a trusted proxy peer, like
    X-Real-IP. "XX" (unknown) and anything not two letters is None."""
    if not _peer_is_trusted(request):
        return None
    value = (request.headers.get("cf-ipcountry") or "").strip().upper()
    if len(value) == 2 and value.isascii() and value.isalpha() and value != "XX":
        return value
    return None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Gate the JSON API (chat + CRM data) behind the static demo API key. The
    static Pages frontends send it in X-API-Key; local pages served by this app
    can still reach the HTML routes, which are deliberately left ungated.
    The key ships in the public chat page, so it is a bot gate, not access
    control: the CRM routes also scope every read and change to the caller's
    own chat session (main.visitor_session). Constant-time compare avoids
    leaking the key via timing."""
    if not compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
