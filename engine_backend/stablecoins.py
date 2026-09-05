"""Durable x402 stablecoin payment hand-off for checkout.

The embedded commerce engine remains the authority for carts and order creation.  This
adapter owns only the external payment boundary: immutable quotes, facilitator
verification/settlement, replay protection, and the recovery state between settlement
and the governed ``checkout.commit`` command.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .store import EngineStore

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_NETWORK = re.compile(r"^eip155:[1-9][0-9]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAYMENT_HEADER_BYTES = 65_536


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class StablecoinConfig:
    """One deliberately narrow production rail: exact-payment ERC-20 over x402 v2."""

    enabled: bool = False
    facilitator_url: str = ""
    public_base_url: str = ""
    network: str = "eip155:8453"
    asset_symbol: str = "USDC"
    asset_name: str = "USDC"
    asset_version: str = "2"
    asset_address: str = ""
    asset_decimals: int = 6
    settlement_currency: str = "USD"
    max_amount: Decimal = Decimal("10000.00")
    pay_to: str = ""
    quote_ttl_seconds: int = 300
    processing_timeout_seconds: int = 60
    facilitator_bearer_token: str | None = field(default=None, repr=False)
    refund_url: str = ""
    refund_bearer_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> StablecoinConfig:
        config = cls(
            enabled=_env_bool("ICOMMERCE_STABLECOIN_ENABLED"),
            facilitator_url=os.getenv("ICOMMERCE_X402_FACILITATOR_URL", "").strip(),
            public_base_url=os.getenv("ICOMMERCE_PUBLIC_BASE_URL", "").strip(),
            network=os.getenv("ICOMMERCE_STABLECOIN_NETWORK", "eip155:8453").strip(),
            asset_symbol=os.getenv("ICOMMERCE_STABLECOIN_ASSET_SYMBOL", "USDC").strip(),
            asset_name=os.getenv("ICOMMERCE_STABLECOIN_ASSET_NAME", "USDC").strip(),
            asset_version=os.getenv("ICOMMERCE_STABLECOIN_ASSET_VERSION", "2").strip(),
            asset_address=os.getenv("ICOMMERCE_STABLECOIN_ASSET_ADDRESS", "").strip(),
            asset_decimals=int(os.getenv("ICOMMERCE_STABLECOIN_ASSET_DECIMALS", "6")),
            settlement_currency=os.getenv(
                "ICOMMERCE_STABLECOIN_SETTLEMENT_CURRENCY", "USD"
            ).strip(),
            max_amount=Decimal(os.getenv("ICOMMERCE_STABLECOIN_MAX_AMOUNT", "10000.00")),
            pay_to=os.getenv("ICOMMERCE_STABLECOIN_PAY_TO", "").strip(),
            quote_ttl_seconds=int(os.getenv("ICOMMERCE_STABLECOIN_QUOTE_TTL_SECONDS", "300")),
            processing_timeout_seconds=int(
                os.getenv("ICOMMERCE_STABLECOIN_PROCESSING_TIMEOUT_SECONDS", "60")
            ),
            facilitator_bearer_token=os.getenv("ICOMMERCE_X402_FACILITATOR_TOKEN"),
            refund_url=os.getenv("ICOMMERCE_STABLECOIN_REFUND_URL", "").strip(),
            refund_bearer_token=os.getenv("ICOMMERCE_STABLECOIN_REFUND_TOKEN"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.enabled:
            return
        if not _NETWORK.fullmatch(self.network):
            raise ValueError("stablecoin network must be an EVM CAIP-2 id such as eip155:8453")
        if not _ADDRESS.fullmatch(self.asset_address):
            raise ValueError("stablecoin asset address must be a 20-byte EVM address")
        if not _ADDRESS.fullmatch(self.pay_to):
            raise ValueError("stablecoin pay-to address must be a 20-byte EVM address")
        if not 0 <= self.asset_decimals <= 18:
            raise ValueError("stablecoin asset decimals must be between 0 and 18")
        if not 30 <= self.quote_ttl_seconds <= 900:
            raise ValueError("stablecoin quote TTL must be between 30 and 900 seconds")
        if not 15 <= self.processing_timeout_seconds <= 900:
            raise ValueError("stablecoin processing timeout must be between 15 and 900 seconds")
        if not self.asset_symbol or len(self.asset_symbol) > 20:
            raise ValueError(
                "stablecoin asset symbol is required and must be at most 20 characters"
            )
        if not self.asset_name or len(self.asset_name) > 100:
            raise ValueError("stablecoin asset name is required and must be at most 100 characters")
        if not self.asset_version or len(self.asset_version) > 20:
            raise ValueError(
                "stablecoin asset version is required and must be at most 20 characters"
            )
        if not re.fullmatch(r"[A-Z]{3}", self.settlement_currency):
            raise ValueError("stablecoin settlement currency must be a three-letter ISO code")
        if not self.max_amount.is_finite() or self.max_amount <= 0:
            raise ValueError("stablecoin maximum amount must be a positive finite decimal")
        self._validate_url("facilitator", self.facilitator_url, allow_local_http=True)
        self._validate_url("public base", self.public_base_url, allow_local_http=False)
        if self.refund_url:
            self._validate_url("refund", self.refund_url, allow_local_http=True)

    @staticmethod
    def _validate_url(label: str, value: str, *, allow_local_http: bool) -> None:
        parsed = urlparse(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        valid_scheme = parsed.scheme == "https" or (
            allow_local_http and local and parsed.scheme == "http"
        )
        if not valid_scheme or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"stablecoin {label} URL must be an HTTPS origin")
        if parsed.query or parsed.fragment:
            raise ValueError(f"stablecoin {label} URL cannot include a query or fragment")
        if label == "public base" and parsed.path not in {"", "/"}:
            raise ValueError("stablecoin public base URL must be an origin without a path")


@dataclass(frozen=True)
class FacilitatorResult:
    success: bool
    payer: str | None = None
    transaction: str | None = None
    reason: str | None = None
    network: str | None = None


class Facilitator(Protocol):
    async def verify(
        self, payment_payload: dict[str, Any], requirements: dict[str, Any]
    ) -> FacilitatorResult: ...

    async def settle(
        self, payment_payload: dict[str, Any], requirements: dict[str, Any]
    ) -> FacilitatorResult: ...


@dataclass(frozen=True)
class RefundResult:
    success: bool
    transaction: str | None = None
    reason: str | None = None
    network: str | None = None


class RefundProvider(Protocol):
    async def refund(self, request: dict[str, Any], idempotency_key: str) -> RefundResult: ...


class FacilitatorUncertain(RuntimeError):
    """The facilitator call failed without proving whether settlement occurred."""


class RefundUncertain(RuntimeError):
    """The refund provider call failed without proving whether funds moved."""

    def __init__(self, message: str, refund: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.refund = refund


class HttpX402Facilitator:
    def __init__(self, config: StablecoinConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._base_url = config.facilitator_url.rstrip("/")
        self._token = config.facilitator_bearer_token
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _post(
        self, operation: str, payment_payload: dict[str, Any], requirements: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "icommerce-agents/x402-v2"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = {
            "x402Version": 2,
            "paymentPayload": payment_payload,
            "paymentRequirements": requirements,
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/{operation}", json=body, headers=headers
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            # Especially for /settle, a timeout or a malformed gateway response is not
            # proof that no transaction was broadcast.  The caller must reconcile it.
            raise FacilitatorUncertain(f"facilitator {operation} outcome is unknown") from error
        if not isinstance(result, dict):
            raise FacilitatorUncertain(f"facilitator {operation} returned an invalid response")
        return result

    async def verify(
        self, payment_payload: dict[str, Any], requirements: dict[str, Any]
    ) -> FacilitatorResult:
        result = await self._post("verify", payment_payload, requirements)
        return FacilitatorResult(
            success=bool(result.get("isValid", result.get("success", False))),
            payer=result.get("payer"),
            reason=result.get("invalidReason") or result.get("errorReason") or result.get("error"),
            network=result.get("network"),
        )

    async def settle(
        self, payment_payload: dict[str, Any], requirements: dict[str, Any]
    ) -> FacilitatorResult:
        result = await self._post("settle", payment_payload, requirements)
        return FacilitatorResult(
            success=bool(result.get("success", False)),
            payer=result.get("payer"),
            transaction=result.get("transaction"),
            reason=result.get("errorReason") or result.get("error"),
            network=result.get("network"),
        )


class HttpStablecoinRefundProvider:
    """Minimal treasury boundary for provider-managed ERC-20 refunds.

    Refunds are intentionally not part of x402 itself. Deployments expose a small HTTPS
    adapter around their signer/custodian and return a transaction hash only after the
    transfer is accepted. Ambiguous transport outcomes are never retried automatically.
    """

    def __init__(self, config: StablecoinConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._url = config.refund_url
        self._token = config.refund_bearer_token
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def refund(self, request: dict[str, Any], idempotency_key: str) -> RefundResult:
        headers = {
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "icommerce-agents/stablecoin-refunds-v1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(self._url, json=request, headers=headers)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RefundUncertain("stablecoin refund outcome is unknown") from error
        if not isinstance(result, dict):
            raise RefundUncertain("stablecoin refund provider returned an invalid response")
        return RefundResult(
            success=result.get("success") is True,
            transaction=result.get("transaction"),
            reason=result.get("errorReason") or result.get("error"),
            network=result.get("network"),
        )


class PaymentNotFound(KeyError):
    pass


class RefundNotFound(KeyError):
    pass


class PaymentConflict(RuntimeError):
    pass


class StablecoinLedger:
    """Transactional access to the adapter-owned payment and audit tables."""

    def __init__(self, store: EngineStore, processing_timeout_seconds: int) -> None:
        self.store = store
        self.processing_timeout_seconds = processing_timeout_seconds

    @staticmethod
    def _event(
        connection: sqlite3.Connection, payment_id: str, event: str, detail: str | None = None
    ) -> None:
        connection.execute(
            "INSERT INTO icommerce_stablecoin_payment_events "
            "(payment_id, event, occurred_at, detail) VALUES (?, ?, ?, ?)",
            (payment_id, event, _iso(_utcnow()), detail),
        )

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_stale(connection, cart_id=values["cart_id"])
            active = connection.execute(
                "SELECT payment_id, state, expires_at "
                "FROM icommerce_stablecoin_payments "
                "WHERE cart_id = ? AND state NOT IN ('failed', 'expired')",
                (values["cart_id"],),
            ).fetchall()
            now = _utcnow()
            for row in active:
                if row["state"] == "quoted" and datetime.fromisoformat(row["expires_at"]) <= now:
                    connection.execute(
                        "UPDATE icommerce_stablecoin_payments "
                        "SET state = 'expired', updated_at = ? WHERE payment_id = ?",
                        (_iso(now), row["payment_id"]),
                    )
                    self._event(connection, row["payment_id"], "expired")
                    continue
                raise PaymentConflict(f"cart already has a nonterminal payment {row['payment_id']}")
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO icommerce_stablecoin_payments ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            self._event(connection, values["payment_id"], "quoted")
            connection.commit()
            return self._get(connection, values["payment_id"], values["session_id"])
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover_stale(
        self,
        connection: sqlite3.Connection,
        *,
        cart_id: str | None = None,
        payment_id: str | None = None,
    ) -> None:
        cutoff = _utcnow() - timedelta(seconds=self.processing_timeout_seconds)
        clauses = ["state IN ('verifying', 'verified', 'settling', 'checkout_committing')"]
        parameters: list[Any] = []
        if cart_id is not None:
            clauses.append("cart_id = ?")
            parameters.append(cart_id)
        if payment_id is not None:
            clauses.append("payment_id = ?")
            parameters.append(payment_id)
        rows = connection.execute(
            "SELECT * FROM icommerce_stablecoin_payments WHERE " + " AND ".join(clauses),
            parameters,
        ).fetchall()
        now = _utcnow()
        for row in rows:
            if datetime.fromisoformat(row["updated_at"]) > cutoff:
                continue
            if row["state"] in {"verifying", "verified"}:
                next_state = (
                    "expired" if datetime.fromisoformat(row["expires_at"]) <= now else "quoted"
                )
                detail = "recovered abandoned pre-settlement processing"
            else:
                next_state = "reconciliation_required"
                detail = "recovered abandoned operation with an unknown external outcome"
            connection.execute(
                "UPDATE icommerce_stablecoin_payments "
                "SET state = ?, updated_at = ?, last_error = ? WHERE payment_id = ?",
                (next_state, _iso(now), detail, row["payment_id"]),
            )
            self._event(connection, row["payment_id"], next_state, detail)

    def recover_stale(self, *, payment_id: str | None = None) -> None:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_stale(connection, payment_id=payment_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _get(
        connection: sqlite3.Connection, payment_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        if session_id is None:
            row = connection.execute(
                "SELECT * FROM icommerce_stablecoin_payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM icommerce_stablecoin_payments "
                "WHERE payment_id = ? AND session_id = ?",
                (payment_id, session_id),
            ).fetchone()
        if row is None:
            raise PaymentNotFound(payment_id)
        return dict(row)

    def get(self, payment_id: str, session_id: str | None = None) -> dict[str, Any]:
        connection = self.store._control_connection()
        try:
            return self._get(connection, payment_id, session_id)
        finally:
            connection.close()

    def list_for_store(self, store_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.recover_stale()
        connection = self.store._control_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM icommerce_stablecoin_payments "
                "WHERE store_id = ? AND state IN "
                "('settling', 'settled', 'checkout_committing', 'completed', "
                "'reconciliation_required') "
                "ORDER BY updated_at DESC LIMIT ?",
                (store_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def transition(
        self,
        payment_id: str,
        session_id: str,
        expected: set[str],
        state: str,
        *,
        event: str | None = None,
        event_detail: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get(connection, payment_id, session_id)
            if row["state"] not in expected:
                connection.rollback()
                raise PaymentConflict(f"payment is {row['state']}")
            updates = {"state": state, "updated_at": _iso(_utcnow()), **fields}
            clause = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(
                f"UPDATE icommerce_stablecoin_payments SET {clause} WHERE payment_id = ?",
                (*updates.values(), payment_id),
            )
            self._event(
                connection,
                payment_id,
                event or state,
                event_detail if event_detail is not None else fields.get("last_error"),
            )
            connection.commit()
            return self._get(connection, payment_id, session_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class StablecoinRefundLedger:
    """Durable refund claims and outcomes around a non-transactional treasury call."""

    def __init__(self, store: EngineStore, processing_timeout_seconds: int) -> None:
        self.store = store
        self.processing_timeout_seconds = processing_timeout_seconds

    @staticmethod
    def _event(
        connection: sqlite3.Connection, refund_id: str, event: str, detail: str | None = None
    ) -> None:
        connection.execute(
            "INSERT INTO icommerce_stablecoin_refund_events "
            "(refund_id, event, occurred_at, detail) VALUES (?, ?, ?, ?)",
            (refund_id, event, _iso(_utcnow()), detail),
        )

    @staticmethod
    def _get(connection: sqlite3.Connection, refund_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM icommerce_stablecoin_refunds WHERE refund_id = ?", (refund_id,)
        ).fetchone()
        if row is None:
            raise RefundNotFound(refund_id)
        return dict(row)

    def recover_stale(self, refund_id: str | None = None) -> None:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cutoff = _iso(_utcnow() - timedelta(seconds=self.processing_timeout_seconds))
            query = (
                "SELECT refund_id FROM icommerce_stablecoin_refunds "
                "WHERE state = 'submitting' AND updated_at <= ?"
            )
            parameters: list[Any] = [cutoff]
            if refund_id is not None:
                query += " AND refund_id = ?"
                parameters.append(refund_id)
            for row in connection.execute(query, parameters).fetchall():
                detail = "recovered abandoned refund with an unknown external outcome"
                connection.execute(
                    "UPDATE icommerce_stablecoin_refunds "
                    "SET state = 'reconciliation_required', updated_at = ?, last_error = ? "
                    "WHERE refund_id = ?",
                    (_iso(_utcnow()), detail, row["refund_id"]),
                )
                self._event(connection, row["refund_id"], "reconciliation_required", detail)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            payment = connection.execute(
                "SELECT * FROM icommerce_stablecoin_payments WHERE payment_id = ? AND store_id = ?",
                (values["payment_id"], values["store_id"]),
            ).fetchone()
            if payment is None:
                raise PaymentNotFound(values["payment_id"])
            if payment["state"] != "completed":
                raise PaymentConflict("only a completed stablecoin payment can be refunded")
            existing = connection.execute(
                "SELECT * FROM icommerce_stablecoin_refunds WHERE idempotency_key = ?",
                (values["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                existing = dict(existing)
                bound = ("payment_id", "amount", "proposal_digest", "operator")
                if any(existing[field] != values[field] for field in bound):
                    raise PaymentConflict("idempotency key is bound to a different refund")
                cutoff = _utcnow() - timedelta(seconds=self.processing_timeout_seconds)
                if (
                    existing["state"] == "submitting"
                    and datetime.fromisoformat(existing["updated_at"]) <= cutoff
                ):
                    detail = "recovered abandoned refund with an unknown external outcome"
                    connection.execute(
                        "UPDATE icommerce_stablecoin_refunds "
                        "SET state = 'reconciliation_required', updated_at = ?, last_error = ? "
                        "WHERE refund_id = ?",
                        (_iso(_utcnow()), detail, existing["refund_id"]),
                    )
                    self._event(
                        connection, existing["refund_id"], "reconciliation_required", detail
                    )
                    existing = self._get(connection, existing["refund_id"])
                connection.commit()
                return existing, False
            reserved = connection.execute(
                "SELECT amount_atomic FROM icommerce_stablecoin_refunds "
                "WHERE payment_id = ? AND state != 'failed'",
                (values["payment_id"],),
            ).fetchall()
            total = sum(int(row["amount_atomic"]) for row in reserved)
            if total + int(values["amount_atomic"]) > int(payment["amount_atomic"]):
                raise PaymentConflict("refund exceeds the unrefunded stablecoin balance")
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO icommerce_stablecoin_refunds ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            self._event(connection, values["refund_id"], "submitting")
            connection.commit()
            return self._get(connection, values["refund_id"]), True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition(
        self,
        refund_id: str,
        expected: set[str],
        state: str,
        *,
        event_detail: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        connection = self.store._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            refund = self._get(connection, refund_id)
            if refund["state"] not in expected:
                raise PaymentConflict(f"refund is {refund['state']}")
            updates = {"state": state, "updated_at": _iso(_utcnow()), **fields}
            clause = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(
                f"UPDATE icommerce_stablecoin_refunds SET {clause} WHERE refund_id = ?",
                (*updates.values(), refund_id),
            )
            self._event(
                connection,
                refund_id,
                state,
                event_detail if event_detail is not None else fields.get("last_error"),
            )
            connection.commit()
            return self._get(connection, refund_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, refund_id: str) -> dict[str, Any]:
        self.recover_stale(refund_id)
        connection = self.store._control_connection()
        try:
            return self._get(connection, refund_id)
        finally:
            connection.close()

    def list_for_store(self, store_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.recover_stale()
        connection = self.store._control_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM icommerce_stablecoin_refunds WHERE store_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (store_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


class StablecoinPayments:
    def __init__(
        self,
        store: EngineStore,
        config: StablecoinConfig,
        facilitator: Facilitator | None = None,
        refund_provider: RefundProvider | None = None,
    ) -> None:
        config.validate()
        self.store = store
        self.config = config
        self.ledger = (
            StablecoinLedger(store, config.processing_timeout_seconds) if config.enabled else None
        )
        self.facilitator = facilitator or (HttpX402Facilitator(config) if config.enabled else None)
        self.refund_provider = refund_provider or (
            HttpStablecoinRefundProvider(config) if config.enabled and config.refund_url else None
        )
        self.refund_ledger = (
            StablecoinRefundLedger(store, config.processing_timeout_seconds)
            if config.enabled
            else None
        )

    async def aclose(self) -> None:
        async def close_client(client: Any) -> None:
            close = getattr(client, "aclose", None)
            if close is not None:
                result = close()
                if isinstance(result, Awaitable):
                    await result

        async with AsyncExitStack() as cleanup:
            seen: set[int] = set()
            for client in (self.facilitator, self.refund_provider):
                if client is not None and id(client) not in seen:
                    seen.add(id(client))
                    cleanup.push_async_callback(close_client, client)

    @property
    def refunds_available(self) -> bool:
        return self.refund_provider is not None and self.refund_ledger is not None

    @staticmethod
    def atomic_amount(amount: str, decimals: int) -> str:
        try:
            value = Decimal(amount)
        except InvalidOperation as error:
            raise ValueError("cart total is not a decimal amount") from error
        scaled = value * (Decimal(10) ** decimals)
        if value <= 0 or scaled != scaled.to_integral_value():
            raise ValueError("cart total cannot be represented exactly by the configured asset")
        return str(int(scaled))

    async def quote(
        self,
        *,
        session_id: str,
        customer_id: str,
        store_id: str,
        cart_id: str,
        cart_snapshot: dict[str, Any],
        shipping_address: dict[str, Any],
        payer_address: str,
    ) -> dict[str, Any]:
        if not self.config.enabled or self.ledger is None:
            raise PaymentConflict("stablecoin checkout is disabled")
        if not _ADDRESS.fullmatch(payer_address):
            raise ValueError("payer_address must be a 20-byte EVM address")
        amount = str(cart_snapshot["grand_total_exact"])
        if cart_snapshot["currency"] != self.config.settlement_currency:
            raise PaymentConflict(
                "cart currency is not supported by the configured stablecoin rail"
            )
        if Decimal(amount) > self.config.max_amount:
            raise PaymentConflict("cart total exceeds the configured stablecoin limit")
        amount_atomic = self.atomic_amount(amount, self.config.asset_decimals)
        now = _utcnow()
        expires = now + timedelta(seconds=self.config.quote_ttl_seconds)
        payment_id = f"pay_{uuid4().hex}"
        cart_digest = digest(cart_snapshot)
        quote_digest = digest(
            {
                "payment_id": payment_id,
                "store_id": store_id,
                "cart_id": cart_id,
                "cart_digest": cart_digest,
                "amount": amount,
                "currency": cart_snapshot["currency"],
                "network": self.config.network,
                "asset": self.config.asset_address.lower(),
                "pay_to": self.config.pay_to.lower(),
                "payer": payer_address.lower(),
                "shipping_address": shipping_address,
                "expires_at": _iso(expires),
            }
        )
        resource_url = (
            self.config.public_base_url.rstrip("/") + f"/shopping/checkout/stablecoin/{payment_id}"
        )
        requirement = {
            "scheme": "exact",
            "network": self.config.network,
            "amount": amount_atomic,
            "asset": self.config.asset_address,
            "payTo": self.config.pay_to,
            "maxTimeoutSeconds": self.config.quote_ttl_seconds,
            "extra": {
                "name": self.config.asset_name,
                "version": self.config.asset_version,
                "quoteDigest": quote_digest,
            },
        }
        payment_required = {
            "x402Version": 2,
            "error": "Payment required",
            "resource": {
                "url": resource_url,
                "description": f"StateSet checkout {payment_id}",
                "mimeType": "application/json",
            },
            "accepts": [requirement],
            "extensions": {},
        }
        values = {
            "payment_id": payment_id,
            "session_id": session_id,
            "customer_id": customer_id,
            "store_id": store_id,
            "cart_id": cart_id,
            "cart_digest": cart_digest,
            "quote_digest": quote_digest,
            "amount": amount,
            "amount_atomic": amount_atomic,
            "currency": cart_snapshot["currency"],
            "asset_symbol": self.config.asset_symbol,
            "asset_address": self.config.asset_address.lower(),
            "asset_decimals": self.config.asset_decimals,
            "network": self.config.network,
            "pay_to": self.config.pay_to.lower(),
            "payer_address": payer_address.lower(),
            "shipping_address_json": _canonical_json(shipping_address),
            "payment_requirements_json": _canonical_json(requirement),
            "state": "quoted",
            "expires_at": _iso(expires),
            "payment_payload_hash": None,
            "transaction_hash": None,
            "order_number": None,
            "checkout_receipt_json": None,
            "last_error": None,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        await asyncio.to_thread(self.ledger.create, values)
        return {
            "payment_id": payment_id,
            "quote_digest": quote_digest,
            "expires_at": _iso(expires),
            "payment_required": payment_required,
        }

    @staticmethod
    def decode_payment_header(value: str) -> tuple[dict[str, Any], str]:
        if not value or len(value.encode()) > _MAX_PAYMENT_HEADER_BYTES:
            raise ValueError("PAYMENT-SIGNATURE is missing or too large")
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
            payload = json.loads(decoded)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("PAYMENT-SIGNATURE must be base64-encoded JSON") from error
        if not isinstance(payload, dict) or payload.get("x402Version") != 2:
            raise ValueError("PAYMENT-SIGNATURE must contain an x402 v2 payload")
        return payload, "sha256:" + hashlib.sha256(decoded).hexdigest()

    @staticmethod
    def validate_payment_payload(
        payload: dict[str, Any], requirements: dict[str, Any], payment: dict[str, Any]
    ) -> None:
        """Reject a payload that does not authorize exactly the frozen quote.

        The facilitator repeats these checks cryptographically. Doing the structural
        comparison locally keeps a bad request away from that trust boundary and makes
        cart, payer, amount, asset, network, and recipient binding explicit here too.
        """
        if payload.get("accepted") != requirements:
            raise ValueError("payment payload does not accept the quoted requirements")
        scheme_payload = payload.get("payload")
        authorization = (
            scheme_payload.get("authorization") if isinstance(scheme_payload, dict) else None
        )
        signature = scheme_payload.get("signature") if isinstance(scheme_payload, dict) else None
        if not isinstance(authorization, dict):
            raise ValueError("payment payload has no EIP-3009 authorization")
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise ValueError("payment payload has no valid EIP-712 signature encoding")
        expected = {
            "from": payment["payer_address"].lower(),
            "to": payment["pay_to"].lower(),
            "value": payment["amount_atomic"],
        }
        actual = {
            "from": str(authorization.get("from", "")).lower(),
            "to": str(authorization.get("to", "")).lower(),
            "value": str(authorization.get("value", "")),
        }
        if actual != expected:
            raise ValueError("payment authorization does not match payer, recipient, or amount")
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(authorization.get("nonce", ""))):
            raise ValueError("payment authorization nonce must be 32 bytes")
        try:
            valid_after = int(authorization["validAfter"])
            valid_before = int(authorization["validBefore"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("payment authorization has invalid time bounds") from error
        quote_expiry = int(datetime.fromisoformat(payment["expires_at"]).timestamp())
        if valid_after < 0 or valid_before <= valid_after or valid_before > quote_expiry:
            raise ValueError("payment authorization exceeds the quote's validity window")

    async def get(self, payment_id: str, session_id: str) -> dict[str, Any]:
        if self.ledger is None:
            raise PaymentNotFound(payment_id)
        return await asyncio.to_thread(self.ledger.get, payment_id, session_id)

    async def get_for_operator(self, payment_id: str, store_id: str) -> dict[str, Any]:
        if self.ledger is None:
            raise PaymentNotFound(payment_id)
        payment = await asyncio.to_thread(self.ledger.get, payment_id)
        if payment["store_id"] != store_id:
            raise PaymentNotFound(payment_id)
        return payment

    async def list_for_operator(self, store_id: str) -> list[dict[str, Any]]:
        if self.ledger is None:
            return []
        return await asyncio.to_thread(self.ledger.list_for_store, store_id)

    async def transition(
        self,
        payment_id: str,
        session_id: str,
        expected: set[str],
        state: str,
        **fields: Any,
    ) -> dict[str, Any]:
        if self.ledger is None:
            raise PaymentNotFound(payment_id)
        return await asyncio.to_thread(
            self.ledger.transition, payment_id, session_id, expected, state, **fields
        )

    async def verify_and_settle(
        self,
        *,
        payment_id: str,
        session_id: str,
        quote_digest: str,
        payment_signature: str,
        current_cart_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if not _DIGEST.fullmatch(quote_digest):
            raise ValueError("invalid quote_digest")
        if self.ledger is not None:
            await asyncio.to_thread(self.ledger.recover_stale, payment_id=payment_id)
        payment = await self.get(payment_id, session_id)
        if payment["quote_digest"] != quote_digest:
            raise PaymentConflict("quote digest does not match")
        if payment["state"] in {"settled", "checkout_committing", "completed"}:
            return payment
        if payment["state"] == "reconciliation_required":
            raise PaymentConflict("payment requires operator reconciliation")
        if payment["state"] in {"verifying", "verified", "settling"}:
            raise PaymentConflict("payment is already being processed")
        if payment["state"] in {"failed", "expired"}:
            raise PaymentConflict(f"payment is {payment['state']}")
        if _utcnow() >= datetime.fromisoformat(payment["expires_at"]):
            await self.transition(payment_id, session_id, {"quoted"}, "expired")
            raise PaymentConflict("payment quote has expired")
        if digest(current_cart_snapshot) != payment["cart_digest"]:
            await self.transition(
                payment_id,
                session_id,
                {"quoted"},
                "failed",
                last_error="cart changed after quote",
            )
            raise PaymentConflict("cart changed after quote")

        payload, payload_hash = self.decode_payment_header(payment_signature)
        requirement = json.loads(payment["payment_requirements_json"])
        self.validate_payment_payload(payload, requirement, payment)
        payment = await self.transition(
            payment_id,
            session_id,
            {"quoted"},
            "verifying",
            payment_payload_hash=payload_hash,
            last_error=None,
        )
        assert self.facilitator is not None
        try:
            verified = await self.facilitator.verify(payload, requirement)
        except FacilitatorUncertain as error:
            await self.transition(
                payment_id, session_id, {"verifying"}, "quoted", last_error=str(error)
            )
            raise
        if (
            not verified.success
            or not verified.payer
            or verified.payer.lower() != payment["payer_address"]
            or (verified.network is not None and verified.network != payment["network"])
        ):
            reason = verified.reason or "payer or network did not match the quote"
            await self.transition(
                payment_id, session_id, {"verifying"}, "quoted", last_error=reason
            )
            raise PaymentConflict("facilitator rejected the payment")

        await self.transition(payment_id, session_id, {"verifying"}, "verified")
        await self.transition(payment_id, session_id, {"verified"}, "settling")
        try:
            settled = await self.facilitator.settle(payload, requirement)
        except FacilitatorUncertain as error:
            await self.transition(
                payment_id,
                session_id,
                {"settling"},
                "reconciliation_required",
                last_error=str(error),
            )
            raise
        if (
            not settled.success
            or not settled.transaction
            or not _TRANSACTION_HASH.fullmatch(settled.transaction)
            or not settled.payer
            or settled.payer.lower() != payment["payer_address"]
            or (settled.network is not None and settled.network != payment["network"])
        ):
            reason = settled.reason or "settlement response did not match the quote"
            # Once /settle has answered, even a negative or internally inconsistent
            # response is not enough evidence to initiate a fresh charge.  An operator
            # must compare the facilitator and chain before this payment can move.
            await self.transition(
                payment_id,
                session_id,
                {"settling"},
                "reconciliation_required",
                last_error=reason,
            )
            raise FacilitatorUncertain("facilitator settlement requires reconciliation")
        return await self.transition(
            payment_id,
            session_id,
            {"settling"},
            "settled",
            transaction_hash=settled.transaction.lower(),
            last_error=None,
        )

    async def preview_refund(
        self, payment_id: str, store_id: str, amount: Decimal
    ) -> dict[str, Any]:
        payment = await self.get_for_operator(payment_id, store_id)
        if payment["state"] != "completed":
            raise PaymentConflict("only a completed stablecoin payment can be refunded")
        normalized = str(amount.quantize(Decimal("0.01")))
        amount_atomic = self.atomic_amount(normalized, int(payment["asset_decimals"]))
        if int(amount_atomic) > int(payment["amount_atomic"]):
            raise PaymentConflict("refund exceeds the captured stablecoin amount")
        proposal = digest(
            {
                "version": "stablecoin-refund-proposal-v1",
                "store_id": store_id,
                "payment_id": payment_id,
                "quote_digest": payment["quote_digest"],
                "amount": normalized,
                "amount_atomic": amount_atomic,
            }
        )
        return {
            "payment_id": payment_id,
            "order_number": payment["order_number"],
            "captured_amount": payment["amount"],
            "refund_amount": normalized,
            "amount_atomic": amount_atomic,
            "asset": payment["asset_symbol"],
            "network": payment["network"],
            "proposal_digest": proposal,
        }

    async def refund(
        self,
        *,
        payment_id: str,
        store_id: str,
        amount: Decimal,
        proposal_digest: str,
        idempotency_key: str,
        operator: str,
    ) -> dict[str, Any]:
        if not self.refunds_available:
            raise PaymentConflict("stablecoin refund provider is not configured")
        preview = await self.preview_refund(payment_id, store_id, amount)
        if preview["proposal_digest"] != proposal_digest:
            raise PaymentConflict("stablecoin refund proposal digest changed")
        payment = await self.get_for_operator(payment_id, store_id)
        now = _iso(_utcnow())
        values = {
            "refund_id": f"rfnd_{uuid4().hex}",
            "payment_id": payment_id,
            "store_id": store_id,
            "amount": preview["refund_amount"],
            "amount_atomic": preview["amount_atomic"],
            "proposal_digest": proposal_digest,
            "idempotency_key": idempotency_key,
            "operator": operator,
            "state": "submitting",
            "transaction_hash": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        assert self.refund_ledger is not None
        refund, created = await asyncio.to_thread(self.refund_ledger.claim, values)
        if not created:
            if refund["state"] == "failed":
                raise PaymentConflict(refund["last_error"] or "stablecoin refund failed")
            return refund
        request = {
            "version": "stablecoin-refund-v1",
            "refundId": refund["refund_id"],
            "paymentId": payment_id,
            "originalTransaction": payment["transaction_hash"],
            "network": payment["network"],
            "asset": payment["asset_address"],
            "from": payment["pay_to"],
            "to": payment["payer_address"],
            "amount": refund["amount_atomic"],
        }
        assert self.refund_provider is not None
        try:
            result = await self.refund_provider.refund(request, idempotency_key)
        except RefundUncertain as error:
            uncertain = await asyncio.to_thread(
                self.refund_ledger.transition,
                refund["refund_id"],
                {"submitting"},
                "reconciliation_required",
                last_error=str(error),
            )
            raise RefundUncertain(str(error), uncertain) from error
        if (
            not result.success
            or not result.transaction
            or not _TRANSACTION_HASH.fullmatch(result.transaction)
            or (result.network is not None and result.network != payment["network"])
        ):
            reason = result.reason or "refund response did not match the payment"
            await asyncio.to_thread(
                self.refund_ledger.transition,
                refund["refund_id"],
                {"submitting"},
                "failed",
                last_error=reason,
            )
            raise PaymentConflict("stablecoin refund provider rejected the refund")
        try:
            return await asyncio.to_thread(
                self.refund_ledger.transition,
                refund["refund_id"],
                {"submitting"},
                "completed",
                transaction_hash=result.transaction.lower(),
                last_error=None,
            )
        except sqlite3.IntegrityError as error:
            detail = "refund provider returned a transaction already recorded elsewhere"
            uncertain = await asyncio.to_thread(
                self.refund_ledger.transition,
                refund["refund_id"],
                {"submitting"},
                "reconciliation_required",
                last_error=detail,
            )
            raise RefundUncertain(detail, uncertain) from error

    async def list_refunds(self, store_id: str) -> list[dict[str, Any]]:
        if self.refund_ledger is None:
            return []
        return await asyncio.to_thread(self.refund_ledger.list_for_store, store_id)

    async def reconcile_refund(
        self,
        refund_id: str,
        store_id: str,
        *,
        refunded: bool,
        transaction_hash: str | None,
        note: str,
    ) -> dict[str, Any]:
        if self.refund_ledger is None:
            raise RefundNotFound(refund_id)
        refund = await asyncio.to_thread(self.refund_ledger.get, refund_id)
        if refund["store_id"] != store_id:
            raise RefundNotFound(refund_id)
        if refund["state"] != "reconciliation_required":
            raise PaymentConflict(f"refund is {refund['state']}")
        if refunded:
            if transaction_hash is None or not _TRANSACTION_HASH.fullmatch(transaction_hash):
                raise ValueError("transaction_hash is required for a confirmed refund")
            try:
                return await asyncio.to_thread(
                    self.refund_ledger.transition,
                    refund_id,
                    {"reconciliation_required"},
                    "completed",
                    transaction_hash=transaction_hash.lower(),
                    last_error=None,
                    event_detail=note,
                )
            except sqlite3.IntegrityError as error:
                raise PaymentConflict("transaction already records another refund") from error
        if refund["transaction_hash"] is not None:
            raise PaymentConflict("a refund with a transaction cannot be marked not refunded")
        return await asyncio.to_thread(
            self.refund_ledger.transition,
            refund_id,
            {"reconciliation_required"},
            "failed",
            last_error="operator confirmed that the refund did not occur",
            event_detail=note,
        )


def public_payment(payment: dict[str, Any]) -> dict[str, Any]:
    """Return status and evidence without leaking addresses or facilitator payloads."""
    receipt = None
    if payment["checkout_receipt_json"]:
        try:
            receipt = json.loads(payment["checkout_receipt_json"])
        except (TypeError, ValueError):
            # A corrupt evidence copy must not make payment status itself unavailable.
            receipt = None
    return {
        "payment_id": payment["payment_id"],
        "quote_digest": payment["quote_digest"],
        "state": payment["state"],
        "amount": payment["amount"],
        "currency": payment["currency"],
        "asset": payment["asset_symbol"],
        "network": payment["network"],
        "expires_at": payment["expires_at"],
        "transaction_hash": payment["transaction_hash"],
        "order_number": payment["order_number"],
        "receipt": receipt,
        "last_error": payment["last_error"],
    }


def public_refund(refund: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": refund["refund_id"],
        "payment_id": refund["payment_id"],
        "amount": refund["amount"],
        "state": refund["state"],
        "transaction_hash": refund["transaction_hash"],
        "last_error": refund["last_error"],
        "created_at": refund["created_at"],
        "updated_at": refund["updated_at"],
    }


__all__ = [
    "Facilitator",
    "FacilitatorResult",
    "FacilitatorUncertain",
    "HttpStablecoinRefundProvider",
    "HttpX402Facilitator",
    "PaymentConflict",
    "PaymentNotFound",
    "RefundNotFound",
    "RefundProvider",
    "RefundResult",
    "RefundUncertain",
    "StablecoinConfig",
    "StablecoinPayments",
    "public_payment",
    "public_refund",
]
