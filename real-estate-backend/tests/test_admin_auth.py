"""POST /admin/login, the login rate limit and the bearer-JWT gate."""

import time

import jwt
import pytest

from app import admin_auth
from app.config import settings

ADMIN_GETS = [
    "/admin/metrics",
    "/admin/sessions",
    "/admin/sessions/6f1c2a4e-1b2c-4d3e-8f90-123456789abc",
    "/admin/leads",
]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(secret: str, **claims) -> str:
    now = int(time.time())
    payload = {"iat": now, "exp": now + 3600, "scope": "admin", **claims}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, secret, algorithm="HS256")


# ---- login on ----


async def test_login_issues_a_7_day_admin_token(client, admin_password, jwt_secret):
    before = int(time.time())
    r = await client.post("/admin/login", json={"password": admin_password})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token", "expires_at"}
    assert before + 168 * 3600 <= body["expires_at"] <= int(time.time()) + 168 * 3600
    claims = jwt.decode(body["token"], jwt_secret, algorithms=["HS256"])
    assert claims["scope"] == "admin"
    assert claims["exp"] == body["expires_at"]
    # The token opens every admin route.
    for path in ADMIN_GETS[:2] + ADMIN_GETS[3:]:
        assert (await client.get(path, headers=_bearer(body["token"]))).status_code == 200


async def test_wrong_password_is_401(client):
    r = await client.post("/admin/login", json={"password": "not-the-password"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthorized"


@pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": "x" * 257}])
async def test_login_body_is_validated(client, body):
    assert (await client.post("/admin/login", json=body)).status_code == 422


async def test_logout_is_a_no_op_204(client):
    assert (await client.post("/admin/logout")).status_code == 204


# ---- login off ----


async def test_empty_hash_turns_login_off(client, admin_password, monkeypatch):
    monkeypatch.setattr(settings, "admin_password_hash", "")
    r = await client.post("/admin/login", json={"password": admin_password})
    assert r.status_code == 401
    # Same answer as a wrong password: nothing tells a caller login is off.
    assert r.json()["detail"] == {"error": "unauthorized", "message": "invalid password"}
    # And no route opens without a token.
    for path in ADMIN_GETS:
        assert (await client.get(path)).status_code == 401


async def test_missing_jwt_secret_is_a_500_on_login_and_401_on_routes(
    client, admin_password, token, monkeypatch
):
    monkeypatch.setattr(settings, "admin_jwt_secret", "")
    r = await client.post("/admin/login", json={"password": admin_password})
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "auth_misconfigured"
    assert (await client.get("/admin/metrics", headers=_bearer(token))).status_code == 401


# ---- rate limit ----


async def test_five_failures_lock_the_ip_out_even_for_the_right_password(
    client, admin_password
):
    for _ in range(5):
        r = await client.post("/admin/login", json={"password": "wrong"})
        assert r.status_code == 401
    r = await client.post("/admin/login", json={"password": admin_password})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "too_many_login_attempts"


async def test_success_clears_earlier_failures(client, admin_password):
    for _ in range(4):
        await client.post("/admin/login", json={"password": "wrong"})
    assert (await client.post("/admin/login", json={"password": admin_password})).status_code == 200
    for _ in range(4):
        assert (await client.post("/admin/login", json={"password": "wrong"})).status_code == 401
    assert (await client.post("/admin/login", json={"password": admin_password})).status_code == 200


async def test_old_failures_leave_the_window(client, admin_password, monkeypatch):
    for _ in range(5):
        await client.post("/admin/login", json={"password": "wrong"})
    real_time = time.time
    monkeypatch.setattr(admin_auth.time, "time", lambda: real_time() + 15 * 60 + 1)
    assert (await client.post("/admin/login", json={"password": admin_password})).status_code == 200


async def test_login_off_attempts_count_toward_the_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password_hash", "")
    for _ in range(5):
        assert (await client.post("/admin/login", json={"password": "x"})).status_code == 401
    assert (await client.post("/admin/login", json={"password": "x"})).status_code == 429


# ---- JWT checks ----


@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_routes_need_a_token(client, path):
    r = await client.get(path)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthorized"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic YWRtaW46eA=="},
        {"Authorization": "Bearer not.a.jwt"},
    ],
)
async def test_malformed_authorization_is_401(client, headers):
    assert (await client.get("/admin/metrics", headers=headers)).status_code == 401


async def test_api_key_and_admin_key_headers_do_not_open_admin(client, api_key):
    for headers in ({"X-API-Key": api_key}, {"X-Admin-Key": api_key}):
        assert (await client.get("/admin/metrics", headers=headers)).status_code == 401


async def test_expired_token_says_so(client, jwt_secret):
    now = int(time.time())
    token = _mint(jwt_secret, iat=now - 7200, exp=now - 3600)
    r = await client.get("/admin/metrics", headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "token_expired"


@pytest.mark.parametrize(
    "token_of",
    [
        lambda secret: _mint("some-other-secret-of-enough-length-1234567890"),
        lambda secret: _mint(secret, scope="viewer"),
        lambda secret: _mint(secret, scope=None),
        lambda secret: _mint(secret, exp=None),
        lambda secret: _mint(secret, iat=None),
        lambda secret: jwt.encode(
            {"iat": int(time.time()), "exp": int(time.time()) + 60, "scope": "admin"},
            secret,
            algorithm="HS512",
        ),
        # alg "none": an unsigned token.
        lambda secret: jwt.encode(
            {"iat": int(time.time()), "exp": int(time.time()) + 60, "scope": "admin"},
            None,
            algorithm="none",
        ),
    ],
    ids=["wrong-secret", "wrong-scope", "no-scope", "no-exp", "no-iat", "hs512", "alg-none"],
)
async def test_bad_tokens_are_401(client, jwt_secret, token_of):
    r = await client.get("/admin/metrics", headers=_bearer(token_of(jwt_secret)))
    assert r.status_code == 401


async def test_a_well_formed_token_opens_the_route(client, jwt_secret):
    r = await client.get("/admin/metrics", headers=_bearer(_mint(jwt_secret)))
    assert r.status_code == 200
