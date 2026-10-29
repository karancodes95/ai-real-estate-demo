"""A failed chat turn logs an error event (stage + class name only) and a
role="error" chat_messages row, and the admin API counts and replays it; a
good turn logs neither. generate_reply is mocked: no test reaches the Claude
Agent SDK."""

from sqlalchemy import select, text

from app import main
from app.db import SessionLocal
from app.models import ChatMessage, Event

APOLOGY = "Sorry, I'm having trouble right now. Please try again."
SECRET = "internal detail that must not be stored"


async def _rows() -> list[tuple[str | None, str, str]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    return [(r.session_id, r.role, r.content) for r in rows]


async def _events() -> list[tuple[str, dict]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(Event).order_by(Event.id))).scalars().all()
    return [(e.type, e.payload) for e in rows]


async def _open(client, api_key) -> str:
    r = await client.post("/api/session", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    return r.json()["session_id"]


async def _chat(client, api_key, message, session_id):
    r = await client.post(
        "/api/chat",
        json={"message": message},
        headers={"X-API-Key": api_key, "X-Session-Id": session_id},
    )
    assert r.status_code == 200
    return r.json()


async def test_failed_turn_logs_an_error_with_the_class_name_only(
    client, api_key, auth, monkeypatch
):
    async def boom(text, session_id, resume=None):
        raise TimeoutError(SECRET)

    monkeypatch.setattr(main, "generate_reply", boom)
    sid = await _open(client, api_key)
    body = await _chat(client, api_key, "Any 2BHK in Baner?", sid)
    # The visitor's answer is unchanged.
    assert body == {"reply": APOLOGY, "session_id": sid}
    assert await _rows() == [
        (sid, "user", "Any 2BHK in Baner?"),
        (sid, "error", "TimeoutError"),
        (sid, "assistant", APOLOGY),
    ]
    # No answer and no finished turn, as in the clinic demo's failed turn.
    assert [(t, p) for t, p in await _events() if t != "session_created"] == [
        ("question_asked", {"text": "Any 2BHK in Baner?"}),
        ("error", {"stage": "generate_reply", "message": "TimeoutError"}),
    ]
    async with SessionLocal() as s:
        dump = (await s.execute(text(
            "SELECT string_agg(payload::text, ' ') FROM events"
        ))).scalar_one()
    assert SECRET not in dump

    metrics = (await client.get("/admin/metrics", headers=auth)).json()
    assert metrics["errors_24h"] == 1
    assert metrics["messages_today"] == 1
    detail = (await client.get(f"/admin/sessions/{sid}", headers=auth)).json()
    assert [(t["role"], t["text"], t["stage"]) for t in detail["turns"]] == [
        ("user", "Any 2BHK in Baner?", None),
        ("error", "TimeoutError", "generate_reply"),
    ]


async def test_failed_first_turn_still_lists_its_session(client, api_key, auth, monkeypatch):
    async def boom(text, session_id, resume=None):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "generate_reply", boom)
    sid = await _open(client, api_key)
    assert (await _chat(client, api_key, "hello", sid))["session_id"] == sid
    (row,) = (await client.get("/admin/sessions", headers=auth)).json()
    assert row["id"] == sid and row["messages"] == 1
    assert (await client.get("/admin/metrics", headers=auth)).json()["errors_24h"] == 1


async def test_good_turn_logs_no_error(client, api_key, auth, monkeypatch):
    async def reply(text, session_id, resume=None):
        return "Happy to help.", "agent-1"

    monkeypatch.setattr(main, "generate_reply", reply)
    sid = await _open(client, api_key)
    body = await _chat(client, api_key, "hi", sid)
    assert body == {"reply": "Happy to help.", "session_id": sid}
    assert await _rows() == [(sid, "user", "hi"), (sid, "assistant", "Happy to help.")]
    assert "error" not in [t for t, _ in await _events()]
    assert (await client.get("/admin/metrics", headers=auth)).json()["errors_24h"] == 0
    (row,) = (await client.get("/admin/sessions", headers=auth)).json()
    # session_created, question_asked, answer_streamed, turn_complete
    assert row["id"] == sid and row["messages"] == 1 and row["events"] == 4
