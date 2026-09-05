"""The host's two middlewares and its CORS policy, installed in this order:

1. ``authenticate_commerce_request`` verifies identity (and, in JWT mode, that the
   session handle belongs to the signed subject) before any commerce route can reach
   an agent or the engine, and applies the durable per-principal rate limit.
2. CORS, added after the auth middleware so it wraps even an early 401/403.
3. ``correlate_and_secure`` attaches the request id, timing metrics, and the safe
   response headers, and binds the request id into the log context.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import AuthenticationError
from .context import HostContext
from .logs import request_id_var

_REQUEST_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
logger = logging.getLogger("host.app")


def _valid_request_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(char in _REQUEST_ID_CHARS for char in value)


def install_middleware(app: FastAPI, ctx: HostContext) -> None:
    store = ctx.store
    authenticator = ctx.authenticator
    metrics = ctx.metrics
    rate_limit_per_minute = ctx.settings.rate_limit_per_minute
    allowed_origins = list(ctx.settings.allowed_origins)

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
        # The context variable stays set until the completion record below has been
        # written, so that record carries the request id too.
        token = request_id_var.set(request_id)
        try:
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
        finally:
            request_id_var.reset(token)
