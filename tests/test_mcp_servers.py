"""The MCP servers expose the role's tool surface, not the engine's."""

from __future__ import annotations


async def test_the_tool_surface_is_the_role_surface_not_the_engines(tmp_path):
    from mcp_servers.shopping import build_shopping_server

    server = build_shopping_server(str(tmp_path / "store.db"))
    names = {tool.name for tool in await server.list_tools()}
    assert "search_products" in names
    assert len(names) < 40, "the role surface is ~20 tools, not the engine's 900"


async def test_the_merchant_server_exposes_apply_change(tmp_path):
    from mcp_servers.merchant import build_merchant_server

    server = build_merchant_server(str(tmp_path / "store.db"))
    names = {tool.name for tool in await server.list_tools()}
    assert "apply_change" in names


def test_servers_build_without_a_model_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from mcp_servers.merchant import build_merchant_server
    from mcp_servers.shopping import build_shopping_server

    build_shopping_server(str(tmp_path / "a.db"))
    build_merchant_server(str(tmp_path / "b.db"))
