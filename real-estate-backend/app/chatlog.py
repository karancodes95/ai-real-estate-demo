"""Persist each chat turn to Postgres — one row per message (user + assistant).

A turn whose reply generation failed also gets a ``role="error"`` row, between
the two, holding only the exception's class name (never its message). The
admin API counts and replays those rows like the clinic demo's error events.

Best-effort: the caller wraps this so a logging failure never breaks a reply.
"""

from .db import SessionLocal
from .models import ChatMessage


async def log_turn(
    session_id: str | None,
    client_ip: str,
    user_message: str,
    bot_reply: str,
    error: str | None = None,
) -> None:
    """Store the user's message, the error (if any) and the assistant's reply."""
    rows = [
        ChatMessage(session_id=session_id, role="user", content=user_message, client_ip=client_ip)
    ]
    if error:
        rows.append(
            ChatMessage(session_id=session_id, role="error", content=error, client_ip=client_ip)
        )
    rows.append(
        ChatMessage(session_id=session_id, role="assistant", content=bot_reply, client_ip=client_ip)
    )
    async with SessionLocal() as s:
        s.add_all(rows)
        await s.commit()
