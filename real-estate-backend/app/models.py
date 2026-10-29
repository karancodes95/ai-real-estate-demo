"""Schema for the lead-to-lease demo (Amberstone Realty)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class DemoSession(Base):
    """One row per demo visit: the clinic demo's sessions table (same columns,
    types and indexes) plus agent_session_id. The id is issued by
    POST /api/session; /api/chat accepts only an unexpired one."""

    __tablename__ = "sessions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    vertical: Mapped[str] = mapped_column(Text, nullable=False)
    referrer_tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    # The Claude Agent SDK session this visit's chat resumes. Server-side only:
    # set after the visit's first good turn, never sent to or read from a client.
    agent_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        Index("sessions_expires_at_idx", "expires_at"),
        Index("sessions_ip_idx", "ip"),
    )


class Event(Base):
    """Every session created, question asked, answer, error and finished turn
    (the clinic demo's events table). The admin API is computed from these."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    __table_args__ = (
        Index("events_session_id_ts_idx", "session_id", "ts"),
        Index("events_session_type_ts_idx", "session_id", "type", "ts"),
    )


class Lead(Base):
    """One qualified lead captured by the intake chat."""

    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str] = mapped_column(String(40), default="")

    # Qualification fields (as stated by the lead, normalized by the agent).
    budget: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(120), default="")
    property_type: Mapped[str] = mapped_column(String(80), default="")
    timeline_months: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unknown
    financing: Mapped[str] = mapped_column(String(40), default="unknown")
    intent: Mapped[str] = mapped_column(String(40), default="unknown")

    # Scoring: deterministic rule score + LLM grade with one-line rationale.
    score: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str] = mapped_column(String(20), default="")
    rationale: Mapped[str] = mapped_column(String(300), default="")

    status: Mapped[str] = mapped_column(String(30), default="new")
    session_id: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EmailLog(Base):
    """Every outbound email attempt (day-0 confirmation), sent or failed."""

    __tablename__ = "email_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(Integer)
    to_email: Mapped[str] = mapped_column(String(180))
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))  # sent | failed | skipped
    detail: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatMessage(Base):
    """Chat transcript — one row per message (user or assistant)."""

    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    client_ip: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IpUsage(Base):
    """Per-IP daily chat counters for the abuse cap."""

    __tablename__ = "ip_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(60))
    day: Mapped[date] = mapped_column(Date)
    count: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("ip", "day"),)
