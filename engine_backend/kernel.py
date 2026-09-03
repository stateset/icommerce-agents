"""The governed write seam.

A governed command is an envelope (contract_version, command_id, idempotency_key,
command_type, principal, store_id, optional approval) executed by
Commerce.execute_kernel_command against host-owned policy. The policy and the principal
are files on disk, never model input; the return is a sealed receipt whose error_code is a
stable code, never prose to parse.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from stateset_embedded import Commerce

from engine_backend.store import EngineStore

CONTRACT_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Receipt(BaseModel):
    receipt_id: str = ""
    command_id: str = ""
    command_type: str = ""
    status: str = ""
    idempotency_key: str = ""
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    sealed: bool = True
    """True for a receipt parsed from the engine's JSON; False for one this module
    synthesizes (e.g. a malformed envelope the binding rejected before sealing a receipt).
    A downstream consumer recording receipt ids as evidence must check this rather than
    inferring it from empty id fields."""

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def approval_evidence(approval_id: str, approved_by: str, scope: str, store_id: str) -> dict:
    """Build a partial approval-evidence template.

    `tenant_id` and `idempotency_key` are left `None` here — they are bound to the
    concrete call by `KernelClient.execute`, which fills `tenant_id` from the host
    principal and sets `idempotency_key` to the call's own idempotency key. The dict
    this function returns is therefore incomplete evidence: do not log or store it as an
    audit record before `execute` has bound it.
    """
    return {
        "approval_id": approval_id,
        "approved_by": approved_by,
        "scope": scope,
        "tenant_id": None,
        "store_id": store_id,
        "idempotency_key": None,
        "approved_at": _now_iso(),
        "expires_at": None,
    }


class KernelClient:
    """Executes governed commands against host-owned policy and principal files.

    Policy and principal are loaded once from disk by the host; nothing in `execute`'s
    signature lets a caller supply or override them.
    """

    def __init__(self, store: EngineStore, policy_path: Path, principal_path: Path) -> None:
        self.store = store
        self.policy = json.loads(Path(policy_path).read_text())
        self.principal = json.loads(Path(principal_path).read_text())

    async def execute(
        self,
        command_type: str,
        payload: dict,
        idempotency_key: str,
        approval: dict | None = None,
    ) -> Receipt:
        bound_approval = None
        if approval is not None:
            bound_approval = dict(approval)
            if bound_approval.get("tenant_id") is None:
                bound_approval["tenant_id"] = self.principal.get("tenant_id")
            bound_approval["idempotency_key"] = idempotency_key
        envelope = {
            "contract_version": CONTRACT_VERSION,
            "command_id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
            "command_type": command_type,
            "principal": {
                "id": self.principal.get("id"),
                "kind": self.principal.get("kind", "agent"),
                "tenant_id": self.principal.get("tenant_id"),
                "delegated_by": self.principal.get("delegated_by"),
                "capabilities": list(self.principal.get("capabilities", [])),
            },
            "store_id": self.store.store_id,
            "correlation_id": None,
            "causation_id": None,
            "expected_version": None,
            "policy_version": self.policy.get("version"),
            "approval": bound_approval,
            "authority": None,
            "deadline": None,
            "trace_id": None,
            "mode": "apply",
            "payload": payload,
            "issued_at": _now_iso(),
        }
        command_json = json.dumps(envelope)
        policy_json = json.dumps(self.policy)

        def body(c: Commerce) -> str:
            return c.execute_kernel_command(command_json, policy_json)

        try:
            raw = await self.store.write("kernel", body)
        except ValueError as error:
            # The binding raises ValueError (PyValueError) for a malformed envelope or
            # policy JSON, or an unsupported command_type — rejected before any receipt
            # is sealed. Any other exception (a programming defect, a lock/runtime error,
            # cancellation) is not a refusal and must propagate rather than masquerade as
            # a receipt.
            return Receipt(
                command_type=command_type,
                idempotency_key=idempotency_key,
                status="failed",
                error_code="kernel.rejected",
                error_message=str(error),
                sealed=False,
            )
        return Receipt.model_validate(json.loads(raw))
