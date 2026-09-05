"""Health, readiness, metrics, and the unauthenticated capabilities probe."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..context import HostContext

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    authenticator = ctx.authenticator
    stablecoin_config = ctx.stablecoin_config
    stablecoin_payments = ctx.stablecoin_payments
    anthropic_client = ctx.anthropic_client
    metrics = ctx.metrics
    metrics_token = ctx.settings.metrics_token

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Prove the engine can answer a read, rather than only that Python is alive."""
        try:
            await store.call(lambda commerce: commerce.products.count())
        except Exception as error:
            raise HTTPException(status_code=503, detail="engine store is unavailable") from error
        return {"status": "ready"}

    @router.get("/metrics")
    async def prometheus_metrics(request: Request) -> PlainTextResponse:
        """Low-cardinality metrics, disabled until a dedicated token is configured."""
        if not metrics_token:
            raise HTTPException(status_code=404, detail="metrics are disabled")
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {metrics_token}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="metrics authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    @router.get("/capabilities")
    async def capabilities() -> dict[str, str]:
        """Whether a model is configured for this deployment -- present or absent,
        never valid or invalid, since that would require a call to the provider. Not
        session-scoped: a browser needs this before it has a session. Never echoes the
        key or the workspace id; this route never touches either value's contents."""
        return {
            "assistant": "available" if anthropic_client is not None else "unconfigured",
            "stablecoin_checkout": "available" if stablecoin_config.enabled else "disabled",
            "stablecoin_refunds": (
                "available"
                if stablecoin_payments.refunds_available
                else "deployment_integration_required"
                if stablecoin_config.enabled
                else "disabled"
            ),
            "direct_checkout": "available" if authenticator.config.mode == "demo" else "disabled",
        }

    return router
