"""Deterministic rule-based lead scoring (0-100), explainable per component.

The LLM extracts/normalizes the fields; these rules turn them into the number.
The LLM separately adds a grade (hot/warm/cold) + one-line rationale — the two
are stored side by side for comparison.
"""

# Normalized values the agent is instructed to pass.
FINANCING_POINTS = {
    "pre_approved": 25,
    "cash": 25,
    "needs_loan": 15,
    "unknown": 0,
}
INTENT_POINTS = {
    "buy": 20,
    "invest": 15,
    "rent": 10,
    "browsing": 0,
    "unknown": 0,
}


def timeline_points(months: int) -> int:
    """Sooner = hotter. 0 means the lead never gave a timeline."""
    if months <= 0:
        return 0
    if months <= 3:
        return 30
    if months <= 6:
        return 20
    if months <= 12:
        return 10
    return 5


def compute_score(
    budget: str, timeline_months: int, financing: str, intent: str
) -> int:
    """Sum of: budget stated (25) + timeline (30) + financing (25) + intent (20)."""
    score = 25 if budget.strip() else 0
    score += timeline_points(timeline_months)
    score += FINANCING_POINTS.get(financing, 0)
    score += INTENT_POINTS.get(intent, 0)
    return score
