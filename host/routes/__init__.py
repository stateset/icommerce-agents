"""One ``APIRouter`` per surface. Each ``build_router`` closes over the shared
:class:`host.context.HostContext` so route bodies read like the single-file host they
were split out of."""

from __future__ import annotations

from fastapi import APIRouter

from ..context import HostContext
from . import merchant, refunds, sessions, shopping, stablecoin, system


def build_routers(ctx: HostContext) -> list[APIRouter]:
    return [
        system.build_router(ctx),
        sessions.build_router(ctx),
        shopping.build_router(ctx),
        stablecoin.build_router(ctx),
        merchant.build_router(ctx),
        refunds.build_router(ctx),
    ]
