"""Schema for the lead-to-lease demo (Amberstone Realty)."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


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
