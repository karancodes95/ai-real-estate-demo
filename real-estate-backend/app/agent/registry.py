"""Agent-tool manifest — the single place that lists the bot's tools, builds the
in-process MCP server, and derives the allow-list. Mirrors ask_clinic's pattern.
"""

from collections import namedtuple

from claude_agent_sdk import create_sdk_mcp_server

from .tools import save_lead

# MCP server key. Fully-qualified tool names are "mcp__{SERVER_KEY}__{tool_name}".
SERVER_KEY = "lease"

AgentTool = namedtuple("AgentTool", ["name", "tool"])

MANIFEST: list[AgentTool] = [
    AgentTool("save_lead", save_lead),
]


def lease_server():
    """The in-process MCP server exposing every manifest tool."""
    return create_sdk_mcp_server(
        name=SERVER_KEY, version="1.0.0", tools=[t.tool for t in MANIFEST]
    )


def allowed_tool_names() -> list[str]:
    """Fully-qualified tool names for ClaudeAgentOptions.allowed_tools."""
    return [f"mcp__{SERVER_KEY}__{t.name}" for t in MANIFEST]
