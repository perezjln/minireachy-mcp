"""MCP tools grouped by functional domain."""

from mcp.server.mcpserver import MCPServer

from . import apps, daemon, media, motion, perception, state
from ._common import Deps, ToolSpec

__all__ = ["Deps", "ToolSpec", "register_all"]


def register_all(server: MCPServer, deps: Deps) -> None:
    for module in (daemon, state, motion, media, apps, perception):
        module.register_tools(server, deps)
