"""Shared FastAPI dependencies for the JSON API routes."""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Header, HTTPException

from .config import settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Gate the JSON API (chat + CRM data) behind the static demo API key. The
    static Pages frontends send it in X-API-Key; local pages served by this app
    can still reach the HTML routes, which are deliberately left ungated.
    Constant-time compare avoids leaking the key via timing."""
    if not compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
