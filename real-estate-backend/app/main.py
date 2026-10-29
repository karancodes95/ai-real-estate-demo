"""FastAPI app: serves the visitor site and the chat endpoint.

The bundled static frontend (static/lead.html, static/crm.html) is served from
this same app for local dev, so those pages call the API same-origin. The
production frontends are static copies hosted on GitHub Pages that call this
API cross-origin, so CORS is enabled and the JSON API routes are gated behind
an X-API-Key header (see app/deps.py). The HTML routes stay ungated. The
/admin routes (app/admin.py) need the admin JWT from POST /admin/login
(app/admin_auth.py) instead, never the API key.

The API key is public (it ships in the chat page), so it is only a bot gate.
The chat page opens a visit with POST /api/session (the clinic demo's route)
and sends the server-issued id as X-Session-Id. /api/chat accepts only an
unexpired one, and the agent resumes only the SDK session stored on it, so a
client can no longer choose which chat it continues. The CRM routes return and
change only the leads of the caller's own session (X-Session-Id); listing
every lead is /admin/leads, behind the JWT.
"""

import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fastapi import HTTPException
from sqlalchemy import select

from . import admin, admin_auth, events, sessions
from .agent.client import generate_reply
from .bootstrap import init_models
from .chatlog import log_turn
from .config import settings
from .db import SessionLocal
from .deps import client_ip as _client_ip
from .deps import proxy_country, require_api_key
from .models import Lead
from .ratelimit import check_and_count
from .schemas import (
    MAX_INPUT_CHARS,
    ChatRequest,
    ChatResponse,
    LeadOut,
    SessionResponse,
    StageUpdate,
)

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("uvicorn.error")

# Canonical lowercase UUID, the form str(UUID) produces for a session id.
UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The one place a turn can fail (around generate_reply), the error event's stage.
ERROR_STAGE = "generate_reply"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    yield


app = FastAPI(title="Amberstone Realty — Enquiry Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Admin API (bearer JWT from POST /admin/login), read by bytcra-admin.
app.include_router(admin_auth.router)
app.include_router(admin.router)

# Explicit CORS allowlist when set; "*" only when unset (dev). Production .env
# should set ALLOWED_ORIGINS to the deployed frontend origin(s) (e.g. the
# GitHub Pages URL). allow_credentials stays False: the gate is the X-API-Key
# header, not cookies, so we never need to reflect credentials.
_origins = (
    [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    if settings.allowed_origins
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Defense-in-depth response headers. The CSP keeps scripts same-origin (the chat
# JS is served from /static), while allowing Google Fonts. Inline styles stay
# allowed (style blocks/attributes are low-risk).
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "lead.html")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- CRM-lite (demo: each visitor sees only their own chat's leads) ----


@app.get("/crm", include_in_schema=False)
async def crm_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "crm.html")


def _lead_out(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        budget=lead.budget,
        location=lead.location,
        property_type=lead.property_type,
        timeline_months=lead.timeline_months,
        financing=lead.financing,
        intent=lead.intent,
        score=lead.score,
        grade=lead.grade,
        rationale=lead.rationale,
        status=lead.status,
        created_at=lead.created_at.strftime("%d %b, %I:%M %p"),
    )


def visitor_session(x_session_id: Annotated[str | None, Header()] = None) -> str | None:
    """The caller's own session id, from the X-Session-Id header (crm.js sends
    the id the chat page stored). Only a canonical lowercase UUID counts, the
    shape of the ids POST /api/session issues; anything else is no session,
    which sees no leads. leads.session_id is set server-side by save_lead."""
    if x_session_id and re.fullmatch(UUID_RE, x_session_id):
        return x_session_id
    return None


VisitorSession = Annotated[str | None, Depends(visitor_session)]


@app.get("/api/crm/leads", response_model=list[LeadOut], dependencies=[Depends(require_api_key)])
async def crm_leads(session_id: VisitorSession) -> list[LeadOut]:
    """The caller's own leads only. No session (or a non-UUID one) gets []."""
    if session_id is None:
        return []
    async with SessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(Lead)
                    .where(Lead.session_id == session_id)
                    .order_by(Lead.score.desc(), Lead.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [_lead_out(lead) for lead in rows]


@app.patch(
    "/api/crm/leads/{lead_id}/stage",
    response_model=LeadOut,
    dependencies=[Depends(require_api_key)],
)
async def crm_move_stage(lead_id: int, body: StageUpdate, session_id: VisitorSession) -> LeadOut:
    """Move one of the caller's own leads. Another session's lead, or any lead
    without a session, is a 404 like a missing one, so ids can't be probed."""
    async with SessionLocal() as s:
        lead = await s.get(Lead, lead_id) if session_id else None
        if lead is None or lead.session_id != session_id:
            raise HTTPException(status_code=404, detail="lead not found")
        lead.status = body.status
        await s.commit()
        await s.refresh(lead)
    return _lead_out(lead)


# ---- visit session (the clinic demo's POST /session) ----


@app.post(
    "/api/session", response_model=SessionResponse, dependencies=[Depends(require_api_key)]
)
async def create_session(
    request: Request,
    # Bound free-form metadata so an attacker can't store unbounded strings.
    vertical: str | None = Query(default=None, max_length=32),
    referrer_tag: str | None = Query(default=None, max_length=100),
    country: str | None = Query(default=None, max_length=8),
) -> SessionResponse:
    v = vertical or sessions.VERTICAL
    if v != sessions.VERTICAL:
        raise HTTPException(status_code=400, detail=f"unknown vertical: {v}")
    ip = sessions.inet_or_none(_client_ip(request))
    # Reuse an existing non-expired session for this (ip, vertical) so the chat
    # survives across tabs / devices on the same network, like the clinic demo.
    # Privacy trade-off: anyone on the same IP shares the chat and its CRM
    # leads (see SECURITY.md).
    session_id = await sessions.get_active_for_ip(ip=ip, vertical=v)
    if session_id is not None:
        await sessions.touch_session(session_id)
    else:
        session_id = await sessions.create_session(
            vertical=v,
            referrer_tag=referrer_tag,
            # The client's value, else the edge proxy's geolocation (deps.py).
            country=country or proxy_country(request),
            ip=ip,
        )
    return SessionResponse(
        session_id=str(session_id),
        vertical=v,
        max_input_chars=MAX_INPUT_CHARS,
        max_output_tokens=0,
    )


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(
    req: ChatRequest, request: Request, x_session_id: Annotated[UUID, Header()]
) -> ChatResponse:
    # Only a server-issued, unexpired session chats: a made-up or expired id is
    # a 404 (the client opens a new session), before the rate limit counts it.
    visit = await sessions.get_live(x_session_id)
    if visit is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found", "message": "session missing or expired"},
        )
    session_id = str(x_session_id)
    client_ip = _client_ip(request)
    try:
        allowed = await check_and_count(client_ip)
    except Exception:
        # Fail open: a transient usage-table error shouldn't take down the demo
        # (the chat tools share this DB and would surface errors anyway).
        logger.warning("rate-limit check failed; allowing request", exc_info=True)
        allowed = True
    if not allowed:
        return ChatResponse(
            reply=(
                "You've reached today's message limit for this demo. To know more, "
                f"write to {settings.agency_email}."
            ),
            session_id=session_id,
        )
    await sessions.touch_session(x_session_id)
    await events.log(x_session_id, "question_asked", {"text": req.message})

    t0 = time.perf_counter()
    error: str | None = None
    try:
        # The agent resumes only the SDK session stored on this visit.
        reply, agent_session_id = await generate_reply(
            req.message, session_id=session_id, resume=visit.agent_session_id
        )
    except Exception as exc:
        logger.exception("chat generation failed")
        # Only the class name is stored (the error event and row), never the
        # message, which could carry internals.
        error = type(exc).__name__
        reply, agent_session_id = "Sorry, I'm having trouble right now. Please try again.", None
        await events.log(x_session_id, "error", {"stage": ERROR_STAGE, "message": error})
    reply = reply or "Sorry, I didn't catch that. Try again?"
    ms = int((time.perf_counter() - t0) * 1000)

    # The next turn resumes the SDK session this one ran in. After a failure it
    # starts a new one: a resume that fails (e.g. the SDK lost the session)
    # would otherwise fail every turn until the visit expires. Both writes are
    # compare-and-set against the id this turn started from, so a parallel
    # turn's newer id (another tab of this visit) is never overwritten.
    next_agent_id = (agent_session_id or visit.agent_session_id) if error is None else None
    if next_agent_id != visit.agent_session_id:
        try:
            await sessions.set_agent_session(
                x_session_id, next_agent_id, expected=visit.agent_session_id
            )
        except Exception:
            # The reply stands; the next turn just starts a new agent chat.
            logger.warning("storing the agent session failed", exc_info=True)

    if error is None:
        await events.log(
            x_session_id, "answer_streamed", {"text": reply, "chars": len(reply), "ms": ms}
        )
        await events.log(x_session_id, "turn_complete", {"total_ms": ms})

    # Persist the turn (best-effort — never fail the reply over a logging error).
    try:
        await log_turn(session_id, client_ip, req.message, reply, error=error)
    except Exception:
        logger.warning("chat logging failed", exc_info=True)

    return ChatResponse(reply=reply, session_id=session_id)
