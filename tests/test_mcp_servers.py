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


async def _stage_a_price_change(store, kernel) -> str:
    """Stages a change directly on the backend (bypassing the MCP tool surface's own
    provenance gates, which are exercised elsewhere) and returns its change_id."""
    from merchant_agent.types import MerchantSessionContext, PriceUpdateItem

    from engine_backend.merchant import EngineMerchant

    staging_backend = EngineMerchant(store, kernel)
    change = await staging_backend.stage_price_update(
        MerchantSessionContext(session_id="s", merchant_id="acme", operator="user:acme-operator"),
        [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)],
    )
    return change.change_id


async def test_apply_change_refuses_without_a_prior_host_approve(store, kernel):
    from mcp.shared.memory import create_connected_server_and_client_session

    from mcp_servers.merchant import build_merchant_server

    change_id = await _stage_a_price_change(store, kernel)
    async with create_connected_server_and_client_session(
        build_merchant_server(store.db_path)
    ) as client:
        await client.call_tool("get_pending_changes", {})
        result = await client.call_tool("apply_change", {"change_id": change_id})
        assert result.isError
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price != 199.00


async def test_apply_change_succeeds_after_host_approve(store, kernel):
    from mcp.shared.memory import create_connected_server_and_client_session

    from mcp_servers.merchant import build_merchant_server

    change_id = await _stage_a_price_change(store, kernel)
    async with create_connected_server_and_client_session(
        build_merchant_server(store.db_path)
    ) as client:
        await client.call_tool("get_pending_changes", {})
        approved = await client.call_tool("host_approve", {"change_id": change_id})
        assert not approved.isError
        applied = await client.call_tool("apply_change", {"change_id": change_id})
        assert applied.isError

    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price != 199.00
