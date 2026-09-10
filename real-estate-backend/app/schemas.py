"""HTTP request/response contracts (validated at the boundary)."""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # Echoed back from a prior reply to continue that conversation (multi-turn).
    # Client-controlled and passed to the SDK's resume; bound its length.
    session_id: str | None = Field(default=None, max_length=100)


class ChatResponse(BaseModel):
    reply: str
    session_id: str | None = None


# CRM-lite pipeline stages, in board order.
STAGES = ["new", "contacted", "visit", "closed_won", "closed_lost"]


class LeadOut(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    budget: str
    location: str
    property_type: str
    timeline_months: int
    financing: str
    intent: str
    score: int
    grade: str
    rationale: str
    status: str
    created_at: str


class StageUpdate(BaseModel):
    status: str = Field(pattern="^(new|contacted|visit|closed_won|closed_lost)$")
