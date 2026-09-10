"""FastAPI app: serves the visitor site and the chat endpoint.

The bundled static frontend (static/lead.html, static/crm.html) is served from
this same app for local dev, so those pages call the API same-origin. The
production frontends are static copies hosted on GitHub Pages that call this
API cross-origin, so CORS is enabled and the JSON API routes are gated behind
an X-API-Key header (see app/deps.py). The HTML routes stay ungated.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fastapi import HTTPException
from sqlalchemy import select

from .agent.client import generate_reply
from .bootstrap import init_models
from .chatlog import log_turn
from .config import settings
from .db import SessionLocal
from .deps import require_api_key
from .models import Lead
from .ratelimit import check_and_count
from .schemas import ChatRequest, ChatResponse, LeadOut, StageUpdate

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    yield


app = FastAPI(title="Amberstone Realty — Enquiry Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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


# ---- CRM-lite (business-facing, demo: no auth) ----


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


@app.get("/api/crm/leads", response_model=list[LeadOut], dependencies=[Depends(require_api_key)])
async def crm_leads() -> list[LeadOut]:
    async with SessionLocal() as s:
        rows = (
            (await s.execute(select(Lead).order_by(Lead.score.desc(), Lead.id.desc())))
            .scalars()
            .all()
        )
    return [_lead_out(lead) for lead in rows]


@app.patch(
    "/api/crm/leads/{lead_id}/stage",
    response_model=LeadOut,
    dependencies=[Depends(require_api_key)],
)
async def crm_move_stage(lead_id: int, body: StageUpdate) -> LeadOut:
    async with SessionLocal() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="lead not found")
        lead.status = body.status
        await s.commit()
        await s.refresh(lead)
    return _lead_out(lead)


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Trust X-Forwarded-For only behind a real proxy
    (config.trust_proxy); otherwise a client could spoof it to dodge the cap."""
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Rightmost entry = the IP our own proxy observed and appended;
            # leftmost entries are client-supplied and spoofable.
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
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
            session_id=req.session_id,
        )
    try:
        reply, session_id = await generate_reply(req.message, resume=req.session_id)
    except Exception:
        logger.exception("chat generation failed")
        reply, session_id = ("Sorry, I'm having trouble right now. Please try again.", req.session_id)
    reply = reply or "Sorry, I didn't catch that. Try again?"

    # Persist the turn (best-effort — never fail the reply over a logging error).
    try:
        await log_turn(session_id, client_ip, req.message, reply)
    except Exception:
        logger.warning("chat logging failed", exc_info=True)

    return ChatResponse(reply=reply, session_id=session_id)
