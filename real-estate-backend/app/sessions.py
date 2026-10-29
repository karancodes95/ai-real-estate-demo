"""Demo visit sessions (the clinic demo's app/core/sessions.py).

POST /api/session issues the id and /api/chat accepts only an unexpired one
(get_live), so a client can no longer pick the chat it continues: the agent
resumes only the SDK session stored on the row (agent_session_id). An id is
valid for SESSION_TTL from creation; activity moves last_active, not the
expiry, as in the clinic demo. Rows are kept after expiry for the admin API.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from . import events
from .db import SessionLocal
from .models import DemoSession

SESSION_TTL = timedelta(hours=24)
VERTICAL = "real_estate"


def inet_or_none(ip: str | None) -> str | None:
    """``ip`` in canonical form, or None when it is no visitor address: not an
    address at all, or the unspecified 0.0.0.0 / :: that client_ip answers for
    an unparseable peer. None stores no IP and reuses no session, so peers
    without an address never share one chat."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return None if addr.is_unspecified else str(addr)


async def get_active_for_ip(*, ip: str | None, vertical: str) -> UUID | None:
    """The most recently active unexpired session for this (ip, vertical), or
    None. Lets a visitor keep one chat across tabs and devices on a network."""
    if not ip:
        return None
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(DemoSession.id)
                .where(
                    DemoSession.ip == ip,
                    DemoSession.vertical == vertical,
                    DemoSession.expires_at > func.now(),
                )
                .order_by(DemoSession.last_active.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def create_session(
    *,
    vertical: str,
    referrer_tag: str | None,
    country: str | None,
    ip: str | None = None,
) -> UUID:
    session_id = uuid4()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    async with SessionLocal() as s:
        s.add(
            DemoSession(
                id=session_id,
                vertical=vertical,
                referrer_tag=referrer_tag,
                country=country,
                expires_at=expires_at,
                ip=ip,
            )
        )
        await s.commit()
    await events.log(session_id, "session_created", {
        "vertical": vertical,
        "referrer_tag": referrer_tag,
        "country": country,
    })
    return session_id


async def get_live(session_id: UUID) -> DemoSession | None:
    """The session if it exists and has not expired, else None."""
    async with SessionLocal() as s:
        return (
            await s.execute(
                select(DemoSession).where(
                    DemoSession.id == session_id, DemoSession.expires_at > func.now()
                )
            )
        ).scalar_one_or_none()


async def touch_session(session_id: UUID) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(DemoSession).where(DemoSession.id == session_id).values(last_active=func.now())
        )
        await s.commit()


async def set_agent_session(
    session_id: UUID, agent_session_id: str | None, *, expected: str | None
) -> bool:
    """Remember the SDK session this visit's next turn resumes (None: a new
    one), only if the row still holds ``expected``, the id this turn started
    from. Compare-and-set, so a turn in a second tab of the same visit never
    overwrites (or clears) an id a parallel turn saved meanwhile. True if
    written."""
    async with SessionLocal() as s:
        result = await s.execute(
            update(DemoSession)
            .where(
                DemoSession.id == session_id,
                DemoSession.agent_session_id.is_not_distinct_from(expected),
            )
            .values(agent_session_id=agent_session_id)
        )
        await s.commit()
    return result.rowcount == 1
