"""POST /api/chat takes only a server-issued, unexpired session (X-Session-Id),
resumes the agent only through that session's server-side agent id, touches
the session, logs the clinic demo's events, keeps chat_messages and the per-IP
cap. generate_reply is mocked: no test reaches the Claude Agent SDK."""

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app import main, sessions
from app.config import settings
from app.db import SessionLocal
from app.models import ChatMessage, DemoSession, Event, IpUsage
from conftest import add_session, db_day_changes


class FakeReply:
    """Stands in for generate_reply: records each call and answers with a new
    agent id on a first turn, or keeps the resumed one."""

    def __init__(self, reply="Happy to help."):
        self.reply = reply
        self.calls: list[dict] = []

    async def __call__(self, text, session_id, resume=None):
        self.calls.append({"text": text, "session_id": session_id, "resume": resume})
        return self.reply, resume or f"agent-{len(self.calls)}"


@pytest.fixture
def fake(monkeypatch) -> FakeReply:
    f = FakeReply()
    monkeypatch.setattr(main, "generate_reply", f)
    return f


async def _open(client, api_key) -> str:
    r = await client.post("/api/session", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    return r.json()["session_id"]


async def _chat(client, api_key, message, session_id=None, body_extra=None):
    headers = {"X-API-Key": api_key}
    if session_id is not None:
        headers["X-Session-Id"] = session_id
    return await client.post(
        "/api/chat", json={"message": message, **(body_extra or {})}, headers=headers
    )


async def _agent_id(sid: str) -> str | None:
    async with SessionLocal() as s:
        return (await s.get(DemoSession, sid)).agent_session_id


async def _event_types(sid: str | None = None) -> list[str]:
    q = select(Event).order_by(Event.id)
    if sid is not None:
        q = q.where(Event.session_id == sid)
    async with SessionLocal() as s:
        return [e.type for e in (await s.execute(q)).scalars()]


async def _messages() -> list[tuple[str | None, str, str]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    return [(r.session_id, r.role, r.content) for r in rows]


async def _nothing_was_logged() -> bool:
    async with SessionLocal() as s:
        counts = [
            (await s.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
            for t in ("events", "chat_messages", "ip_usage")
        ]
    return counts == [0, 0, 0]


# ---- only a server-issued, unexpired session chats ----


async def test_a_made_up_session_is_refused(client, api_key, fake):
    r = await _chat(client, api_key, "hi", session_id=str(uuid4()))
    assert r.status_code == 404
    assert r.json()["detail"] == {
        "error": "session_not_found", "message": "session missing or expired",
    }
    assert fake.calls == []
    # Refused before the rate limit and before any logging.
    assert await _nothing_was_logged()


async def test_an_expired_session_is_refused(client, api_key, fake):
    expired = await add_session("127.0.0.1", minutes_ago=25 * 60, expires_in_minutes=-1,
                                agent_session_id="agent-old")
    r = await _chat(client, api_key, "continue please", session_id=expired)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "session_not_found"
    assert fake.calls == []
    assert await _nothing_was_logged()


@pytest.mark.parametrize("bad", [None, "", "x", "not-a-uuid", "1234"])
async def test_a_missing_or_malformed_session_header_is_422(client, api_key, fake, bad):
    r = await _chat(client, api_key, "hi", session_id=bad)
    assert r.status_code == 422
    assert fake.calls == []


async def test_a_body_session_id_is_ignored(client, api_key, fake):
    # The old contract (session_id in the body, no header) no longer chats...
    victim = await add_session("10.9.9.9", agent_session_id="agent-victim")
    r = await _chat(client, api_key, "hi", body_extra={"session_id": victim})
    assert r.status_code == 422
    # ...and next to a real header it never picks the chat that resumes.
    mine = await _open(client, api_key)
    r = await _chat(client, api_key, "hi", session_id=mine,
                    body_extra={"session_id": victim})
    assert r.status_code == 200
    assert fake.calls == [{"text": "hi", "session_id": mine, "resume": None}]
    assert await _agent_id(victim) == "agent-victim"
    assert await _event_types(victim) == []


async def test_the_api_key_is_still_required(client, api_key, fake):
    sid = await _open(client, api_key)
    r = await client.post("/api/chat", json={"message": "hi"}, headers={"X-Session-Id": sid})
    assert r.status_code == 401
    assert fake.calls == []


async def test_an_uppercase_form_of_a_real_id_is_the_same_session(client, api_key, fake):
    sid = await _open(client, api_key)
    r = await _chat(client, api_key, "hi", session_id=sid.upper())
    assert r.status_code == 200 and r.json()["session_id"] == sid


# ---- the agent resumes only through the server-side mapping ----


async def test_the_agent_resumes_only_the_sessions_own_agent_id(client, client_from, api_key,
                                                                 fake):
    mine = await _open(client, api_key)
    theirs = await _open(client_from("10.0.0.2"), api_key)
    assert mine != theirs

    r1 = await _chat(client, api_key, "I want a flat", session_id=mine)
    assert r1.json() == {"reply": "Happy to help.", "session_id": mine}
    assert await _agent_id(mine) == "agent-1"
    await _chat(client_from("10.0.0.2"), api_key, "hello", session_id=theirs)
    assert await _agent_id(theirs) == "agent-2"
    await _chat(client, api_key, "in Baner", session_id=mine)

    assert fake.calls == [
        {"text": "I want a flat", "session_id": mine, "resume": None},
        {"text": "hello", "session_id": theirs, "resume": None},
        {"text": "in Baner", "session_id": mine, "resume": "agent-1"},
    ]
    # The agent id never reaches the client.
    assert "agent-" not in r1.text


async def test_a_failed_first_turn_stores_no_agent_id(client, api_key, monkeypatch):
    calls = []

    async def flaky(text, session_id, resume=None):
        calls.append(resume)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "ok", "agent-fresh"

    monkeypatch.setattr(main, "generate_reply", flaky)
    sid = await _open(client, api_key)
    await _chat(client, api_key, "hi", session_id=sid)
    assert await _agent_id(sid) is None
    await _chat(client, api_key, "hi again", session_id=sid)
    assert calls == [None, None]
    assert await _agent_id(sid) == "agent-fresh"


async def test_a_failed_resume_starts_a_new_agent_chat_next_turn(client, api_key,
                                                                  monkeypatch):
    # A resume that fails (e.g. the SDK lost the session) must not fail every
    # turn of the visit: the next turn starts a new agent chat.
    sid = await add_session("127.0.0.1", agent_session_id="agent-lost")
    calls = []

    async def lost_once(text, session_id, resume=None):
        calls.append(resume)
        if resume == "agent-lost":
            raise RuntimeError("no conversation found")
        return "ok", "agent-new"

    monkeypatch.setattr(main, "generate_reply", lost_once)
    await _chat(client, api_key, "hi", session_id=sid)
    assert await _agent_id(sid) is None
    assert (await _chat(client, api_key, "hi again", session_id=sid)).json()["reply"] == "ok"
    assert calls == ["agent-lost", None]
    assert await _agent_id(sid) == "agent-new"


async def test_a_reply_without_an_agent_id_keeps_the_stored_one(client, api_key,
                                                                monkeypatch):
    sid = await add_session("127.0.0.1", agent_session_id="agent-1")

    async def no_result(text, session_id, resume=None):
        return "ok", None

    monkeypatch.setattr(main, "generate_reply", no_result)
    await _chat(client, api_key, "hi", session_id=sid)
    assert await _agent_id(sid) == "agent-1"


# ---- two tabs of one visit: the agent id is compare-and-set ----


async def _until_agent_id(sid: str, value: str | None) -> None:
    """Wait (bounded) until the session's stored agent id is ``value``."""
    for _ in range(200):
        if await _agent_id(sid) == value:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"agent id never became {value!r}")


async def test_a_parallel_first_turn_does_not_overwrite_the_saved_agent_id(
    client, api_key, monkeypatch
):
    sid = await _open(client, api_key)
    both_started = asyncio.Event()
    calls = []

    async def reply(text, session_id, resume=None):
        calls.append(resume)
        if text == "tab one":
            await both_started.wait()
            return "one", "agent-1"
        # Tab two started from the same empty row, then finishes after tab
        # one saved its id: its own id must not replace it.
        both_started.set()
        await _until_agent_id(sid, "agent-1")
        return "two", "agent-2"

    monkeypatch.setattr(main, "generate_reply", reply)
    one, two = await asyncio.gather(
        _chat(client, api_key, "tab one", session_id=sid),
        _chat(client, api_key, "tab two", session_id=sid),
    )
    assert (one.json()["reply"], two.json()["reply"]) == ("one", "two")
    assert calls == [None, None]
    assert await _agent_id(sid) == "agent-1"


async def test_a_failed_turn_does_not_clear_an_id_a_parallel_turn_saved(
    client, api_key, monkeypatch
):
    sid = await add_session("127.0.0.1", agent_session_id="agent-x")
    both_started = asyncio.Event()

    async def reply(text, session_id, resume=None):
        if text == "good tab":
            await both_started.wait()
            return "ok", "agent-y"   # the SDK reported a new id for this visit
        both_started.set()
        await _until_agent_id(sid, "agent-y")
        raise RuntimeError("the failing tab")

    monkeypatch.setattr(main, "generate_reply", reply)
    await asyncio.gather(
        _chat(client, api_key, "good tab", session_id=sid),
        _chat(client, api_key, "bad tab", session_id=sid),
    )
    # The failed turn's clear expected "agent-x" and found "agent-y": no write.
    assert await _agent_id(sid) == "agent-y"


async def test_set_agent_session_writes_only_over_the_expected_id():
    sid = await add_session(agent_session_id=None)
    assert await sessions.set_agent_session(UUID(sid), "a1", expected=None) is True
    assert await sessions.set_agent_session(UUID(sid), "a2", expected=None) is False
    assert await _agent_id(sid) == "a1"
    assert await sessions.set_agent_session(UUID(sid), None, expected="stale") is False
    assert await sessions.set_agent_session(UUID(sid), None, expected="a1") is True
    assert await _agent_id(sid) is None


# ---- touch and events ----


async def test_a_turn_touches_the_session(client, api_key, fake):
    sid = await add_session("127.0.0.1", minutes_ago=60)
    async with SessionLocal() as s:
        before = await s.get(DemoSession, sid)
    await _chat(client, api_key, "hi", session_id=sid)
    async with SessionLocal() as s:
        visit = await s.get(DemoSession, sid)
    assert visit.last_active > before.last_active
    # Activity does not extend the expiry (as in the clinic demo).
    assert visit.expires_at == before.expires_at


async def test_a_good_turn_logs_question_answer_and_turn_complete(client, api_key, fake):
    sid = await _open(client, api_key)
    await _chat(client, api_key, "Any 2BHK in Baner?", session_id=sid)
    async with SessionLocal() as s:
        rows = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
    assert [(str(e.session_id), e.type) for e in rows] == [
        (sid, "session_created"), (sid, "question_asked"), (sid, "answer_streamed"),
        (sid, "turn_complete"),
    ]
    _, question, answer, done = rows
    assert question.payload == {"text": "Any 2BHK in Baner?"}
    assert answer.payload["text"] == "Happy to help."
    assert answer.payload["chars"] == len("Happy to help.")
    assert isinstance(answer.payload["ms"], int)
    assert isinstance(done.payload["total_ms"], int)
    assert question.ts.tzinfo is not None and question.ts <= answer.ts <= done.ts
    # The plain transcript is still logged, under the server-issued id.
    assert await _messages() == [
        (sid, "user", "Any 2BHK in Baner?"), (sid, "assistant", "Happy to help."),
    ]


async def test_an_empty_reply_logs_the_fallback(client, api_key, monkeypatch):
    monkeypatch.setattr(main, "generate_reply", FakeReply(reply=""))
    sid = await _open(client, api_key)
    body = (await _chat(client, api_key, "hmm", session_id=sid)).json()
    assert body["reply"] == "Sorry, I didn't catch that. Try again?"
    async with SessionLocal() as s:
        answer = (
            await s.execute(select(Event).where(Event.type == "answer_streamed"))
        ).scalar_one()
    assert answer.payload["text"] == body["reply"]


async def test_event_logging_failure_never_breaks_the_reply(client, api_key, fake, monkeypatch):
    sid = await _open(client, api_key)

    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr("app.events.Event", Broken)
    r = await _chat(client, api_key, "hi", session_id=sid)
    assert r.status_code == 200 and r.json()["reply"] == "Happy to help."


# ---- the per-IP cap still applies ----


async def test_the_daily_ip_cap_still_applies(client, api_key, fake, monkeypatch):
    monkeypatch.setattr(settings, "chat_daily_ip_cap", 1)
    sid = await _open(client, api_key)
    first = (await _chat(client, api_key, "one", session_id=sid)).json()
    second = (await _chat(client, api_key, "two", session_id=sid)).json()
    assert first["reply"] == "Happy to help."
    assert second["reply"].startswith("You've reached today's message limit")
    assert second["session_id"] == sid
    assert [c["text"] for c in fake.calls] == ["one"]
    # The capped message is neither a question nor a transcript row.
    assert await _event_types(sid) == [
        "session_created", "question_asked", "answer_streamed", "turn_complete",
    ]
    assert [content for _, _, content in await _messages()] == ["one", "Happy to help."]
    async with SessionLocal() as s:
        usage = (await s.execute(select(IpUsage))).scalar_one()
    assert (usage.ip, usage.count, usage.blocked) == ("127.0.0.1", 1, 1)


# ---- end to end: what the admin API then reports ----


async def test_a_visit_shows_up_in_every_admin_field(client, client_from, api_key, auth,
                                                     monkeypatch):
    if await db_day_changes(minutes_ahead=2):
        pytest.skip("too close to the DB's midnight for a 'today' window")
    calls = []

    async def reply(text, session_id, resume=None):
        calls.append(text)
        if text == "fail":
            raise TimeoutError("internal detail that must not be stored")
        return f"re: {text}", resume or "agent-1"

    monkeypatch.setattr(main, "generate_reply", reply)
    r = await client.post(
        "/api/session", params={"referrer_tag": "linkedin", "country": "IN"},
        headers={"X-API-Key": api_key},
    )
    sid = r.json()["session_id"]
    await _chat(client, api_key, "hi", session_id=sid)
    await _chat(client, api_key, "fail", session_id=sid)
    # A second visitor opens the page and leaves without asking.
    await _open(client_from("10.0.0.2"), api_key)

    metrics = (await client.get("/admin/metrics", headers=auth)).json()
    assert metrics == {
        "scope": "today", "sessions_today": 2, "messages_today": 2,
        "active_sessions": 2, "errors_24h": 1, "unique_visitors_today": 2,
        "engaged_visitors_today": 1, "bounce_rate_today": 0.5,
        "avg_messages_per_engaged": 2.0,
    }

    rows = (await client.get("/admin/sessions", headers=auth)).json()
    mine = next(row for row in rows if row["id"] == sid)
    assert mine["ip"] == "127.0.0.1"
    assert mine["referrer_tag"] == "linkedin" and mine["country"] == "IN"
    # session_created, 2 x question_asked, answer_streamed, turn_complete, error
    assert mine["events"] == 6 and mine["messages"] == 2

    detail = (await client.get(f"/admin/sessions/{sid}", headers=auth)).json()
    assert detail["expires_at"] is not None
    assert (datetime.fromisoformat(detail["expires_at"])
            - datetime.fromisoformat(detail["created_at"])).total_seconds() == pytest.approx(
        24 * 3600, abs=60)
    assert [(t["role"], t["text"], t["stage"]) for t in detail["turns"]] == [
        ("user", "hi", None), ("assistant", "re: hi", None),
        ("user", "fail", None), ("error", "TimeoutError", "generate_reply"),
    ]
