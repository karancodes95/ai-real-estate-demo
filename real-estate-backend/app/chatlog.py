"""Persist each chat turn to Postgres — one row per message (user + assistant).

Best-effort: the caller wraps this so a logging failure never breaks a reply.
"""

from .db import SessionLocal
from .models import ChatMessage


async def log_turn(
    session_id: str | None, client_ip: str, user_message: str, bot_reply: str
) -> None:
    """Store the user's message and the assistant's reply as two rows."""
    async with SessionLocal() as s:
        s.add_all(
            [
                ChatMessage(
                    session_id=session_id, role="user", content=user_message, client_ip=client_ip
                ),
                ChatMessage(
                    session_id=session_id, role="assistant", content=bot_reply, client_ip=client_ip
                ),
            ]
        )
        await s.commit()
