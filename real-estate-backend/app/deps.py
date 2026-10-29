"""Shared FastAPI dependencies for the JSON API routes."""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Header, HTTPException, Request

from .config import settings


def client_ip(request: Request) -> str:
    """Best-effort client IP. Trust X-Forwarded-For only behind a real proxy
    (config.trust_proxy); otherwise a client could spoof it to dodge the cap."""
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Rightmost entry = the IP our own proxy observed and appended;
            # leftmost entries are client-supplied and spoofable.
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


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
