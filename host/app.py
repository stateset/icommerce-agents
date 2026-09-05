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

This module builds the deployment (``create_app``) and owns the two middlewares. The
routes live in ``host.routes``, shared helpers in ``host.context``, request bodies in
``host.schemas``, and every environment knob in ``host.settings``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

from . import logs
from .anthropic_client import build_anthropic_client
from .auth import AuthConfig, AuthenticationError, Authenticator
from .context import HostContext, _with_change_evidence
from .metrics import HostMetrics
from .routes import build_routers
from .sessions import SessionRegistry
from .settings import HostSettings

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_REQUEST_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
logger = logging.getLogger(__name__)


def _valid_request_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(char in _REQUEST_ID_CHARS for char in value)


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
    rate_limit_per_minute = settings.rate_limit_per_minute
    allowed_origins = list(settings.allowed_origins)

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

    @app.middleware("http")
    async def authenticate_commerce_request(request: Request, call_next):
        """Verify identity before any commerce route can reach an agent or engine.

        In JWT mode a session id is only a workflow handle, never a bearer credential:
        subsequent requests must present the same signed subject that created it.
        """
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith(("/shopping/", "/merchant/")):
            return await call_next(request)
        try:
            identity = await asyncio.to_thread(
                authenticator.authenticate, request.headers.get("Authorization")
            )
        except AuthenticationError as error:
            return JSONResponse(
                status_code=401,
                content={"detail": str(error)},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.identity = identity
        expected_kind = "customer" if path.startswith("/shopping/") else "operator"
        if identity is not None:
            if expected_kind == "customer" and not identity.permits(
                role="customer", scope="shopping:use"
            ):
                return JSONResponse(status_code=403, content={"detail": "shopping access required"})
            if expected_kind == "operator" and (
                not identity.permits(role="merchant", scope="merchant:write")
                or identity.store_id != store.store_id
            ):
                return JSONResponse(status_code=403, content={"detail": "merchant access required"})
        if rate_limit_per_minute:
            principal = identity.subject if identity is not None else "demo"
            window_start = int(time.time() // 60) * 60
            allowed = await asyncio.to_thread(
                store.consume_rate_limit,
                f"{path.split('/', 2)[1]}:{principal}",
                rate_limit_per_minute,
                window_start,
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "request rate limit exceeded"},
                    headers={"Retry-After": str(60 - int(time.time()) % 60)},
                )
        if authenticator.config.mode == "demo" or path in (
            "/shopping/session",
            "/merchant/session",
        ):
            return await call_next(request)
        session_id = request.headers.get("X-Session-Id")
        if not session_id:
            return JSONResponse(status_code=401, content={"detail": "missing X-Session-Id"})
        try:
            binding = store.binding(session_id)
        except KeyError:
            return JSONResponse(status_code=401, content={"detail": "unknown session"})
        if binding.kind != expected_kind:
            return JSONResponse(status_code=403, content={"detail": "session role mismatch"})
        if identity is None or binding.authenticated_subject != identity.subject:
            return JSONResponse(status_code=403, content={"detail": "session subject mismatch"})
        return await call_next(request)

    # Added after the auth middleware so CORS wraps even an early 401/403 response.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "PAYMENT-SIGNATURE",
            "X-Request-Id",
            "X-Session-Id",
        ],
        expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-Request-Id"],
    )

    @app.middleware("http")
    async def correlate_and_secure(request: Request, call_next):
        """Attach a non-secret correlation id and safe API response defaults."""
        supplied = request.headers.get("X-Request-Id", "")
        request_id = supplied if _valid_request_id(supplied) else secrets.token_hex(16)
        request.state.request_id = request_id
        token = logs.request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
            )
            raise
        finally:
            logs.request_id_var.reset(token)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        metrics.request(request.method, route_path, response.status_code, elapsed_ms / 1000)
        response.headers["X-Request-Id"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        logger.info(
            "request completed method=%s path=%s status=%d elapsed_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

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
