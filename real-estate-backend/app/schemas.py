"""HTTP request/response contracts (validated at the boundary)."""

from pydantic import BaseModel, Field

# The chat message cap, also sent to the client by POST /api/session.
MAX_INPUT_CHARS = 2000


class SessionResponse(BaseModel):
    # The clinic demo's shape. The caps are UX hints for the client.
    session_id: str
    vertical: str
    max_input_chars: int
    # The Agent SDK runs with no output-token cap here, so this is 0 (none).
    max_output_tokens: int


class ChatRequest(BaseModel):
    # The session is the X-Session-Id header (a server-issued id), not a body
    # field: a "session_id" sent in the body is ignored.
    message: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)


class ChatResponse(BaseModel):
    reply: str
    session_id: str | None = None  # the caller's X-Session-Id, echoed


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
