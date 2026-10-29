"""Pytest fixtures: the app in-process over httpx.ASGITransport (like the
clinic demo's tests) on a throwaway ``lead_to_lease_test`` database.

Env vars are set BEFORE importing the app so its settings and engine bind to
the test database and a test-only API key, admin password and JWT secret (env
vars win over .env). The schema comes from the app's own create_all; every
test starts from empty tables. The live ``lead_to_lease`` DB is never touched,
and no test calls the Claude Agent SDK (tests that chat mock generate_reply).
"""

import getpass
import json
import os
import secrets
from uuid import uuid4

import asyncpg
import httpx
import pytest
from argon2 import PasswordHasher

TEST_DB = "lead_to_lease_test"
PG_USER = os.environ.get("PGUSER") or getpass.getuser()
PG_HOST = "localhost"
PG_PORT = 5432

# Fresh per run, never the real ones.
TEST_ADMIN_PASSWORD = secrets.token_urlsafe(16)
TEST_API_KEY = secrets.token_hex(16)
TEST_JWT_SECRET = secrets.token_hex(32)

# Must be set before `app` (and its engine/settings) import below.
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://{PG_USER}@{PG_HOST}:{PG_PORT}/{TEST_DB}"
os.environ["API_KEY"] = TEST_API_KEY
os.environ["ADMIN_PASSWORD_HASH"] = PasswordHasher().hash(TEST_ADMIN_PASSWORD)
os.environ["ADMIN_JWT_SECRET"] = TEST_JWT_SECRET
os.environ["ADMIN_SESSION_TTL_HOURS"] = "168"
os.environ["TRUST_PROXY"] = "false"
os.environ["CHAT_DAILY_IP_CAP"] = "0"
os.environ["ALLOWED_ORIGINS"] = ""
# No test may send mail.
os.environ["ZOHO_EMAIL"] = ""
os.environ["ZOHO_APP_PASSWORD"] = ""

from sqlalchemy import text  # noqa: E402

from app import admin_auth  # noqa: E402
from app.bootstrap import init_models  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

TABLES = "events, sessions, chat_messages, leads, email_log, ip_usage"


@pytest.fixture(scope="session")
def admin_password() -> str:
    return TEST_ADMIN_PASSWORD


@pytest.fixture(scope="session")
def api_key() -> str:
    return TEST_API_KEY


@pytest.fixture(scope="session")
def jwt_secret() -> str:
    return TEST_JWT_SECRET


@pytest.fixture(scope="session", autouse=True)
async def _test_db():
    # Guard: tests write and delete rows, so never run them on a live DB.
    assert settings.database_url.endswith(f"/{TEST_DB}"), "not the test database"

    conn = await asyncpg.connect(user=PG_USER, host=PG_HOST, port=PG_PORT, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()
    await init_models()
    yield
    await engine.dispose()
    conn = await asyncpg.connect(user=PG_USER, host=PG_HOST, port=PG_PORT, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _clean():
    """Empty tables and an empty login rate-limit bucket for every test."""
    async with SessionLocal() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
        await s.commit()
    admin_auth._login_attempts.clear()
    yield
    admin_auth._login_attempts.clear()


CLIENT_IP = "127.0.0.1"


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app, client=(CLIENT_IP, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_from():
    """A factory for clients whose socket peer is another IP (TRUST_PROXY is
    off, so the peer is the visitor's IP)."""
    clients = []

    def make(ip: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip, 50000))
        ac = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(ac)
        return ac

    yield make
    for ac in clients:
        await ac.aclose()


@pytest.fixture
async def token(client, admin_password) -> str:
    r = await client.post("/admin/login", json={"password": admin_password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def auth(token) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_message(
    session_id: str | None,
    role: str,
    content: str = "x",
    ip: str = "10.0.0.1",
    minutes_ago: float = 0,
) -> None:
    """Insert one chat_messages row `minutes_ago` before the DB's now."""
    async with SessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO chat_messages (session_id, role, content, client_ip, created_at) "
                "VALUES (:sid, :role, :content, :ip, "
                "        LOCALTIMESTAMP - make_interval(secs => :secs))"
            ),
            {"sid": session_id, "role": role, "content": content, "ip": ip,
             "secs": minutes_ago * 60},
        )
        await s.commit()


async def add_lead(name: str, score: int, minutes_ago: float = 0, **fields) -> None:
    """Insert one leads row `minutes_ago` before the DB's now."""
    values = {
        "name": name, "email": f"{name.lower()}@example.test", "phone": "",
        "budget": "", "location": "", "property_type": "", "timeline_months": 0,
        "financing": "unknown", "intent": "unknown", "score": score, "grade": "warm",
        "rationale": "", "status": "new", "session_id": "",
        **fields,
    }
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    async with SessionLocal() as s:
        await s.execute(
            text(
                f"INSERT INTO leads ({cols}, created_at) "
                f"VALUES ({params}, LOCALTIMESTAMP - make_interval(secs => :secs))"
            ),
            {**values, "secs": minutes_ago * 60},
        )
        await s.commit()


async def add_session(
    ip: str | None = "10.0.0.1",
    minutes_ago: float = 0,
    active_minutes_ago: float | None = None,
    expires_in_minutes: float = 24 * 60,
    referrer_tag: str | None = None,
    country: str | None = None,
    agent_session_id: str | None = None,
) -> str:
    """Insert one sessions row created `minutes_ago` before the DB's now, last
    active `active_minutes_ago` (default: at creation), expiring
    `expires_in_minutes` after now (negative = already expired). Returns its id."""
    sid = str(uuid4())
    if active_minutes_ago is None:
        active_minutes_ago = minutes_ago
    async with SessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO sessions (id, created_at, last_active, vertical, referrer_tag, "
                "                      country, expires_at, ip, agent_session_id) "
                "VALUES (CAST(:id AS uuid), NOW() - make_interval(secs => :created), "
                "        NOW() - make_interval(secs => :active), 'real_estate', :ref, "
                "        :country, NOW() + make_interval(secs => :expires), "
                "        CAST(:ip AS inet), :agent)"
            ),
            {"id": sid, "created": minutes_ago * 60, "active": active_minutes_ago * 60,
             "ref": referrer_tag, "country": country, "expires": expires_in_minutes * 60,
             "ip": ip, "agent": agent_session_id},
        )
        await s.commit()
    return sid


async def add_event(
    session_id: str, type_: str, payload: dict | None = None, minutes_ago: float = 0
) -> None:
    """Insert one events row `minutes_ago` before the DB's now."""
    async with SessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO events (session_id, ts, type, payload) "
                "VALUES (CAST(:sid AS uuid), NOW() - make_interval(secs => :secs), :type, "
                "        CAST(:payload AS jsonb))"
            ),
            {"sid": session_id, "secs": minutes_ago * 60, "type": type_,
             "payload": json.dumps(payload or {})},
        )
        await s.commit()


async def db_day_changes(minutes_ago: float = 0, minutes_ahead: float = 1) -> bool:
    """True when the DB's local day (date_trunc('day', NOW()) in its own
    TimeZone, the boundary every 'today' window uses) differs between
    `minutes_ago` and `minutes_ahead` from the DB's now, so a test asserting
    'today' over that span would be flaky and should skip."""
    async with SessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT date_trunc('day', NOW() - make_interval(secs => :ago)) "
                    "    <> date_trunc('day', NOW() + make_interval(secs => :ahead))"
                ),
                {"ago": minutes_ago * 60, "ahead": minutes_ahead * 60},
            )
        ).scalar_one()
