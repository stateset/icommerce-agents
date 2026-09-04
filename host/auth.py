"""Authentication for the HTTP host.

The reference app remains frictionless in ``demo`` mode. A deployment can switch to
``jwt`` mode, where every commerce request carries a verified bearer token and every
host session is cryptographically bound to that token's subject.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

import jwt


class AuthenticationError(ValueError):
    """A bearer token is missing, invalid, expired, or lacks required claims."""


@dataclass(frozen=True)
class AuthConfig:
    mode: Literal["demo", "jwt"] = "demo"
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    # Intended for automated tests and tightly controlled private deployments. Public
    # deployments should use asymmetric signing through ``jwks_url``.
    hs256_secret: str | None = None

    @classmethod
    def from_env(cls) -> AuthConfig:
        mode = os.getenv("ICOMMERCE_AUTH_MODE", "demo").strip().lower()
        if mode not in ("demo", "jwt"):
            raise ValueError("ICOMMERCE_AUTH_MODE must be 'demo' or 'jwt'")
        return cls(
            mode=mode,
            issuer=os.getenv("ICOMMERCE_JWT_ISSUER"),
            audience=os.getenv("ICOMMERCE_JWT_AUDIENCE"),
            jwks_url=os.getenv("ICOMMERCE_JWKS_URL"),
            hs256_secret=os.getenv("ICOMMERCE_JWT_HS256_SECRET"),
        )


@dataclass(frozen=True)
class Identity:
    subject: str
    roles: frozenset[str]
    scopes: frozenset[str]
    email: str | None
    store_id: str | None

    def permits(self, *, role: str, scope: str) -> bool:
        return role in self.roles or scope in self.scopes


def _string_set(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return frozenset(value)
    return frozenset()


class Authenticator:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self._jwks: jwt.PyJWKClient | None = None
        if config.mode == "demo":
            return
        if not config.issuer or not config.audience:
            raise ValueError("JWT auth requires ICOMMERCE_JWT_ISSUER and ICOMMERCE_JWT_AUDIENCE")
        if bool(config.jwks_url) == bool(config.hs256_secret):
            raise ValueError("JWT auth requires exactly one of JWKS URL or HS256 secret")
        if config.jwks_url:
            self._jwks = jwt.PyJWKClient(config.jwks_url)

    def authenticate(self, authorization: str | None) -> Identity | None:
        if self.config.mode == "demo":
            return None
        if not authorization:
            raise AuthenticationError("missing bearer token")
        if len(authorization) > 16_384:
            raise AuthenticationError("bearer token is too large")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise AuthenticationError("missing bearer token")
        token = token.strip()
        try:
            if self._jwks is not None:
                key = self._jwks.get_signing_key_from_jwt(token).key
                algorithms = ["RS256", "ES256"]
            else:
                key = self.config.hs256_secret
                algorithms = ["HS256"]
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise AuthenticationError("invalid bearer token") from error
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationError("invalid bearer token subject")
        email = claims.get("email")
        store_id = claims.get("store_id")
        return Identity(
            subject=subject.strip(),
            roles=_string_set(claims.get("roles") or claims.get("role")),
            scopes=_string_set(claims.get("scope") or claims.get("scp")),
            email=email.strip() if isinstance(email, str) and email.strip() else None,
            store_id=(store_id.strip() if isinstance(store_id, str) and store_id.strip() else None),
        )


__all__ = ["AuthConfig", "AuthenticationError", "Authenticator", "Identity"]
