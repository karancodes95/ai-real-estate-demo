"""GET /admin/metrics, /admin/sessions, /admin/sessions/{id} and /admin/leads:
shapes (the clinic demo's field names), the UUID filter, the null fields,
the windows and the params."""

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from conftest import add_lead, add_message

METRICS_KEYS = {
    "scope", "sessions_today", "messages_today", "active_sessions", "errors_24h",
    "unique_visitors_today", "engaged_visitors_today", "bounce_rate_today",
    "avg_messages_per_engaged",
}
SESSION_ROW_KEYS = {
    "id", "created_at", "last_active", "vertical", "ip", "referrer_tag", "country",
    "events", "messages",
}
SESSION_DETAIL_KEYS = {
    "id", "created_at", "last_active", "expires_at", "vertical", "ip", "referrer_tag",
    "country", "turns",
}
TURN_KEYS = {"ts", "role", "text", "stage"}
LEAD_KEYS = {
    "id", "name", "email", "phone", "budget", "location", "property_type",
    "timeline_months", "financing", "intent", "score", "grade", "rationale", "status",
    "created_at",
}


def _aware(value: str) -> datetime:
    """Parse an ISO 8601 time and insist it carries an offset."""
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None, value
    return parsed


def _sid() -> str:
    return str(uuid4())


async def _turn(sid, ip="10.0.0.1", minutes_ago=0.0, error=False):
    """One chat turn as app.chatlog writes it."""
    await add_message(sid, "user", "question", ip, minutes_ago)
    if error:
        await add_message(sid, "error", "RuntimeError", ip, minutes_ago)
    await add_message(sid, "assistant", "answer", ip, minutes_ago)


# ---- /admin/metrics ----


async def test_metrics_empty(client, auth):
    r = await client.get("/admin/metrics", headers=auth)
    assert r.status_code == 200
    assert r.json() == {
        "scope": "today", "sessions_today": 0, "messages_today": 0,
        "active_sessions": 0, "errors_24h": 0, "unique_visitors_today": 0,
        "engaged_visitors_today": 0, "bounce_rate_today": None,
        "avg_messages_per_engaged": 0.0,
    }


async def test_metrics_today_and_all(client, auth):
    a, b, old = _sid(), _sid(), _sid()
    await _turn(a, ip="10.0.0.1", minutes_ago=1)          # active now
    await _turn(a, ip="10.0.0.1", minutes_ago=1)
    await _turn(b, ip="10.0.0.2", minutes_ago=30, error=True)
    await _turn(old, ip="10.0.0.3", minutes_ago=3 * 24 * 60, error=True)  # 3 days ago
    # Not a session (NULL / client-sent id) but still a visitor's message.
    await _turn(None, ip="10.0.0.4", minutes_ago=2)
    await _turn("not-a-uuid", ip="10.0.0.4", minutes_ago=2)

    today = (await client.get("/admin/metrics", headers=auth)).json()
    assert set(today) == METRICS_KEYS
    # Midnight may fall between rows written minutes apart; skip if so.
    first_today = datetime.now() - timedelta(minutes=30)
    if first_today.date() != datetime.now().date():
        pytest.skip("too close to midnight for a 'today' window")
    assert today["scope"] == "today"
    assert today["sessions_today"] == 2          # a, b (UUID ids only)
    assert today["messages_today"] == 5          # user rows today, any id
    assert today["unique_visitors_today"] == 3   # .1 .2 .4
    assert today["engaged_visitors_today"] == 3
    assert today["avg_messages_per_engaged"] == round(5 / 3, 2)
    assert today["active_sessions"] == 1         # only a, in the last 15 min
    assert today["errors_24h"] == 1              # b's; old's is 3 days ago
    assert today["bounce_rate_today"] is None

    every = (await client.get("/admin/metrics", params={"scope": "all"}, headers=auth)).json()
    assert every["scope"] == "all"
    assert every["sessions_today"] == 3
    assert every["messages_today"] == 6
    assert every["unique_visitors_today"] == 4
    assert every["engaged_visitors_today"] == 4
    assert every["avg_messages_per_engaged"] == 1.5
    # Live-ops windows do not change with scope.
    assert every["active_sessions"] == 1
    assert every["errors_24h"] == 1
    assert every["bounce_rate_today"] is None


async def test_metrics_scope_is_validated(client, auth):
    assert (await client.get("/admin/metrics", params={"scope": "week"}, headers=auth)).status_code == 422


# ---- /admin/sessions ----


async def test_sessions_rows_are_uuid_only_newest_first(client, auth):
    a, b = _sid(), _sid()
    await _turn(a, ip="10.0.0.1", minutes_ago=60)
    await _turn(a, ip="10.0.0.9", minutes_ago=5)        # latest IP wins
    await _turn(b, ip="10.0.0.2", minutes_ago=20, error=True)
    await _turn(None, minutes_ago=1)
    await _turn("not-a-uuid", minutes_ago=1)
    await _turn(a.upper(), minutes_ago=1)                # not canonical lowercase
    await _turn("x" * 36, minutes_ago=1)

    r = await client.get("/admin/sessions", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [a, b]
    first, second = rows
    assert set(first) == SESSION_ROW_KEYS
    assert first["vertical"] == "real_estate"
    assert first["ip"] == "10.0.0.9"
    assert first["events"] == 4 and first["messages"] == 2
    assert second["events"] == 3 and second["messages"] == 1   # user, error, assistant
    for row in rows:
        assert row["referrer_tag"] is None and row["country"] is None
        assert _aware(row["created_at"]) <= _aware(row["last_active"])
    now = datetime.now(timezone.utc)
    assert abs((now - _aware(first["last_active"])) - timedelta(minutes=5)) < timedelta(minutes=1)
    assert abs((now - _aware(first["created_at"])) - timedelta(minutes=60)) < timedelta(minutes=1)


async def test_sessions_limit_and_since(client, auth):
    recent, week_old, ancient = _sid(), _sid(), _sid()
    await _turn(recent, minutes_ago=10)
    await _turn(week_old, minutes_ago=3 * 24 * 60)
    await _turn(ancient, minutes_ago=30 * 24 * 60)

    async def ids(**params):
        r = await client.get("/admin/sessions", params=params, headers=auth)
        assert r.status_code == 200
        return [row["id"] for row in r.json()]

    assert await ids() == [recent, week_old, ancient]
    assert await ids(since="all") == [recent, week_old, ancient]
    assert await ids(since="24h") == [recent]
    assert await ids(since="7d") == [recent, week_old]
    assert await ids(limit=2) == [recent, week_old]
    assert await ids(limit=1, since="7d") == [recent]
    if (datetime.now() - timedelta(minutes=10)).date() == datetime.now().date():
        assert await ids(since="today") == [recent]


async def test_sessions_since_counts_activity_not_creation(client, auth):
    sid = _sid()
    await _turn(sid, minutes_ago=10 * 24 * 60)   # started 10 days ago
    await _turn(sid, minutes_ago=5)              # still chatting
    r = await client.get("/admin/sessions", params={"since": "24h"}, headers=auth)
    assert [row["id"] for row in r.json()] == [sid]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 501}, {"limit": "x"}, {"since": "1y"}]
)
async def test_sessions_params_are_validated(client, auth, params):
    assert (await client.get("/admin/sessions", params=params, headers=auth)).status_code == 422


# ---- /admin/sessions/{id} ----


async def test_session_detail_replays_the_chat(client, auth):
    sid = _sid()
    await add_message(sid, "user", "hi", "10.0.0.1", 10)
    await add_message(sid, "assistant", "hello", "10.0.0.1", 10)
    await add_message(sid, "user", "budget 80L", "10.0.0.7", 2)
    await add_message(sid, "error", "TimeoutError", "10.0.0.7", 2)
    await add_message(sid, "assistant", "Sorry, try again.", "10.0.0.7", 2)
    await _turn(_sid())   # another session's rows stay out

    r = await client.get(f"/admin/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == SESSION_DETAIL_KEYS
    assert body["id"] == sid
    assert body["vertical"] == "real_estate"
    assert body["ip"] == "10.0.0.7"
    assert body["expires_at"] is None
    assert body["referrer_tag"] is None and body["country"] is None
    turns = body["turns"]
    assert all(set(t) == TURN_KEYS for t in turns)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "hi"), ("assistant", "hello"), ("user", "budget 80L"),
        ("error", "TimeoutError"), ("assistant", "Sorry, try again."),
    ]
    assert [t["stage"] for t in turns] == [None, None, None, "generate_reply", None]
    assert _aware(body["created_at"]) == _aware(turns[0]["ts"])
    assert _aware(body["last_active"]) == _aware(turns[-1]["ts"])
    assert _aware(turns[0]["ts"]) < _aware(turns[2]["ts"])


async def test_session_detail_accepts_an_uppercase_uuid_path(client, auth):
    sid = _sid()
    await _turn(sid)
    r = await client.get(f"/admin/sessions/{sid.upper()}", headers=auth)
    assert r.status_code == 200 and r.json()["id"] == sid


async def test_unknown_session_is_404(client, auth):
    r = await client.get(f"/admin/sessions/{_sid()}", headers=auth)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "session_not_found"


async def test_non_uuid_session_ids_are_unreachable(client, auth):
    await _turn("not-a-uuid")
    r = await client.get("/admin/sessions/not-a-uuid", headers=auth)
    assert r.status_code == 422


# ---- /admin/leads ----


async def test_leads_shape_and_order(client, auth):
    await add_lead("Cold", 20, minutes_ago=5)
    await add_lead("Hot", 90, minutes_ago=50, phone="+91 90000 00000", budget="80L",
                   location="Pune", property_type="flat", timeline_months=3,
                   financing="pre_approved", intent="buy", grade="hot",
                   rationale="ready buyer", status="visit")
    await add_lead("Warm", 55, minutes_ago=1)
    await add_lead("Warm2", 55, minutes_ago=0)

    r = await client.get("/admin/leads", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    # Best score first, then newest (higher id) - the CRM's own order.
    assert [row["name"] for row in rows] == ["Hot", "Warm2", "Warm", "Cold"]
    assert all(set(row) == LEAD_KEYS for row in rows)
    hot = rows[0]
    assert {k: hot[k] for k in LEAD_KEYS - {"id", "created_at"}} == {
        "name": "Hot", "email": "hot@example.test", "phone": "+91 90000 00000",
        "budget": "80L", "location": "Pune", "property_type": "flat",
        "timeline_months": 3, "financing": "pre_approved", "intent": "buy",
        "score": 90, "grade": "hot", "rationale": "ready buyer", "status": "visit",
    }
    now = datetime.now(timezone.utc)
    assert abs((now - _aware(hot["created_at"])) - timedelta(minutes=50)) < timedelta(minutes=1)


async def test_leads_limit_and_since(client, auth):
    await add_lead("Now", 10, minutes_ago=1)
    await add_lead("ThreeDays", 50, minutes_ago=3 * 24 * 60)
    await add_lead("Month", 90, minutes_ago=30 * 24 * 60)

    async def names(**params):
        r = await client.get("/admin/leads", params=params, headers=auth)
        assert r.status_code == 200
        return [row["name"] for row in r.json()]

    assert await names() == ["Month", "ThreeDays", "Now"]
    assert await names(since="all") == ["Month", "ThreeDays", "Now"]
    assert await names(since="7d") == ["ThreeDays", "Now"]
    assert await names(since="24h") == ["Now"]
    assert await names(limit=1) == ["Month"]
    if (datetime.now() - timedelta(minutes=1)).date() == datetime.now().date():
        assert await names(since="today") == ["Now"]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 501}, {"since": "forever"}]
)
async def test_leads_params_are_validated(client, auth, params):
    assert (await client.get("/admin/leads", params=params, headers=auth)).status_code == 422


async def test_crm_leads_keeps_its_shape_and_format(client, api_key):
    # Who sees which lead is tests/test_crm_scope.py; this is the row shape.
    sid = "6f1c2a4e-1b2c-4d3e-8f90-123456789abc"
    await add_lead("Hot", 90, session_id=sid)
    assert (await client.get("/api/crm/leads")).status_code == 401
    r = await client.get("/api/crm/leads", headers={"X-API-Key": api_key, "X-Session-Id": sid})
    assert r.status_code == 200
    (row,) = r.json()
    assert set(row) == LEAD_KEYS and row["name"] == "Hot"
    # Still the CRM page's own display format ("29 Oct, 09:05 AM"), not ISO.
    assert re.fullmatch(r"\d{2} [A-Z][a-z]{2}, \d{2}:\d{2} [AP]M", row["created_at"])
