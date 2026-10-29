"""GET /admin/metrics, /admin/sessions, /admin/sessions/{id} and /admin/leads:
shapes (the clinic demo's field names), every metrics field computed from the
sessions and events tables the way the clinic demo does, the windows and the
params."""

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from conftest import add_event, add_lead, add_message, add_session, db_day_changes

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


def _near(value: str, minutes_ago: float) -> bool:
    now = datetime.now(timezone.utc)
    return abs((now - _aware(value)) - timedelta(minutes=minutes_ago)) < timedelta(minutes=1)


async def _turn(sid, minutes_ago=0.0, error=False, text="question"):
    """One chat turn's events as app.main.chat logs them."""
    await add_event(sid, "question_asked", {"text": text}, minutes_ago)
    if error:
        await add_event(sid, "error", {"stage": "generate_reply", "message": "RuntimeError"},
                        minutes_ago)
        return
    await add_event(sid, "answer_streamed", {"text": "answer", "chars": 6, "ms": 5}, minutes_ago)
    await add_event(sid, "turn_complete", {"total_ms": 5}, minutes_ago)


# ---- /admin/metrics ----


async def test_metrics_empty(client, auth):
    r = await client.get("/admin/metrics", headers=auth)
    assert r.status_code == 200
    assert r.json() == {
        "scope": "today", "sessions_today": 0, "messages_today": 0,
        "active_sessions": 0, "errors_24h": 0, "unique_visitors_today": 0,
        "engaged_visitors_today": 0, "bounce_rate_today": 0.0,
        "avg_messages_per_engaged": 0.0,
    }


async def test_metrics_today_and_all(client, auth):
    if await db_day_changes(minutes_ago=30):
        pytest.skip("too close to the DB's midnight for a 'today' window")
    # Two sessions on one IP, chatting now.
    a = await add_session("10.0.0.1", minutes_ago=1, active_minutes_ago=0)
    await _turn(a, 1)
    await _turn(a, 1)
    d = await add_session("10.0.0.1", minutes_ago=2)
    await _turn(d, 2)
    # Asked once, failed, 30 min ago (not active any more).
    b = await add_session("10.0.0.2", minutes_ago=30)
    await _turn(b, 30, error=True)
    # Opened the page today and never asked: a bounce.
    await add_session("10.0.0.3", minutes_ago=5)
    # No IP: a session and a message, but not a visitor.
    e = await add_session(None, minutes_ago=1)
    await _turn(e, 1)
    # Returning visitor: session from 2 days ago, asked today.
    f = await add_session("10.0.0.5", minutes_ago=2 * 24 * 60, active_minutes_ago=10)
    await _turn(f, 10)
    # 3 days ago, expired: asked and failed then.
    old = await add_session("10.0.0.4", minutes_ago=3 * 24 * 60, expires_in_minutes=-2 * 24 * 60)
    await _turn(old, 3 * 24 * 60, error=True)
    # Touched 5 min ago but expired a minute ago: not active, not a visitor today.
    await add_session("10.0.0.6", minutes_ago=2 * 24 * 60, active_minutes_ago=5,
                      expires_in_minutes=-1)

    today = (await client.get("/admin/metrics", headers=auth)).json()
    assert set(today) == METRICS_KEYS
    assert today["scope"] == "today"
    assert today["sessions_today"] == 5          # a, d, b, the bounce, e
    assert today["messages_today"] == 6          # question_asked today, any session
    assert today["unique_visitors_today"] == 4   # .1 .2 .3 .5
    assert today["engaged_visitors_today"] == 3  # .1 .2 .5
    assert today["bounce_rate_today"] == 0.25    # 1 - 3/4
    assert today["avg_messages_per_engaged"] == round(5 / 3, 2)  # .1: 3, .2: 1, .5: 1
    assert today["active_sessions"] == 5         # a, d, the bounce, e, f
    assert today["errors_24h"] == 1              # b's; old's is 3 days ago

    every = (await client.get("/admin/metrics", params={"scope": "all"}, headers=auth)).json()
    assert every["scope"] == "all"
    assert every["sessions_today"] == 8
    assert every["messages_today"] == 7
    assert every["unique_visitors_today"] == 6   # .1 to .6
    assert every["engaged_visitors_today"] == 4  # .1 .2 .4 .5
    assert every["bounce_rate_today"] == round(1 - 4 / 6, 4)
    assert every["avg_messages_per_engaged"] == 1.5
    # Live-ops windows do not change with scope.
    assert every["active_sessions"] == 5
    assert every["errors_24h"] == 1


async def test_metrics_bounce_rate_is_zero_when_every_visitor_asks(client, auth):
    sid = await add_session("10.0.0.1")
    await _turn(sid)
    body = (await client.get("/admin/metrics", headers=auth)).json()
    assert body["unique_visitors_today"] == body["engaged_visitors_today"] == 1
    assert body["bounce_rate_today"] == 0.0


async def test_metrics_only_question_events_are_messages(client, auth):
    sid = await add_session("10.0.0.1")
    for type_ in ("session_created", "answer_streamed", "turn_complete", "error"):
        await add_event(sid, type_)
    body = (await client.get("/admin/metrics", headers=auth)).json()
    assert body["messages_today"] == 0
    assert body["engaged_visitors_today"] == 0
    assert body["bounce_rate_today"] == 1.0
    assert body["errors_24h"] == 1


async def test_metrics_scope_is_validated(client, auth):
    assert (await client.get("/admin/metrics", params={"scope": "week"}, headers=auth)).status_code == 422


# ---- /admin/sessions ----


async def test_sessions_rows_newest_activity_first(client, auth):
    a = await add_session("10.0.0.9", minutes_ago=60, active_minutes_ago=5,
                          referrer_tag="linkedin", country="IN")
    await _turn(a, 60)
    await _turn(a, 5)
    b = await add_session("2001:db8::7", minutes_ago=20)
    await _turn(b, 20, error=True)
    c = await add_session(None, minutes_ago=40)   # no events: a bounce

    r = await client.get("/admin/sessions", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [a, b, c]
    first, second, third = rows
    assert all(set(row) == SESSION_ROW_KEYS for row in rows)
    assert all(row["vertical"] == "real_estate" for row in rows)
    assert first["ip"] == "10.0.0.9"
    assert first["referrer_tag"] == "linkedin" and first["country"] == "IN"
    assert first["events"] == 6 and first["messages"] == 2
    assert second["ip"] == "2001:db8::7"
    assert second["referrer_tag"] is None and second["country"] is None
    assert second["events"] == 2 and second["messages"] == 1   # question, error
    assert third["ip"] is None and third["events"] == 0 and third["messages"] == 0
    assert _near(first["last_active"], 5) and _near(first["created_at"], 60)
    for row in rows:
        assert _aware(row["created_at"]) <= _aware(row["last_active"])


async def test_sessions_limit_and_since(client, auth):
    recent = await add_session(minutes_ago=10)
    week_old = await add_session(minutes_ago=3 * 24 * 60)
    ancient = await add_session(minutes_ago=30 * 24 * 60)

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
    if not await db_day_changes(minutes_ago=10):
        assert await ids(since="today") == [recent]


async def test_sessions_since_counts_activity_not_creation(client, auth):
    sid = await add_session(minutes_ago=10 * 24 * 60, active_minutes_ago=5)
    r = await client.get("/admin/sessions", params={"since": "24h"}, headers=auth)
    assert [row["id"] for row in r.json()] == [sid]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 501}, {"limit": "x"}, {"since": "1y"}]
)
async def test_sessions_params_are_validated(client, auth, params):
    assert (await client.get("/admin/sessions", params=params, headers=auth)).status_code == 422


# ---- /admin/sessions/{id} ----


async def test_session_detail_replays_the_chat(client, auth):
    # Created 10 min ago, so it expires 24h after that.
    sid = await add_session("10.0.0.7", minutes_ago=10, active_minutes_ago=2,
                            expires_in_minutes=24 * 60 - 10,
                            referrer_tag="karancodes95.github.io", country="IN")
    await add_event(sid, "session_created", {"vertical": "real_estate"}, 10)
    await add_event(sid, "question_asked", {"text": "hi"}, 10)
    await add_event(sid, "answer_streamed", {"text": "hello", "chars": 5, "ms": 3}, 10)
    await add_event(sid, "turn_complete", {"total_ms": 3}, 10)
    await add_event(sid, "question_asked", {"text": "budget 80L"}, 2)
    await add_event(sid, "error", {"stage": "generate_reply", "message": "TimeoutError"}, 2)
    await _turn(await add_session())   # another session's events stay out

    r = await client.get(f"/admin/sessions/{sid}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == SESSION_DETAIL_KEYS
    assert body["id"] == sid
    assert body["vertical"] == "real_estate"
    assert body["ip"] == "10.0.0.7"
    assert body["referrer_tag"] == "karancodes95.github.io" and body["country"] == "IN"
    assert _near(body["created_at"], 10) and _near(body["last_active"], 2)
    assert _near(body["expires_at"], -(24 * 60 - 10))
    turns = body["turns"]
    assert all(set(t) == TURN_KEYS for t in turns)
    # session_created and turn_complete are not transcript turns.
    assert [(t["role"], t["text"], t["stage"]) for t in turns] == [
        ("user", "hi", None), ("assistant", "hello", None),
        ("user", "budget 80L", None), ("error", "TimeoutError", "generate_reply"),
    ]
    assert _aware(turns[0]["ts"]) < _aware(turns[2]["ts"])


async def test_session_detail_with_no_turns(client, auth):
    sid = await add_session(None, expires_in_minutes=-5)
    body = (await client.get(f"/admin/sessions/{sid}", headers=auth)).json()
    assert body["turns"] == [] and body["ip"] is None
    assert body["referrer_tag"] is None and body["country"] is None
    assert _aware(body["expires_at"]) < datetime.now(timezone.utc)


async def test_session_detail_accepts_an_uppercase_uuid_path(client, auth):
    sid = await add_session()
    r = await client.get(f"/admin/sessions/{sid.upper()}", headers=auth)
    assert r.status_code == 200 and r.json()["id"] == sid


async def test_unknown_session_is_404(client, auth):
    # Old chat_messages rows (from before the sessions table) are not sessions.
    old = str(uuid4())
    await add_message(old, "user", "hi")
    for sid in (old, str(uuid4())):
        r = await client.get(f"/admin/sessions/{sid}", headers=auth)
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "session_not_found"


async def test_non_uuid_session_ids_are_unreachable(client, auth):
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
    if not await db_day_changes(minutes_ago=1):
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
