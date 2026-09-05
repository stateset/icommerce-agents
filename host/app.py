"""The FastAPI host: both commerce-agents roles behind HTTP, over one engine store.

Two rules this package exists to enforce:

- The model never completes an order. ``checkout`` (the agent tool) renders the cart and
  charges nothing; only trusted shopping routes complete one. Direct checkout and the
  optional x402 rail both go through the governed ``checkout.commit`` kernel command,
  and no agent tool reaches either route.
- Approval is the operator's, and it happens here. ``POST /merchant/changes/{id}/approve``
  is the only place ``EngineMerchant.approve`` is called, the operator comes from the
  session binding, never the request body, and an unknown change id is a 404 before that
  call is ever made.

Identity is never a tool argument and never a request body field: ``POST .../session``
mints an unguessable id, binds it to a principal server-side, and returns only the id.
Every other route reads it back from ``X-Session-Id``.

This module builds the deployment (``create_app``). The middlewares live in
``host.middleware``, the routes in ``host.routes``, shared helpers in ``host.context``,
request bodies in ``host.schemas``, and every environment knob in ``host.settings``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from merchant_agent.types import ChangeStatus, MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent
from shopping_agent.types import ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent

from engine_backend import SKILLS_DIR
from engine_backend.agent_config import merchant_agent_config, shopping_agent_config
from engine_backend.kernel import KernelClient
from engine_backend.merchant import EngineMerchant
from engine_backend.seed import seed_store
from engine_backend.stablecoins import (
    Facilitator,
    RefundProvider,
    StablecoinConfig,
    StablecoinPayments,
)
from engine_backend.store import EngineStore
from engine_backend.storefront import EngineStorefront

from .anthropic_client import build_anthropic_client
from .auth import AuthConfig, Authenticator
from .context import HostContext, _with_change_evidence
from .metrics import HostMetrics
from .middleware import install_middleware
from .routes import build_routers
from .sessions import SessionRegistry
from .settings import HostSettings

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def create_app(
    db_path: str,
    auth_config: AuthConfig | None = None,
    *,
    stale_apply_seconds: int | None = None,
    stablecoin_config: StablecoinConfig | None = None,
    stablecoin_facilitator: Facilitator | None = None,
    stablecoin_refund_provider: RefundProvider | None = None,
) -> FastAPI:
    """Build one deployment: one engine store (seeded), both backends, one kernel
    client bound to the host-owned policy and principal files, and both agents."""
    # Validate deployment settings before opening the engine or allocating any
    # network client, so a misconfigured deployment fails in milliseconds.
    authenticator = Authenticator(auth_config or AuthConfig.from_env())
    settings = HostSettings.from_env(stale_apply_seconds=stale_apply_seconds)
    settings.validate(auth_config=authenticator.config)
    stablecoin_config = stablecoin_config or StablecoinConfig.from_env()
    stablecoin_config.validate()

    store = EngineStore(db_path)
    store.cleanup_expired_sessions()
    seed_store(store.commerce)

    storefront = EngineStorefront(store)
    kernel = KernelClient(
        store, CONFIG_DIR / "kernel-policy.json", CONFIG_DIR / "kernel-principal.json"
    )
    merchant = EngineMerchant(store, kernel)
    stablecoin_payments = StablecoinPayments(
        store,
        stablecoin_config,
        facilitator=stablecoin_facilitator,
        refund_provider=stablecoin_refund_provider,
    )
    anthropic_client = build_anthropic_client()
    shopping_agent = ShoppingAgent(
        backend=storefront,
        skills_dir=SKILLS_DIR("shopping"),
        config=shopping_agent_config(),
        client=anthropic_client,
    )
    merchant_agent = MerchantAgent(
        backend=merchant,
        skills_dir=SKILLS_DIR("merchant"),
        config=merchant_agent_config(),
        client=anthropic_client,
    )
    shopping_sessions: SessionRegistry[ShoppingSessionState] = SessionRegistry(
        ShoppingSessionState, store, "shopping", settings.chat_lease_seconds
    )
    merchant_sessions: SessionRegistry[MerchantSessionState] = SessionRegistry(
        MerchantSessionState, store, "merchant", settings.chat_lease_seconds
    )
    metrics = HostMetrics()
    ctx = HostContext(
        settings=settings,
        store=store,
        storefront=storefront,
        kernel=kernel,
        merchant=merchant,
        authenticator=authenticator,
        stablecoin_config=stablecoin_config,
        stablecoin_payments=stablecoin_payments,
        anthropic_client=anthropic_client,
        shopping_agent=shopping_agent,
        merchant_agent=merchant_agent,
        shopping_sessions=shopping_sessions,
        merchant_sessions=merchant_sessions,
        metrics=metrics,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Both agents share this client. Close it once, and attempt every cleanup
        # even if a provider's close raises (including cancellation).
        async with AsyncExitStack() as cleanup:
            if anthropic_client is not None:
                cleanup.push_async_callback(anthropic_client.close)
            cleanup.push_async_callback(stablecoin_payments.aclose)
            yield

    app = FastAPI(title="StateSet iCommerce agents host", lifespan=lifespan)

    install_middleware(app, ctx)
    for router in build_routers(ctx):
        app.include_router(router)
    app.state.context = ctx
    return app


__all__ = [
    "CONFIG_DIR",
    "ChangeStatus",
    "MerchantAgent",
    "MerchantSessionContext",
    "StablecoinPayments",
    "_with_change_evidence",
    "build_anthropic_client",
    "create_app",
]
