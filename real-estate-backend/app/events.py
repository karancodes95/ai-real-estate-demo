"""Structured event logging to the ``events`` table, one row per event, with
the clinic demo's event types:

    session_created  {vertical, referrer_tag, country}
    question_asked   {text}
    answer_streamed  {text, chars, ms}
    error            {stage, message}   message is the exception's class name
    turn_complete    {total_ms}

The admin API computes its metrics and transcripts from these rows.

Best-effort like the clinic demo's events.log: a failed insert is logged and
swallowed, never raised, so logging can't break a chat reply. Text is stored
verbatim (as chat_messages already does); see SECURITY.md.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from .db import SessionLocal
from .models import Event

logger = logging.getLogger("uvicorn.error")


async def log(session_id: UUID, event_type: str, payload: dict[str, Any] | None = None) -> None:
    try:
        async with SessionLocal() as s:
            s.add(Event(session_id=session_id, type=event_type, payload=payload or {}))
            await s.commit()
    except Exception:  # never let logging crash the request
        logger.exception("event log failed (event_type=%s)", event_type)
