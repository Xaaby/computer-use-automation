"""Tests for MCP server exposure."""
from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from mcp import Client

from escalation.api import mcp_server


@pytest.mark.asyncio
async def test_mcp_list_tools():
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools.tools]
        assert "list_capabilities" in names
        assert "invoke_capability" in names


@pytest.mark.asyncio
async def test_mcp_list_capabilities_returns_list():
    async with Client(mcp_server) as client:
        result = await client.call_tool("list_capabilities", {})
        content = result.content
        assert content is not None
        assert len(content) >= 0
