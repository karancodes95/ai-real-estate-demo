"""Bearer-token auth for the admin API (copied from the clinic demo's
app/api/admin_auth.py, without its legacy X-Admin-Key path).

    POST /admin/login {password}          → {token, expires_at}
    All other /admin/* need:
      Authorization: Bearer <jwt>

The token is a stateless HMAC-signed JWT (HS256). Stateless because the only
"user" is the operator; per-token revocation isn't needed (rotate
ADMIN_JWT_SECRET to invalidate every live session at once). The static
API_KEY (chat + CRM) never unlocks /admin: it ships in the public client.

An empty ADMIN_PASSWORD_HASH means login is off: every login answers 401, so
no token can be issued and every /admin call is refused.

Login is rate-limited in-process - 5 attempts / 15 min / IP - to bound
brute-force without adding a DB table or Redis dep.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import settings
from .deps import client_ip

router = APIRouter(prefix="/admin", tags=["admin"])

_JWT_ALG = "HS256"

# In-process login rate limit. Keyed by IP, value is a deque of recent attempt
# timestamps (Unix seconds). Resets on process restart - acceptable for a
# brute-force throttle since argon2 verification is already ~100ms each.
_LOGIN_WINDOW_SECONDS = 15 * 60  # 15 minutes
_LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict[str, deque[float]] = defaultdict(deque)

# Single shared hasher with library defaults (argon2id, ~50ms on modern HW).
_ph = PasswordHasher()


def _prune_attempts(ip: str, now: float) -> None:
    """Drop attempt timestamps older than the rolling window."""
    bucket = _login_attempts[ip]
    cutoff = now - _LOGIN_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def _check_login_rate_limit(ip: str) -> None:
    """Reject the login attempt if this IP has tried too many times recently."""
    now = time.time()
    _prune_attempts(ip, now)
    if len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_login_attempts",
                "message": (
                    f"Too many failed logins. Try again in "
                    f"{_LOGIN_WINDOW_SECONDS // 60} min."
                ),
            },
        )


def _record_login_attempt(ip: str) -> None:
    """Append a timestamp. Caller decides whether to clear on success."""
    _login_attempts[ip].append(time.time())


def _clear_login_attempts(ip: str) -> None:
    """Successful login clears the IP's failure bucket."""
    _login_attempts.pop(ip, None)


def _issue_token() -> tuple[str, int]:
    """Mint a new admin JWT. Returns (token, expires_at_unix)."""
    if not settings.admin_jwt_secret:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "auth_misconfigured",
                "message": "ADMIN_JWT_SECRET not set in backend .env",
            },
        )
    iat = int(time.time())
    exp = iat + settings.admin_session_ttl_hours * 3600
    token = jwt.encode(
        {"iat": iat, "exp": exp, "scope": "admin"},
        settings.admin_jwt_secret,
        algorithm=_JWT_ALG,
    )
    return token, exp


def _decode_token(token: str) -> dict:
    """Decode + verify a bearer token. Raises HTTPException(401) on any failure."""
    if not settings.admin_jwt_secret:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})
    try:
        return jwt.decode(
            token, settings.admin_jwt_secret, algorithms=[_JWT_ALG],
            options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired", "message": "session expired"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid token"},
        )


def verify_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    """Dependency that gates every /admin/* endpoint but /admin/login:
    ``Authorization: Bearer <jwt>`` issued by /admin/login, nothing else."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            payload = _decode_token(value)
            if payload.get("scope") != "admin":
                raise HTTPException(
                    status_code=401,
                    detail={"error": "unauthorized", "message": "invalid token scope"},
                )
            return
    raise HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "message": "missing or invalid credentials"},
    )


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_at: int  # Unix seconds


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request) -> LoginResponse:
    ip = client_ip(request)
    _check_login_rate_limit(ip)

    if not settings.admin_password_hash:
        # No hash set → login is disabled (operator hasn't bootstrapped).
        # Don't reveal that vs. wrong-password to clients; just 401.
        _record_login_attempt(ip)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid password"},
        )

    try:
        _ph.verify(settings.admin_password_hash, req.password)
    except (VerifyMismatchError, InvalidHashError):
        _record_login_attempt(ip)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid password"},
        )

    # Successful login: clear the failure bucket so the operator isn't
    # rate-limited by their own earlier typos.
    _clear_login_attempts(ip)
    token, exp = _issue_token()
    return LoginResponse(token=token, expires_at=exp)


@router.post("/logout")
async def logout() -> Response:
    """No-op on a stateless JWT design. The client discards the token; the
    server has nothing to delete. Exposed so an admin app's logout button has
    a well-named endpoint to call."""
    return Response(status_code=204)
