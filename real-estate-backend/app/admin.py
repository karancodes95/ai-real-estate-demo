"""Admin read API for bytcra-admin: the clinic/winery demos' /admin routes
(same params, limits and field names), computed from this demo's tables.

    GET /admin/metrics?scope=today|all
    GET /admin/sessions?limit=1..500&since=all|today|24h|7d
    GET /admin/sessions/{session_id}      (UUID)
    GET /admin/leads?limit=1..500&since=today|24h|7d|all   (this demo's LeadOut)

There is no sessions table: a session is the chat_messages rows sharing one
Claude Agent SDK session_id. Only UUID-shaped ids count as sessions; NULL or
other ids (a failed turn logs whatever the client sent) are left out of the
session routes. Messages are role="user" rows, errors role="error" rows.

What this demo does not measure comes back as null, never a made-up number:
bounce_rate_today (the static site sends no page-view signal, so every visitor
seen has chatted), referrer_tag and country (not captured), expires_at (SDK
sessions do not expire here) and the stage of non-error turns.

Times: the columns are timestamps without zone, written by the DB's now(), so
they are compared against LOCALTIMESTAMP and returned as ISO 8601 with an
offset via ``AT TIME ZONE current_setting('TimeZone')``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from .admin_auth import verify_admin
from .db import SessionLocal
from .schemas import LeadOut

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_admin)])

VERTICAL = "real_estate"
# Canonical lowercase UUID, the form the SDK issues and str(UUID) produces.
UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The one place a turn can fail (app.main.chat around generate_reply).
ERROR_STAGE = "generate_reply"

_TODAY = "date_trunc('day', LOCALTIMESTAMP)"
# A naive column as an aware timestamp (asyncpg returns it tz-aware).
_TZ = "AT TIME ZONE current_setting('TimeZone')"


def _iso(value: datetime) -> str:
    return value.isoformat()


class Metrics(BaseModel):
    # Same fields as the clinic demo's Metrics. With scope=all the "_today"
    # fields carry all-time values; `scope` says which is in the payload.
    scope: str  # "today" | "all"
    sessions_today: int
    messages_today: int
    active_sessions: int  # always last 15 min (scope-independent)
    errors_24h: int  # always last 24h (scope-independent)
    unique_visitors_today: int
    engaged_visitors_today: int
    bounce_rate_today: float | None  # not measured here: always null
    avg_messages_per_engaged: float


class SessionRow(BaseModel):
    id: str
    created_at: str
    last_active: str
    vertical: str
    ip: str | None = None
    referrer_tag: str | None = None  # not captured: always null
    country: str | None = None  # not captured: always null
    events: int
    messages: int


class TranscriptTurn(BaseModel):
    ts: str
    role: str  # "user" | "assistant" | "error"
    text: str
    stage: str | None = None  # ERROR_STAGE on error turns, else null


class SessionDetail(BaseModel):
    id: str
    created_at: str
    last_active: str
    expires_at: str | None = None  # no expiry here: always null
    vertical: str
    ip: str | None = None
    referrer_tag: str | None = None
    country: str | None = None
    turns: list[TranscriptTurn]


@router.get("/metrics", response_model=Metrics)
async def metrics(
    scope: str = Query(default="today", pattern="^(today|all)$"),
) -> Metrics:
    """KPIs for the admin dashboard.

    scope="today" (default): rows written today (DB-local day). A visitor is
    a distinct client IP with any chat row in scope; an engaged visitor one
    with at least one message. A session counts on the day of its first row.
    scope="all": the same over every row kept.
    `active_sessions` (last 15 min) and `errors_24h` (last 24h) are always
    windowed regardless of scope - they're live-ops signals, not totals.
    """
    rows_filter = f"WHERE created_at >= {_TODAY}" if scope == "today" else ""
    sessions_filter = f"WHERE first_at >= {_TODAY}" if scope == "today" else ""
    sql = f"""
        WITH scoped AS (
          SELECT role, client_ip FROM chat_messages {rows_filter}
        ),
        sessions AS (
          SELECT session_id, MIN(created_at) AS first_at, MAX(created_at) AS last_at
          FROM chat_messages
          WHERE session_id ~ :uuid_re
          GROUP BY session_id
        ),
        msgs_by_ip AS (
          SELECT client_ip, COUNT(*)::int AS msgs
          FROM scoped
          WHERE role = 'user' AND client_ip <> ''
          GROUP BY client_ip
        )
        SELECT
          (SELECT COUNT(*) FROM sessions {sessions_filter}) AS sessions_today,
          (SELECT COUNT(*) FROM scoped WHERE role = 'user') AS messages_today,
          (SELECT COUNT(DISTINCT client_ip) FROM scoped WHERE client_ip <> '')
            AS unique_visitors_today,
          (SELECT COUNT(*) FROM msgs_by_ip) AS engaged_visitors_today,
          (SELECT COALESCE(AVG(msgs)::float, 0) FROM msgs_by_ip)
            AS avg_messages_per_engaged,
          (SELECT COUNT(*) FROM sessions
             WHERE last_at >= LOCALTIMESTAMP - INTERVAL '15 minutes') AS active_sessions,
          (SELECT COUNT(*) FROM chat_messages
             WHERE role = 'error'
               AND created_at >= LOCALTIMESTAMP - INTERVAL '24 hours') AS errors_24h
    """
    async with SessionLocal() as s:
        row = (await s.execute(text(sql), {"uuid_re": UUID_RE})).mappings().one()

    return Metrics(
        scope=scope,
        sessions_today=row["sessions_today"] or 0,
        messages_today=row["messages_today"] or 0,
        active_sessions=row["active_sessions"] or 0,
        errors_24h=row["errors_24h"] or 0,
        unique_visitors_today=row["unique_visitors_today"] or 0,
        engaged_visitors_today=row["engaged_visitors_today"] or 0,
        bounce_rate_today=None,
        avg_messages_per_engaged=round(row["avg_messages_per_engaged"] or 0.0, 2),
    )


@router.get("/sessions", response_model=list[SessionRow])
async def list_sessions(
    limit: int = Query(default=500, ge=1, le=500),
    since: str = Query(default="all", pattern="^(all|today|24h|7d)$"),
) -> list[SessionRow]:
    # "today" / "24h" / "7d" mean *active in that window* (like the clinic
    # demo): first or last row inside it. "all" returns every session.
    def window(start: str) -> str:
        return f"(MIN(created_at) >= {start} OR MAX(created_at) >= {start})"

    having = {
        "all": "TRUE",
        "today": window(_TODAY),
        "24h": window("LOCALTIMESTAMP - INTERVAL '24 hours'"),
        "7d": window("LOCALTIMESTAMP - INTERVAL '7 days'"),
    }[since]
    sql = f"""
        SELECT
          session_id AS id,
          MIN(created_at) {_TZ} AS created_at,
          MAX(created_at) {_TZ} AS last_active,
          (ARRAY_AGG(client_ip ORDER BY created_at DESC, id DESC)
             FILTER (WHERE client_ip <> ''))[1] AS ip,
          COUNT(*) AS events,
          COUNT(*) FILTER (WHERE role = 'user') AS messages
        FROM chat_messages
        WHERE session_id ~ :uuid_re
        GROUP BY session_id
        HAVING {having}
        ORDER BY MAX(created_at) DESC, session_id
        LIMIT :limit
    """
    async with SessionLocal() as s:
        rows = (
            (await s.execute(text(sql), {"uuid_re": UUID_RE, "limit": limit})).mappings().all()
        )
    return [
        SessionRow(
            id=r["id"],
            created_at=_iso(r["created_at"]),
            last_active=_iso(r["last_active"]),
            vertical=VERTICAL,
            ip=r["ip"],
            events=r["events"],
            messages=r["messages"],
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def session_detail(session_id: UUID) -> SessionDetail:
    # str(UUID) is the canonical lowercase form, so only UUID-shaped ids match.
    sql = f"""
        SELECT role, content, client_ip, created_at {_TZ} AS ts
        FROM chat_messages
        WHERE session_id = :sid
        ORDER BY created_at ASC, id ASC
    """
    async with SessionLocal() as s:
        rows = (await s.execute(text(sql), {"sid": str(session_id)})).mappings().all()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found", "message": "session not found"},
        )

    turns = [
        TranscriptTurn(
            ts=_iso(r["ts"]),
            role=r["role"],
            text=r["content"],
            stage=ERROR_STAGE if r["role"] == "error" else None,
        )
        for r in rows
    ]
    ips = [r["client_ip"] for r in rows if r["client_ip"]]
    return SessionDetail(
        id=str(session_id),
        created_at=_iso(rows[0]["ts"]),
        last_active=_iso(rows[-1]["ts"]),
        expires_at=None,
        vertical=VERTICAL,
        ip=ips[-1] if ips else None,
        turns=turns,
    )


@router.get("/leads", response_model=list[LeadOut])
async def list_leads(
    limit: int = Query(default=100, ge=1, le=500),
    since: str = Query(default="all", pattern="^(today|24h|7d|all)$"),
) -> list[LeadOut]:
    """This demo's own lead shape (as /api/crm/leads), in its order (best
    score first, then newest), with created_at as ISO 8601 with an offset."""
    where = {
        "today": f"WHERE created_at >= {_TODAY}",
        "24h": "WHERE created_at >= LOCALTIMESTAMP - INTERVAL '24 hours'",
        "7d": "WHERE created_at >= LOCALTIMESTAMP - INTERVAL '7 days'",
        "all": "",
    }[since]
    sql = f"""
        SELECT id, name, email, phone, budget, location, property_type,
               timeline_months, financing, intent, score, grade, rationale,
               status, created_at {_TZ} AS created_at
        FROM leads
        {where}
        ORDER BY score DESC, id DESC
        LIMIT :limit
    """
    async with SessionLocal() as s:
        rows = (await s.execute(text(sql), {"limit": limit})).mappings().all()
    return [LeadOut(**{**r, "created_at": _iso(r["created_at"])}) for r in rows]
