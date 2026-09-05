"""``McpSettings`` reads each server's identity and bind address once and validates."""

import pytest

from mcp_servers.settings import DEFAULT_DB_PATH, McpSettings


def test_defaults_are_loopback_and_the_seeded_principals():
    shopping = McpSettings.shopping(env={})
    merchant = McpSettings.merchant(env={})
    assert (shopping.host, shopping.port, shopping.url) == (
        "127.0.0.1",
        8300,
        "http://127.0.0.1:8300/mcp",
    )
    assert (merchant.host, merchant.port) == ("127.0.0.1", 8301)
    assert shopping.principal == "rowan@example.invalid"
    assert merchant.principal == "user:acme-operator"
    assert shopping.session_id != merchant.session_id
    assert shopping.db_path == merchant.db_path == str(DEFAULT_DB_PATH)
    assert shopping.unsafe_env_var != merchant.unsafe_env_var


def test_environment_overrides_every_field():
    settings = McpSettings.merchant(
        env={
            "MERCHANT_MCP_HOST": "0.0.0.0",
            "MERCHANT_MCP_PORT": "9001",
            "MERCHANT_MCP_SESSION_ID": "ops-1",
            "ACME_OPERATOR": "user:ops",
            "MERCHANT_MCP_DB": "/srv/store.db",
            "MERCHANT_MCP_MEMORY_FILE": "/srv/memory.json",
        }
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 9001
    assert settings.session_id == "ops-1"
    assert settings.principal == "user:ops"
    assert settings.db_path == "/srv/store.db"
    assert str(settings.memory_file) == "/srv/memory.json"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STOREFRONT_MCP_PORT": "http"}, "integer port"),
        ({"STOREFRONT_MCP_PORT": "70000"}, "between 1 and 65535"),
        ({"STOREFRONT_MCP_HOST": "  "}, "host must not be empty"),
        ({"STOREFRONT_MCP_SESSION_ID": " padded "}, "non-empty token"),
        ({"ACME_CUSTOMER": ""}, "principal must not be empty"),
        ({"STOREFRONT_MCP_DB": ":memory:"}, "file path"),
    ],
)
def test_invalid_values_are_refused(env, message):
    with pytest.raises(ValueError, match=message):
        McpSettings.shopping(env=env)


def test_build_servers_honor_explicit_settings(engine_db):
    from mcp_servers.merchant import build_merchant_server
    from mcp_servers.shopping import build_shopping_server

    shopping = McpSettings.shopping(env={"STOREFRONT_MCP_SESSION_ID": "custom-shop"})
    merchant = McpSettings.merchant(env={"MERCHANT_MCP_SESSION_ID": "custom-ops"})
    build_shopping_server(engine_db("a.db"), settings=shopping)
    build_merchant_server(engine_db("b.db"), settings=merchant)
