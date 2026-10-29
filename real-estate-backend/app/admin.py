"""Admin read API for bytcra-admin: the clinic/winery demos' /admin routes
(same params, limits, field names and SQL), computed from this demo's
``sessions`` and ``events`` tables (app/models.py) like the clinic demo's.

    GET /admin/metrics?scope=today|all
    GET /admin/sessions?limit=1..500&since=all|today|24h|7d
    GET /admin/sessions/{session_id}      (UUID)
    GET /admin/leads?limit=1..500&since=today|24h|7d|all   (this demo's LeadOut)

A session is one visit, opened by POST /api/session (one per IP while it is
unexpired). A visitor is a distinct session IP, an engaged visitor one that
asked a question (a question_asked event), and the bounce rate the share of
visitors who never did. Transcripts replay the question_asked, answer_streamed
and error events; only error turns carry a stage. Visits from before the
sessions table existed are only in chat_messages and are not listed here.

Times: sessions and events are timestamptz (compared against NOW()); leads
are timestamps without zone, written by the DB's now(), so they are compared
against LOCALTIMESTAMP and returned as ISO 8601 with an offset via
``AT TIME ZONE current_setting('TimeZone')``.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from .admin_auth import verify_admin
from .db import SessionLocal
from .schemas import LeadOut

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_admin)])

# For the leads route (naive timestamps).
_TODAY = "date_trunc('day', LOCALTIMESTAMP)"
# A naive column as an aware timestamp (asyncpg returns it tz-aware).
_TZ = "AT TIME ZONE current_setting('TimeZone')"


def _iso(value: datetime) -> str:
    return value.isoformat()


def _ip(value) -> str | None:
    # asyncpg returns INET as an ipaddress object.
    return str(value) if value is not None else None


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
    bounce_rate_today: float  # 0..1, computed over unique IPs
    avg_messages_per_engaged: float


class SessionRow(BaseModel):
    id: str
    created_at: str
    last_active: str
    vertical: str
    ip: str | None = None
    referrer_tag: str | None = None
    country: str | None = None
    events: int
    messages: int


class TranscriptTurn(BaseModel):
    ts: str
    role: str  # "user" | "assistant" | "error"
    text: str
    stage: str | None = None  # set on error turns only


class SessionDetail(BaseModel):
    id: str
    created_at: str
    last_active: str
    expires_at: str
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

    scope="today" (default): metrics are filtered to today's activity.
      Visitor = distinct IP with any activity today (session created today
      OR any event timestamped today). Returning visitors whose session was
      created on a prior day are still counted, because the backend reuses
      sessions per IP for 24h.
    scope="all": the same metrics over every row kept.
    `active_sessions` (last 15 min) and `errors_24h` (last 24h) are always
    windowed regardless of scope - they're live-ops signals, not totals.
    """
    if scope == "today":
        ip_filter = (
            "AND (s.created_at >= date_trunc('day', NOW()) "
            "     OR e.ts        >= date_trunc('day', NOW()))"
        )
        msg_filter = "AND e.ts >= date_trunc('day', NOW())"
        sessions_filter = "WHERE created_at >= date_trunc('day', NOW())"
        events_filter = "AND ts >= date_trunc('day', NOW())"
    else:
        ip_filter = ""
        msg_filter = ""
        sessions_filter = ""
        events_filter = ""

    sql = f"""
        WITH visitor_ips AS (
          SELECT DISTINCT s.ip
          FROM sessions s
          LEFT JOIN events e ON e.session_id = s.id
          WHERE s.ip IS NOT NULL
            {ip_filter}
        ),
        msgs_by_ip AS (
          SELECT s.ip, COUNT(*)::int AS msgs
          FROM events e
          JOIN sessions s ON s.id = e.session_id
          WHERE e.type = 'question_asked'
            AND s.ip IS NOT NULL
            {msg_filter}
          GROUP BY s.ip
        )
        SELECT
          (SELECT COUNT(*) FROM sessions {sessions_filter}) AS sessions_today,
          (SELECT COUNT(*) FROM events
             WHERE type = 'question_asked' {events_filter}) AS messages_today,
          (SELECT COUNT(*) FROM visitor_ips) AS unique_visitors_today,
          (SELECT COUNT(*) FROM msgs_by_ip) AS engaged_visitors_today,
          (SELECT COALESCE(AVG(msgs)::float, 0) FROM msgs_by_ip)
            AS avg_messages_per_engaged,
          (SELECT COUNT(*) FROM sessions
             WHERE last_active >= NOW() - INTERVAL '15 minutes'
               AND expires_at > NOW()) AS active_sessions,
          (SELECT COUNT(*) FROM events
             WHERE type = 'error'
               AND ts >= NOW() - INTERVAL '24 hours') AS errors_24h
    """
    async with SessionLocal() as s:
        row = (await s.execute(text(sql))).mappings().one()

    visitors = row["unique_visitors_today"] or 0
    engaged = row["engaged_visitors_today"] or 0
    bounce_rate = (1.0 - (engaged / visitors)) if visitors > 0 else 0.0

    return Metrics(
        scope=scope,
        sessions_today=row["sessions_today"] or 0,
        messages_today=row["messages_today"] or 0,
        active_sessions=row["active_sessions"] or 0,
        errors_24h=row["errors_24h"] or 0,
        unique_visitors_today=visitors,
        engaged_visitors_today=engaged,
        bounce_rate_today=round(bounce_rate, 4),
        avg_messages_per_engaged=round(row["avg_messages_per_engaged"] or 0.0, 2),
    )


@router.get("/sessions", response_model=list[SessionRow])
async def list_sessions(
    limit: int = Query(default=500, ge=1, le=500),
    since: str = Query(default="all", pattern="^(all|today|24h|7d)$"),
) -> list[SessionRow]:
    # "Today" / "24h" / "7d" mean *active in that window*, not *created in*:
    # with IP-based session reuse, a returning visitor keeps a session row
    # created on a prior day. "all" returns every session (capped by `limit`).
    where = {
        "all": "TRUE",
        "today": "(s.created_at >= date_trunc('day', NOW())"
                 " OR s.last_active >= date_trunc('day', NOW()))",
        "24h": "(s.created_at >= NOW() - INTERVAL '24 hours'"
               " OR s.last_active >= NOW() - INTERVAL '24 hours')",
        "7d": "(s.created_at >= NOW() - INTERVAL '7 days'"
              " OR s.last_active >= NOW() - INTERVAL '7 days')",
    }[since]
    sql = f"""
        SELECT
          s.id,
          s.created_at,
          s.last_active,
          s.vertical,
          s.ip,
          s.referrer_tag,
          s.country,
          COUNT(e.id) AS events,
          COUNT(e.id) FILTER (WHERE e.type = 'question_asked') AS messages
        FROM sessions s
        LEFT JOIN events e ON e.session_id = s.id
        WHERE {where}
        GROUP BY s.id
        ORDER BY s.last_active DESC
        LIMIT :limit
    """
    async with SessionLocal() as s:
        rows = (await s.execute(text(sql), {"limit": limit})).mappings().all()
    return [
        SessionRow(
            id=str(r["id"]),
            created_at=_iso(r["created_at"]),
            last_active=_iso(r["last_active"]),
            vertical=r["vertical"],
            ip=_ip(r["ip"]),
            referrer_tag=r["referrer_tag"],
            country=r["country"],
            events=r["events"],
            messages=r["messages"],
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def session_detail(session_id: UUID) -> SessionDetail:
    async with SessionLocal() as s:
        visit = (
            await s.execute(
                text(
                    "SELECT id, created_at, last_active, expires_at, vertical, "
                    "       ip, referrer_tag, country "
                    "FROM sessions WHERE id = :sid"
                ),
                {"sid": session_id},
            )
        ).mappings().one_or_none()
        if visit is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "session_not_found", "message": "session not found"},
            )
        rows = (
            await s.execute(
                text(
                    "SELECT ts, type, payload "
                    "FROM events "
                    "WHERE session_id = :sid "
                    "  AND type IN ('question_asked', 'answer_streamed', 'error') "
                    "ORDER BY ts ASC, id ASC"
                ),
                {"sid": session_id},
            )
        ).mappings().all()

    turns: list[TranscriptTurn] = []
    for r in rows:
        raw = r["payload"]
        p = json.loads(raw) if isinstance(raw, str) else (raw or {})
        ts = _iso(r["ts"])
        if r["type"] == "question_asked":
            turns.append(TranscriptTurn(ts=ts, role="user", text=p.get("text", "")))
        elif r["type"] == "answer_streamed":
            turns.append(TranscriptTurn(ts=ts, role="assistant", text=p.get("text", "")))
        elif r["type"] == "error":
            turns.append(
                TranscriptTurn(
                    ts=ts, role="error", text=p.get("message", ""), stage=p.get("stage")
                )
            )

    return SessionDetail(
        id=str(visit["id"]),
        created_at=_iso(visit["created_at"]),
        last_active=_iso(visit["last_active"]),
        expires_at=_iso(visit["expires_at"]),
        vertical=visit["vertical"],
        ip=_ip(visit["ip"]),
        referrer_tag=visit["referrer_tag"],
        country=visit["country"],
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
