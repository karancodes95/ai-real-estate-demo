"""In-process agent tools — the bot's only way to touch lead data.

One tool for v1: ``save_lead``. It receives the fields the LLM extracted plus
its grade/rationale, computes the deterministic rule score, persists the lead,
and fires the day-0 confirmation email (soft-fail, logged to email_log).
"""

import asyncio
import logging
from typing import Annotated, Any

from claude_agent_sdk import tool

from ..config import settings
from ..db import SessionLocal
from ..emailer import send_confirmation_sync
from ..models import EmailLog, Lead
from ..scoring import compute_score

logger = logging.getLogger("uvicorn.error")


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def _summary(lead: Lead) -> str:
    parts = [f"- Name: {lead.name}"]
    if lead.budget:
        parts.append(f"- Budget: {lead.budget}")
    if lead.location:
        parts.append(f"- Area: {lead.location}")
    if lead.property_type:
        parts.append(f"- Property type: {lead.property_type}")
    if lead.timeline_months:
        parts.append(f"- Timeline: within {lead.timeline_months} months")
    if lead.financing != "unknown":
        parts.append(f"- Financing: {lead.financing.replace('_', ' ')}")
    if lead.intent != "unknown":
        parts.append(f"- Looking to: {lead.intent}")
    return "\n".join(parts)


@tool(
    "save_lead",
    "Save the qualified enquiry once name + email are known. Call exactly once.",
    {
        "name": Annotated[str, "Lead's name"],
        "email": Annotated[str, "Lead's email address"],
        "phone": Annotated[str, "Phone if given, else empty string"],
        "budget": Annotated[str, "Budget as stated, e.g. '80L', else empty"],
        "location": Annotated[str, "Preferred area/city, else empty"],
        "property_type": Annotated[str, "flat/villa/plot/office/..., else empty"],
        "timeline_months": Annotated[int, "Months until they want to move/buy; 0 if unknown"],
        "financing": Annotated[str, "pre_approved | cash | needs_loan | unknown"],
        "intent": Annotated[str, "buy | invest | rent | browsing | unknown"],
        "grade": Annotated[str, "Your grade: hot | warm | cold"],
        "rationale": Annotated[str, "One line on why you graded it that way"],
        "session_id": Annotated[str, "Chat session id if known, else empty"],
    },
)
async def save_lead(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    email = str(args.get("email", "")).strip()
    if not name or "@" not in email:
        return _text("I need a name and a valid email before saving.", is_error=True)

    financing = str(args.get("financing", "unknown"))
    intent = str(args.get("intent", "unknown"))
    try:
        timeline_months = int(args.get("timeline_months", 0))
    except (TypeError, ValueError):
        timeline_months = 0
    budget = str(args.get("budget", "")).strip()

    lead = Lead(
        name=name,
        email=email,
        phone=str(args.get("phone", "")).strip(),
        budget=budget,
        location=str(args.get("location", "")).strip(),
        property_type=str(args.get("property_type", "")).strip(),
        timeline_months=timeline_months,
        financing=financing,
        intent=intent,
        score=compute_score(budget, timeline_months, financing, intent),
        grade=str(args.get("grade", "")).strip(),
        rationale=str(args.get("rationale", "")).strip()[:300],
        session_id=str(args.get("session_id", "")).strip()[:100],
    )
    async with SessionLocal() as s:
        s.add(lead)
        await s.commit()
        await s.refresh(lead)

    # Day-0 confirmation — soft-fail; the chat reply never depends on it.
    subject = f"We received your enquiry — {settings.agency_name}"
    try:
        await asyncio.to_thread(send_confirmation_sync, email, name, _summary(lead))
        email_status, detail = "sent", ""
    except Exception as exc:  # noqa: BLE001 — any send failure is soft
        email_status, detail = "failed", str(exc)[:300]
        if "not configured" in str(exc):
            email_status = "skipped"
        logger.warning("day-0 email %s: %s", email_status, detail)
    async with SessionLocal() as s:
        s.add(
            EmailLog(
                lead_id=lead.id,
                to_email=email,
                subject=subject,
                status=email_status,
                detail=detail,
            )
        )
        await s.commit()

    sent_note = (
        "Confirmation email sent."
        if email_status == "sent"
        else "Confirmation email could not be sent."
    )
    return _text(
        f"Lead #{lead.id} saved (score {lead.score}/100, grade {lead.grade}). {sent_note}"
    )
