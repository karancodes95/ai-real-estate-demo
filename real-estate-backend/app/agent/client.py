"""Reply generation via the Claude Agent SDK (mirrors ask_clinic's client).

Auth: the SDK uses the machine's Claude Code login (local dev) or
CLAUDE_CODE_OAUTH_TOKEN (headless/server). No API key needed.

Built-in Claude Code tools (Bash/Read/Write/...) are hard-disabled: ``tools=[]``
removes the built-in base set, ``allowed_tools`` auto-allows ONLY our lease MCP
tools, and ``permission_mode="dontAsk"`` denies anything else without prompting
(so this headless backend never hangs on an approval request).

Two ids per turn, both server-side:
- ``session_id``: the visitor's server-issued session (sessions.id, from
  POST /api/session). It is set as the turn context (tools.turn_session_id)
  for save_lead, which stores it on the lead, so the CRM shows the lead to
  that visitor.
- ``resume``: the agent's own SDK session from an earlier turn of that visit
  (sessions.agent_session_id), never a client value. A new conversation's SDK
  id is picked here (a uuid4, passed as the SDK's ``session_id`` option).
"""

import logging
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .prompts import SYSTEM
from .registry import SERVER_KEY, allowed_tool_names, lease_server
from .tools import turn_session_id

logger = logging.getLogger("uvicorn.error")

# In-process MCP server, built once from the manifest.
_lease_server = lease_server()


async def generate_reply(
    user_text: str, session_id: str, resume: str | None = None
) -> tuple[str, str | None]:
    """Return (reply_text, agent_session_id) for a single visitor message.

    ``session_id`` is the visitor's session, stored on any lead saved this
    turn. Pass the ``agent_session_id`` of the visit's previous turn back as
    ``resume`` to continue that conversation: the SDK loads its history,
    giving multi-turn context. On the first turn pass ``resume=None``; a fresh
    agent_session_id is returned.
    """
    # This turn's SDK session: the resumed one, or a new id the server picks.
    turn_id = resume or str(uuid4())
    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools (base set)
        mcp_servers={SERVER_KEY: _lease_server},
        allowed_tools=allowed_tool_names(),  # only our lease tools are usable
        permission_mode="dontAsk",  # deny non-approved tools; never prompt/hang
        setting_sources=[],  # SDK isolation: ignore filesystem settings/CLAUDE.md
        system_prompt=SYSTEM,
        resume=resume,  # None → new session; else continue the given session
        # A new session runs under the server's id (the SDK allows this
        # option only without resume); a resumed one keeps its own id.
        session_id=None if resume else turn_id,
    )
    parts: list[str] = []
    agent_session_id: str | None = None
    # Set before the client connects: the SDK's tool tasks copy this context.
    token = turn_session_id.set(session_id)
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    agent_session_id = message.session_id
    finally:
        turn_session_id.reset(token)
    if agent_session_id and agent_session_id != turn_id:
        # Should not happen; the next turn resumes the id the SDK reported.
        logger.warning("SDK session id differs from the turn's id")
    return "".join(parts).strip(), agent_session_id
