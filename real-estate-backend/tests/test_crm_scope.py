"""The CRM routes show and change only the caller's own session's leads, and
save_lead stores the visitor's server-issued session id (never the agent's
SDK id, one from the LLM or the visitor's JSON). The public chat API key alone
opens no lead.

No test reaches the Claude Agent SDK: the generate_reply tests swap
ClaudeSDKClient for a fake that runs the tool the way the SDK does (a reader
task spawned on connect, which spawns one task per tool call, both through the
SDK's own spawn_detached), so the turn context must cross those task hops.
"""

import re
from uuid import uuid4

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk._internal._task_compat import spawn_detached
from sqlalchemy import select

from app.agent import client as agent_client
from app.agent.tools import save_lead, turn_session_id
from app.db import SessionLocal
from app.models import DemoSession, Lead
from conftest import add_lead

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Hex letters in both, so their upper-case forms differ.
A = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
B = "b7e2f9c1-3d4a-4e5f-9a6b-7c8d9e0f1a2b"
LEAD_ARGS = {
    "name": "Meera", "email": "meera@example.test", "phone": "", "budget": "90L",
    "location": "Baner", "property_type": "flat", "timeline_months": 3,
    "financing": "pre_approved", "intent": "buy", "grade": "hot",
    "rationale": "Ready to buy.",
}


def _h(api_key: str, session_id: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": api_key}
    if session_id is not None:
        headers["X-Session-Id"] = session_id
    return headers


async def _names(client, headers) -> list[str]:
    r = await client.get("/api/crm/leads", headers=headers)
    assert r.status_code == 200
    return [row["name"] for row in r.json()]


async def _lead_ids() -> dict[str, int]:
    async with SessionLocal() as s:
        return {lead.name: lead.id for lead in (await s.execute(select(Lead))).scalars()}


async def _stored() -> list[tuple[str, str, str]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(Lead).order_by(Lead.id))).scalars().all()
    return [(lead.name, lead.session_id, lead.status) for lead in rows]


# ---- reads ----


async def test_each_session_sees_only_its_own_leads(client, api_key):
    await add_lead("Asha", 70, session_id=A)
    await add_lead("Anil", 40, session_id=A)
    await add_lead("Ben", 90, session_id=B)
    await add_lead("NoSession", 99)  # session_id ""
    assert await _names(client, _h(api_key, A)) == ["Asha", "Anil"]
    assert await _names(client, _h(api_key, B)) == ["Ben"]
    assert await _names(client, _h(api_key, str(uuid4()))) == []


async def test_the_chat_key_alone_lists_no_leads(client, api_key):
    await add_lead("Asha", 70, session_id=A)
    await add_lead("Ben", 90, session_id=B)
    await add_lead("NoSession", 99)
    assert await _names(client, _h(api_key)) == []


@pytest.mark.parametrize(
    "bad",
    ["", "x", A.upper(), "{" + A + "}", A[:-1], A + "0", A.replace("-", ""),
     "' OR '1'='1", "%", "*"],
    ids=["empty", "short", "upper", "braces", "short-uuid", "long-uuid", "no-dashes",
         "sql", "like-wildcard", "star"],
)
async def test_a_non_uuid_session_sees_nothing_even_if_stored(client, api_key, bad):
    # Even a lead stored under that exact value is not listed.
    await add_lead("Stored", 70, session_id=bad)
    await add_lead("Asha", 80, session_id=A)
    assert await _names(client, _h(api_key, bad)) == []


async def test_the_api_key_is_still_required(client):
    await add_lead("Asha", 70, session_id=A)
    assert (await client.get("/api/crm/leads", headers={"X-Session-Id": A})).status_code == 401


async def test_cross_session_leads_are_only_on_the_admin_jwt(client, api_key, auth):
    await add_lead("Asha", 70, session_id=A)
    await add_lead("Ben", 90, session_id=B)
    r = await client.get("/admin/leads", headers=auth)
    assert [row["name"] for row in r.json()] == ["Ben", "Asha"]
    assert (await client.get("/admin/leads", headers=_h(api_key, A))).status_code == 401


# ---- stage changes ----


async def _move(client, headers, lead_id, status="contacted"):
    return await client.patch(
        f"/api/crm/leads/{lead_id}/stage", json={"status": status}, headers=headers
    )


async def test_a_session_moves_its_own_lead(client, api_key):
    await add_lead("Asha", 70, session_id=A)
    ids = await _lead_ids()
    r = await _move(client, _h(api_key, A), ids["Asha"])
    assert r.status_code == 200 and r.json()["status"] == "contacted"
    assert await _stored() == [("Asha", A, "contacted")]


async def test_a_session_cannot_move_another_sessions_lead(client, api_key):
    await add_lead("Asha", 70, session_id=A)
    await add_lead("Ben", 90, session_id=B)
    ids = await _lead_ids()
    r = await _move(client, _h(api_key, A), ids["Ben"])
    # Same answer as a lead that does not exist, so ids can't be probed.
    assert r.status_code == 404
    missing = await _move(client, _h(api_key, A), 999_999)
    assert missing.status_code == 404 and missing.json() == r.json()
    assert await _stored() == [("Asha", A, "new"), ("Ben", B, "new")]


@pytest.mark.parametrize("session", [None, "", "x", A.upper()])
async def test_no_session_moves_nothing(client, api_key, session):
    await add_lead("Asha", 70, session_id=A)
    await add_lead("NoSession", 90)
    ids = await _lead_ids()
    for lead_id in ids.values():
        assert (await _move(client, _h(api_key, session), lead_id)).status_code == 404
    assert await _stored() == [("Asha", A, "new"), ("NoSession", "", "new")]


async def test_a_session_cannot_move_a_lead_without_a_session(client, api_key):
    await add_lead("NoSession", 90)
    ids = await _lead_ids()
    assert (await _move(client, _h(api_key, A), ids["NoSession"])).status_code == 404


# ---- the lead tool stores the server-side session id ----


def test_the_tool_schema_has_no_session_id():
    assert "session_id" not in save_lead.input_schema


async def test_save_lead_stores_the_turn_session_not_its_args():
    token = turn_session_id.set(A)
    try:
        result = await save_lead.handler({**LEAD_ARGS, "session_id": B})
    finally:
        turn_session_id.reset(token)
    assert not result.get("is_error")
    assert await _stored() == [("Meera", A, "new")]


async def test_save_lead_outside_a_turn_stores_no_session(client, api_key):
    await save_lead.handler({**LEAD_ARGS, "session_id": B})
    assert await _stored() == [("Meera", "", "new")]
    assert await _names(client, _h(api_key, B)) == []


class FakeSDKClient:
    """Runs save_lead the way ClaudeSDKClient does: connecting spawns a reader
    task (Query.start), and the reader spawns one task per tool call
    (Query._spawn_control_request_handler), both via the SDK's spawn_detached.
    The tool is called with a "session_id" argument, as an LLM might send."""

    seen: list = []

    def __init__(self, options):
        self.options = options
        FakeSDKClient.seen.append(options)

    async def __aenter__(self):
        async def tool_call():
            await save_lead.handler({**LEAD_ARGS, "session_id": B})

        async def reader():
            await spawn_detached(tool_call()).wait()

        self._reader = spawn_detached(reader())
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text):
        pass

    async def receive_response(self):
        await self._reader.wait()
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=self.options.session_id or self.options.resume,
        )


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSDKClient.seen = []
    monkeypatch.setattr(agent_client, "ClaudeSDKClient", FakeSDKClient)
    return FakeSDKClient


async def test_a_new_turn_runs_under_an_agent_id_the_server_picks(fake_sdk):
    _, agent_id = await agent_client.generate_reply("I want a flat in Baner", session_id=A)
    (options,) = fake_sdk.seen
    assert options.resume is None
    assert UUID.fullmatch(options.session_id) and agent_id == options.session_id
    assert agent_id != A
    # The tool, two task hops away, stored the visitor's session, not the
    # agent's id or the LLM's.
    assert await _stored() == [("Meera", A, "new")]
    assert turn_session_id.get() == ""


async def test_a_resumed_turn_keeps_its_agent_id(fake_sdk):
    _, agent_id = await agent_client.generate_reply("my email is ...", session_id=A,
                                                    resume="agent-7")
    (options,) = fake_sdk.seen
    # The SDK takes session_id only without resume.
    assert options.resume == "agent-7" and options.session_id is None
    assert agent_id == "agent-7"
    assert await _stored() == [("Meera", A, "new")]


async def test_chat_then_crm_shows_the_visitor_their_lead_only(client, api_key, fake_sdk):
    await add_lead("Ben", 90, session_id=B)
    sid = (await client.post("/api/session", headers=_h(api_key))).json()["session_id"]
    r = await client.post("/api/chat", json={"message": "hi"}, headers=_h(api_key, sid))
    assert r.json()["session_id"] == sid and sid != B
    # The lead is under the server-issued session the CRM page sends.
    assert await _names(client, _h(api_key, sid)) == ["Meera"]
    assert await _names(client, _h(api_key, B)) == ["Ben"]
    # The agent's own id is stored server-side only and opens no lead.
    async with SessionLocal() as s:
        agent_id = (await s.get(DemoSession, sid)).agent_session_id
    assert UUID.fullmatch(agent_id) and agent_id != sid
    assert await _names(client, _h(api_key, agent_id)) == []
