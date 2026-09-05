"""Deployment-owned network clients must all be closed, including on failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from engine_backend.stablecoins import StablecoinConfig, StablecoinPayments
from host.app import create_app


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_shutdown_attempts_every_client(engine_db, monkeypatch, failure):
    claude = SimpleNamespace(close=AsyncMock())
    facilitator = SimpleNamespace(aclose=AsyncMock())
    refund_provider = SimpleNamespace(aclose=AsyncMock(side_effect=failure))
    monkeypatch.setattr("host.app.build_anthropic_client", lambda: claude)
    app = create_app(
        engine_db("store.db"),
        stablecoin_config=StablecoinConfig(enabled=False),
        stablecoin_facilitator=facilitator,
        stablecoin_refund_provider=refund_provider,
    )

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            claude.close.assert_not_called()
            facilitator.aclose.assert_not_called()
            refund_provider.aclose.assert_not_called()

    if failure is None:
        await run_lifespan()
    else:
        with pytest.raises(failure):
            await run_lifespan()
    claude.close.assert_awaited_once()
    facilitator.aclose.assert_awaited_once()
    refund_provider.aclose.assert_awaited_once()


async def test_shared_payment_provider_closed_only_once():
    provider = SimpleNamespace(aclose=AsyncMock())
    payments = StablecoinPayments(
        Mock(), StablecoinConfig(enabled=False), facilitator=provider, refund_provider=provider
    )
    await payments.aclose()
    provider.aclose.assert_awaited_once()


async def test_payment_cleanup_supports_optional_and_synchronous_close():
    provider = SimpleNamespace(aclose=Mock())
    payments = StablecoinPayments(
        Mock(), StablecoinConfig(enabled=False), facilitator=provider, refund_provider=object()
    )
    await payments.aclose()
    provider.aclose.assert_called_once()


async def test_keyless_host_still_closes_payments(engine_db, monkeypatch):
    monkeypatch.setattr("host.app.build_anthropic_client", lambda: None)
    provider = SimpleNamespace(aclose=AsyncMock())
    app = create_app(
        engine_db("store.db"),
        stablecoin_config=StablecoinConfig(enabled=False),
        stablecoin_facilitator=provider,
    )
    async with app.router.lifespan_context(app):
        pass
    provider.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("ICOMMERCE_METRICS_TOKEN", "short", "metrics token"),
        ("ICOMMERCE_SESSION_TTL_SECONDS", "1", "session TTL"),
        ("ICOMMERCE_CHAT_LEASE_SECONDS", "1", "chat turn lease"),
        ("ICOMMERCE_ENVIRONMENT", "invalid", "ICOMMERCE_ENVIRONMENT"),
        ("ICOMMERCE_ENVIRONMENT", "production", "unsafe production configuration"),
    ],
)
def test_invalid_config_does_not_allocate_network_clients(
    engine_db, monkeypatch, setting, value, message
):
    monkeypatch.setenv(setting, value)
    claude_factory = Mock()
    payment_factory = Mock()
    monkeypatch.setattr("host.app.build_anthropic_client", claude_factory)
    monkeypatch.setattr("host.app.StablecoinPayments", payment_factory)
    with pytest.raises(ValueError, match=message):
        create_app(engine_db("store.db"), stablecoin_config=StablecoinConfig(enabled=False))
    claude_factory.assert_not_called()
    payment_factory.assert_not_called()
