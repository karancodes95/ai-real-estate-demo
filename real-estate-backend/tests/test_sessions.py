"""POST /api/session: the clinic demo's session route (same query params and
bounds, the same payload shape, one live session per IP), behind the API key."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import sessions
from app.config import settings
from app.db import SessionLocal
from app.models import DemoSession, Event
from conftest import CLIENT_IP, add_session

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def _open(client, api_key, **params):
    return await client.post("/api/session", params=params, headers={"X-API-Key": api_key})


async def _rows() -> list[DemoSession]:
    async with SessionLocal() as s:
        return list(
            (await s.execute(select(DemoSession).order_by(DemoSession.created_at))).scalars()
        )


async def _events() -> list[tuple[str, str, dict]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
    return [(str(e.session_id), e.type, e.payload) for e in rows]


async def _last_active(sid: str) -> datetime:
    async with SessionLocal() as s:
        return (
            await s.execute(text("SELECT last_active FROM sessions WHERE id = CAST(:id AS uuid)"),
                            {"id": sid})
        ).scalar_one()


async def test_opens_a_session_with_the_clinic_payload(client, api_key):
    r = await _open(client, api_key, referrer_tag="linkedin", country="IN")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "session_id": body["session_id"], "vertical": "real_estate",
        "max_input_chars": 2000, "max_output_tokens": 0,
    }
    assert UUID.fullmatch(body["session_id"])

    (row,) = await _rows()
    assert str(row.id) == body["session_id"]
    assert row.vertical == "real_estate"
    assert str(row.ip) == CLIENT_IP
    assert row.referrer_tag == "linkedin" and row.country == "IN"
    assert row.agent_session_id is None
    assert row.created_at.tzinfo is not None
    assert abs(row.expires_at - row.created_at - timedelta(hours=24)) < timedelta(seconds=5)
    assert await _events() == [(body["session_id"], "session_created", {
        "vertical": "real_estate", "referrer_tag": "linkedin", "country": "IN",
    })]


async def test_vertical_may_be_named_but_must_be_this_one(client, api_key):
    assert (await _open(client, api_key, vertical="real_estate")).status_code == 200
    r = await _open(client, api_key, vertical="healthcare")
    assert r.status_code == 400
    assert r.json()["detail"] == "unknown vertical: healthcare"


@pytest.mark.parametrize(
    "params",
    [{"vertical": "x" * 33}, {"referrer_tag": "x" * 101}, {"country": "x" * 9}],
    ids=["vertical", "referrer_tag", "country"],
)
async def test_free_form_params_are_bounded(client, api_key, params):
    assert (await _open(client, api_key, **params)).status_code == 422
    assert await _rows() == []


async def test_params_at_their_bounds_are_stored(client, api_key):
    assert (await _open(client, api_key, referrer_tag="r" * 100, country="c" * 8)).status_code == 200
    (row,) = await _rows()
    assert row.referrer_tag == "r" * 100 and row.country == "c" * 8


async def test_the_api_key_is_required(client):
    assert (await client.post("/api/session")).status_code == 401
    assert (await client.post("/api/session", headers={"X-API-Key": "nope"})).status_code == 401
    assert await _rows() == []


async def test_the_same_ip_gets_its_live_session_back(client, api_key):
    first = (await _open(client, api_key, referrer_tag="linkedin")).json()["session_id"]
    async with SessionLocal() as s:
        await s.execute(text("UPDATE sessions SET last_active = NOW() - INTERVAL '1 hour'"))
        await s.commit()
    before = await _last_active(first)
    again = (await _open(client, api_key, referrer_tag="other")).json()["session_id"]
    assert again == first
    # Reuse touches the session but keeps its first metadata.
    assert await _last_active(first) > before
    (row,) = await _rows()
    assert row.referrer_tag == "linkedin"
    assert [t for _, t, _ in await _events()] == ["session_created"]


async def test_another_ip_gets_its_own_session(client, client_from, api_key):
    mine = (await _open(client, api_key)).json()["session_id"]
    theirs = (await _open(client_from("10.1.2.3"), api_key)).json()["session_id"]
    assert mine != theirs
    assert sorted(str(r.ip) for r in await _rows()) == sorted([CLIENT_IP, "10.1.2.3"])


async def test_an_expired_session_is_not_reused(client, api_key):
    expired = await add_session(CLIENT_IP, minutes_ago=25 * 60, expires_in_minutes=-60)
    sid = (await _open(client, api_key)).json()["session_id"]
    assert sid != expired
    assert len(await _rows()) == 2


async def test_the_most_recently_active_live_session_is_reused(client, api_key):
    await add_session(CLIENT_IP, minutes_ago=60, active_minutes_ago=50)
    recent = await add_session(CLIENT_IP, minutes_ago=60, active_minutes_ago=5)
    assert (await _open(client, api_key)).json()["session_id"] == recent


async def test_a_session_of_another_vertical_is_not_reused(client, api_key):
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO sessions (id, vertical, expires_at, ip) VALUES "
            "(gen_random_uuid(), 'healthcare', NOW() + INTERVAL '1 hour', CAST(:ip AS inet))"
        ), {"ip": CLIENT_IP})
        await s.commit()
    await _open(client, api_key)
    assert sorted(r.vertical for r in await _rows()) == ["healthcare", "real_estate"]


@pytest.mark.parametrize(
    "raw, stored",
    [("10.0.0.1", "10.0.0.1"), ("2001:DB8::1", "2001:db8::1"), ("unknown", None),
     ("", None), (None, None), ("10.0.0.1, 10.0.0.2", None), ("999.1.1.1", None),
     ("0.0.0.0", None), ("::", None)],
)
def test_only_an_address_reaches_the_inet_column(raw, stored):
    assert sessions.inet_or_none(raw) == stored


async def test_no_peer_address_stores_no_ip_and_reuses_nothing(client_from, api_key):
    no_ip = client_from("unknown")
    one = (await _open(no_ip, api_key)).json()["session_id"]
    two = (await _open(no_ip, api_key)).json()["session_id"]
    assert one != two
    assert [r.ip for r in await _rows()] == [None, None]


# ---- country from the edge proxy (Cloudflare's CF-IPCountry) ----


@pytest.fixture
def behind_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)


async def _open_with_country_header(client, api_key, value, **params):
    return await client.post(
        "/api/session", params=params,
        headers={"X-API-Key": api_key, "CF-IPCountry": value},
    )


async def test_country_falls_back_to_the_proxy_header(client, api_key, behind_proxy):
    assert (await _open_with_country_header(client, api_key, "in")).status_code == 200
    (row,) = await _rows()
    assert row.country == "IN"


async def test_the_client_country_wins_over_the_header(client, api_key, behind_proxy):
    await _open_with_country_header(client, api_key, "IN", country="DE")
    (row,) = await _rows()
    assert row.country == "DE"


@pytest.mark.parametrize("value", ["XX", "T1", "IND", "", "1N"])
async def test_unknown_or_odd_header_values_store_no_country(client, api_key, behind_proxy,
                                                              value):
    await _open_with_country_header(client, api_key, value)
    (row,) = await _rows()
    assert row.country is None


async def test_the_header_is_ignored_without_a_trusted_proxy(client, api_key):
    assert settings.trust_proxy is False
    await _open_with_country_header(client, api_key, "IN")
    (row,) = await _rows()
    assert row.country is None


async def test_expiry_is_24h_from_creation(client, api_key):
    await _open(client, api_key)
    (row,) = await _rows()
    now = datetime.now(timezone.utc)
    assert abs(row.expires_at - (now + timedelta(hours=24))) < timedelta(minutes=1)
