"""``build_anthropic_client`` is the one place an ``AsyncAnthropic`` gets constructed for
the host and the eval runner. No test here supplies a real key: with none set the
function must return ``None``, exactly like the pre-existing skip paths expect."""

from __future__ import annotations

from host.anthropic_client import build_anthropic_client


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert build_anthropic_client() is None


async def test_key_without_workspace_id_carries_no_workspace_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    client = build_anthropic_client()
    assert client is not None
    headers = client._custom_headers or {}
    assert "anthropic-workspace-id" not in headers
    await client.close()


async def test_key_with_workspace_id_carries_the_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc-test-not-real")
    client = build_anthropic_client()
    assert client is not None
    assert client._custom_headers.get("anthropic-workspace-id") == "wrkspc-test-not-real"
    await client.close()
