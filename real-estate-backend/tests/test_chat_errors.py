"""A failed chat turn logs a role="error" row (class name only) and the admin
API counts and replays it; a good turn logs no error row. generate_reply is
mocked: no test reaches the Claude Agent SDK."""

from uuid import uuid4

from sqlalchemy import select

from app import main
from app.db import SessionLocal
from app.models import ChatMessage

APOLOGY = "Sorry, I'm having trouble right now. Please try again."


async def _rows() -> list[tuple[str | None, str, str]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    return [(r.session_id, r.role, r.content) for r in rows]


async def _chat(client, api_key, message, session_id=None):
    r = await client.post(
        "/api/chat",
        json={"message": message, "session_id": session_id},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200
    return r.json()


async def test_failed_turn_logs_an_error_row_with_the_class_name_only(
    client, api_key, auth, monkeypatch
):
    sid = str(uuid4())

    async def boom(text, resume=None):
        raise TimeoutError("internal detail that must not be stored")

    monkeypatch.setattr(main, "generate_reply", boom)
    body = await _chat(client, api_key, "Any 2BHK in Baner?", session_id=sid)
    # The visitor's answer is unchanged.
    assert body == {"reply": APOLOGY, "session_id": sid}
    assert await _rows() == [
        (sid, "user", "Any 2BHK in Baner?"),
        (sid, "error", "TimeoutError"),
        (sid, "assistant", APOLOGY),
    ]

    metrics = (await client.get("/admin/metrics", headers=auth)).json()
    assert metrics["errors_24h"] == 1
    assert metrics["messages_today"] == 1
    detail = (await client.get(f"/admin/sessions/{sid}", headers=auth)).json()
    assert [(t["role"], t["text"], t["stage"]) for t in detail["turns"]] == [
        ("user", "Any 2BHK in Baner?", None),
        ("error", "TimeoutError", "generate_reply"),
        ("assistant", APOLOGY, None),
    ]


async def test_failed_first_turn_has_no_session(client, api_key, auth, monkeypatch):
    async def boom(text, resume=None):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "generate_reply", boom)
    body = await _chat(client, api_key, "hello")
    assert body["session_id"] is None
    assert [role for _, role, _ in await _rows()] == ["user", "error", "assistant"]
    # Counted as an error, but there is no session to list.
    assert (await client.get("/admin/metrics", headers=auth)).json()["errors_24h"] == 1
    assert (await client.get("/admin/sessions", headers=auth)).json() == []


async def test_good_turn_logs_no_error_row(client, api_key, auth, monkeypatch):
    sid = str(uuid4())

    async def reply(text, resume=None):
        return "Happy to help.", sid

    monkeypatch.setattr(main, "generate_reply", reply)
    body = await _chat(client, api_key, "hi")
    assert body == {"reply": "Happy to help.", "session_id": sid}
    assert await _rows() == [(sid, "user", "hi"), (sid, "assistant", "Happy to help.")]
    assert (await client.get("/admin/metrics", headers=auth)).json()["errors_24h"] == 0
    (row,) = (await client.get("/admin/sessions", headers=auth)).json()
    assert row["id"] == sid and row["messages"] == 1 and row["events"] == 2
