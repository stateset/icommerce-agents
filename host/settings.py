"""Deployment settings for the host, read from the environment once and validated
before any engine handle or network client is built.

``AuthConfig`` and ``StablecoinConfig`` keep their own ``from_env`` readers; this module
owns everything else ``create_app`` used to read inline, so a deployment has one place
to look for a knob and one place where an unsafe combination is refused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .auth import AuthConfig

DEFAULT_ALLOWED_ORIGINS = ("http://localhost:3000", "http://localhost:3100")


@dataclass(frozen=True)
class HostSettings:
    environment: str = "development"
    stale_apply_seconds: int = 900
    session_ttl_seconds: int = 28800
    chat_lease_seconds: int = 900
    rate_limit_per_minute: int = 0
    metrics_token: str | None = None
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS

    @classmethod
    def from_env(cls, *, stale_apply_seconds: int | None = None) -> HostSettings:
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "ICOMMERCE_ALLOWED_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)
            ).split(",")
            if origin.strip()
        )
        return cls(
            environment=os.getenv("ICOMMERCE_ENVIRONMENT", "development").strip().lower(),
            stale_apply_seconds=(
                int(os.getenv("ICOMMERCE_STALE_APPLY_SECONDS", "900"))
                if stale_apply_seconds is None
                else stale_apply_seconds
            ),
            session_ttl_seconds=int(os.getenv("ICOMMERCE_SESSION_TTL_SECONDS", "28800")),
            chat_lease_seconds=int(os.getenv("ICOMMERCE_CHAT_LEASE_SECONDS", "900")),
            rate_limit_per_minute=int(os.getenv("ICOMMERCE_RATE_LIMIT_PER_MINUTE", "0")),
            metrics_token=os.getenv("ICOMMERCE_METRICS_TOKEN"),
            allowed_origins=origins,
        )

    def validate(self, *, auth_config: AuthConfig) -> None:
        """Reject values that are invalid anywhere, then combinations unsafe on the edge."""
        if self.stale_apply_seconds < 1:
            raise ValueError("stale apply recovery threshold must be at least one second")
        if self.session_ttl_seconds < 60:
            raise ValueError("session TTL must be at least 60 seconds")
        if not 30 <= self.chat_lease_seconds <= 3600:
            raise ValueError("chat turn lease must be between 30 and 3600 seconds")
        if self.rate_limit_per_minute < 0:
            raise ValueError("request rate limit cannot be negative")
        if self.metrics_token is not None and len(self.metrics_token.encode()) < 32:
            raise ValueError("metrics token must be at least 32 bytes")
        validate_production_deployment(self, auth_config=auth_config)


def validate_production_deployment(settings: HostSettings, *, auth_config: AuthConfig) -> None:
    """Reject configurations that are safe for a demo but unsafe on the public edge."""
    if settings.environment not in {"development", "test", "production"}:
        raise ValueError("ICOMMERCE_ENVIRONMENT must be development, test, or production")
    if settings.environment != "production":
        return
    problems: list[str] = []
    if auth_config.mode != "jwt":
        problems.append("ICOMMERCE_AUTH_MODE must be jwt")
    elif auth_config.jwks_url is None:
        problems.append("asymmetric ICOMMERCE_JWKS_URL authentication is required")
    if settings.metrics_token is None:
        problems.append("ICOMMERCE_METRICS_TOKEN is required")
    if settings.rate_limit_per_minute <= 0:
        problems.append("ICOMMERCE_RATE_LIMIT_PER_MINUTE must be enabled")
    for origin in settings.allowed_origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            problems.append(f"browser origin must be an HTTPS origin: {origin!r}")
    if not settings.allowed_origins:
        problems.append("at least one browser origin is required")
    if problems:
        raise ValueError("unsafe production configuration: " + "; ".join(problems))
