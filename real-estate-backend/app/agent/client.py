"""Reply generation via the Claude Agent SDK (mirrors ask_clinic's client).

Auth: the SDK uses the machine's Claude Code login (local dev) or
CLAUDE_CODE_OAUTH_TOKEN (headless/server). No API key needed.

Built-in Claude Code tools (Bash/Read/Write/...) are hard-disabled: ``tools=[]``
removes the built-in base set, ``allowed_tools`` auto-allows ONLY our lease MCP
tools, and ``permission_mode="dontAsk"`` denies anything else without prompting
(so this headless backend never hangs on an approval request).
"""

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .prompts import SYSTEM
from .registry import SERVER_KEY, allowed_tool_names, lease_server

# In-process MCP server, built once from the manifest.
_lease_server = lease_server()


async def generate_reply(
    user_text: str, resume: str | None = None
) -> tuple[str, str | None]:
    """Return (reply_text, session_id) for a single visitor message.

    Pass the ``session_id`` from a previous turn back as ``resume`` to continue
    that conversation — the SDK loads its history, giving multi-turn context.
    On the first turn pass ``resume=None``; a fresh session_id is returned.
    """
    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools (base set)
        mcp_servers={SERVER_KEY: _lease_server},
        allowed_tools=allowed_tool_names(),  # only our lease tools are usable
        permission_mode="dontAsk",  # deny non-approved tools; never prompt/hang
        setting_sources=[],  # SDK isolation: ignore filesystem settings/CLAUDE.md
        system_prompt=SYSTEM,
        resume=resume,  # None → new session; else continue the given session
    )
    parts: list[str] = []
    session_id: str | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_text)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
    return "".join(parts).strip(), session_id
