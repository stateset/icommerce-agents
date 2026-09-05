"""Merchant chat, operator approval, reconciliation, and the change list.

``POST /merchant/changes/{id}/approve`` is the only place ``EngineMerchant.approve`` is
called; the operator comes from the session binding, never the request body."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from commerce_common.streaming import AgentEvent
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import ActorKind, ChangeStatus, MerchantSessionContext, StagedChange

from engine_backend import staging
from engine_backend.custom_objects import list_payloads
from engine_backend.reconciliation import assess as assess_reconciliation
from engine_backend.staging import STAGED_TYPE
from engine_backend.store import MerchantOperationBusy

from ..context import HostContext, _with_change_evidence
from ..schemas import (
    ApprovalRequest,
    ChatTurnRequest,
    ReconciliationRequest,
    ReconciliationStartRequest,
)
from ..sessions import ChatTurnBusy

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    merchant = ctx.merchant
    merchant_agent = ctx.merchant_agent
    merchant_sessions = ctx.merchant_sessions
    stale_apply_seconds = ctx.settings.stale_apply_seconds
    _bound_merchant_context = ctx.bound_merchant_context

    @router.post("/merchant/chat")
    async def merchant_chat(
        request: ChatTurnRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        session = _bound_merchant_context(x_session_id)
        claimed = await ctx.claim_chat(merchant_sessions, session.session_id)
        chat = claimed.session
        chat.state.approved_change_ids.update(merchant.approved_ids)

        async def enrich(event: AgentEvent) -> AgentEvent:
            return await _with_change_evidence(store, event)

        def reconcile_approvals() -> None:
            # The upstream gate and the engine adapter deliberately enforce approval
            # independently. The backend consumes its mark on an apply attempt; mirror
            # that consumption into session state after every turn so a failed attempt
            # cannot retain a stale upstream approval.
            chat.state.approved_change_ids.intersection_update(merchant.approved_ids)

        return ctx.stream_chat_turn(
            "merchant",
            merchant_sessions,
            claimed,
            lambda: merchant_agent.stream_turn(chat.messages, session, chat.state),
            message=request.message,
            request_id=http_request.state.request_id,
            enrich=enrich,
            after_turn=reconcile_approvals,
        )

    @router.post("/merchant/changes/{change_id}/approve")
    async def merchant_approve_change(
        change_id: str,
        request: ApprovalRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """The operator's approval, and the only place it happens. The operator comes
        from the session binding, never from the request body."""
        session = _bound_merchant_context(x_session_id)
        try:
            claimed = await merchant_sessions.claim(session.session_id)
        except ChatTurnBusy as error:
            raise HTTPException(
                status_code=409, detail="another chat turn is in progress"
            ) from error
        chat = claimed.session
        # Serialize approval against this session's streaming turns. Otherwise a turn's
        # final reconciliation could erase an approval issued while that turn was still
        # in flight.
        try:
            record = await staging.load_record(store, change_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
            change = StagedChange.model_validate(record)
            if change.status is not ChangeStatus.STAGED:
                # An already-applied or discarded change has nothing left to approve;
                # accepting one would put a live change id into `approved_ids`.
                raise HTTPException(
                    status_code=409,
                    detail=f"change {change_id} is {change.status.value}, not staged",
                )
            digest = staging.proposal_digest(change, record.get("payload"))
            if digest != record.get("proposal_digest") or digest != request.proposal_digest:
                raise HTTPException(status_code=409, detail="proposal digest changed")
            # Claude Commerce's executor checks this session-owned mark before it calls
            # the backend. The backend checks a separate operator-bound mark again at
            # the mutation boundary; both are required for the HTTP path.
            try:
                merchant.approve(change_id, session.operator)
            except ChangeNotApplicable as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            chat.state.approved_change_ids.add(change_id)
        finally:
            await merchant_sessions.finish(claimed)
        approval = store.approval_record(change_id)
        if approval is None:
            raise HTTPException(status_code=500, detail="approval record missing after approval")
        return {
            "change_id": change_id,
            "approved_by": session.operator,
            "proposal_digest": approval["proposal_digest"],
        }

    @router.get("/merchant/changes/{change_id}/reconciliation")
    async def merchant_reconciliation_read(
        change_id: str, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _bound_merchant_context(x_session_id)
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        control = store.approval_record(change_id)
        if control is None or control["state"] != "reconciliation_required":
            raise HTTPException(
                status_code=409,
                detail=f"change {change_id} does not require reconciliation",
            )
        change = StagedChange.model_validate(record)
        assessment = await assess_reconciliation(store, change, record.get("payload"))
        return {
            "change": change.model_dump(mode="json"),
            "proposal_digest": record["proposal_digest"],
            "control": control,
            "assessment": assessment.model_dump(mode="json"),
            "events": store.approval_events(change_id),
        }

    @router.post("/merchant/changes/{change_id}/reconciliation/start")
    async def merchant_reconciliation_start(
        change_id: str,
        request: ReconciliationStartRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        change = StagedChange.model_validate(record)
        digest = staging.proposal_digest(change, record.get("payload"))
        if digest != record.get("proposal_digest") or digest != request.proposal_digest:
            raise HTTPException(status_code=409, detail="proposal digest changed")
        try:
            store.recover_stale_approval(
                change_id,
                session.operator,
                digest,
                stale_before=datetime.now(UTC) - timedelta(seconds=stale_apply_seconds),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "change_id": change_id,
            "state": "reconciliation_required",
            "assessment": (
                await assess_reconciliation(store, change, record.get("payload"))
            ).model_dump(mode="json"),
        }

    @router.post("/merchant/changes/{change_id}/reconciliation")
    async def merchant_reconciliation_resolve(
        change_id: str,
        request: ReconciliationRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        try:
            with store.merchant_operation(change_id):
                return await _resolve_reconciliation(change_id, request, session)
        except MerchantOperationBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    async def _resolve_reconciliation(
        change_id: str, request: ReconciliationRequest, session: MerchantSessionContext
    ) -> dict[str, Any]:
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        change = StagedChange.model_validate(record)
        digest = staging.proposal_digest(change, record.get("payload"))
        if digest != record.get("proposal_digest") or digest != request.proposal_digest:
            raise HTTPException(status_code=409, detail="proposal digest changed")
        control = store.approval_record(change_id)
        if control is None or control["state"] != "reconciliation_required":
            raise HTTPException(
                status_code=409,
                detail=f"change {change_id} does not require reconciliation",
            )
        try:
            store.claim_reconciliation(
                change_id,
                session.operator,
                digest,
                request.resolution,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            # Assess only after winning the durable reconciliation claim. This avoids
            # persisting a conclusion another operator computed before losing the race.
            assessment = await assess_reconciliation(store, change, record.get("payload"))
            if request.resolution == "confirmed_applied":
                if assessment.outcome != "applied":
                    raise HTTPException(
                        status_code=409,
                        detail="live state does not fully match the approved proposal",
                    )
                resolved = change.model_copy(
                    update={
                        "status": ChangeStatus.APPLIED,
                        "applied_at": datetime.now(UTC),
                        "applied_by": session.operator,
                        "guardrail_notes": [
                            *change.guardrail_notes,
                            "operator reconciled live state as fully applied",
                        ],
                    }
                )
            else:
                resolved = change.model_copy(
                    update={
                        "status": ChangeStatus.DISCARDED,
                        "discarded_at": datetime.now(UTC),
                        "discarded_by": session.operator,
                        "discarded_by_kind": ActorKind.OPERATOR,
                        "guardrail_notes": [
                            *change.guardrail_notes,
                            "operator accepted current live state after ambiguous apply",
                        ],
                    }
                )
            await staging.save(store, resolved)
        except Exception as error:
            store.abort_reconciliation(
                change_id,
                session.operator,
                digest,
                request.resolution,
                str(error),
            )
            raise
        store.finish_reconciliation(
            change_id,
            session.operator,
            digest,
            request.resolution,
        )
        return {
            "change_id": change_id,
            "resolution": request.resolution,
            "assessment": assessment.model_dump(mode="json"),
        }

    @router.get("/merchant/changes")
    async def merchant_changes_read(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Pending and applied changes, each carrying its structured evidence -- a
        sealed kernel receipt id or an activity-log id, read straight from the
        persisted record, never inferred from ``guardrail_notes`` prose. This is a
        read: it introduces no write and calls neither ``approve`` nor ``apply_change``."""
        _bound_merchant_context(x_session_id)
        records = await store.call(lambda c: list_payloads(c, STAGED_TYPE))
        approval_records = store.approval_records(
            [record["change_id"] for record in records if record.get("change_id")]
        )
        approval_events = store.approval_event_records(
            [record["change_id"] for record in records if record.get("change_id")]
        )
        changes = []
        for record in records:
            change = StagedChange.model_validate(record)
            control = approval_records.get(change.change_id)
            operationally_active = control and control["state"] in (
                "applying",
                "reconciliation_required",
                "reconciling",
            )
            if (
                change.status not in (ChangeStatus.STAGED, ChangeStatus.APPLIED)
                and not operationally_active
            ):
                continue
            item = change.model_dump(mode="json")
            item["evidence"] = record.get("evidence") or []
            item["proposal_digest"] = record.get("proposal_digest")
            item["apply_control"] = control
            control = item["apply_control"]
            item["recovery_available_at"] = None
            if control and control["state"] in ("applying", "reconciling"):
                recovery_started_at = (
                    control.get("claimed_at")
                    if control["state"] == "applying"
                    else control.get("resolved_at")
                )
                if recovery_started_at:
                    item["recovery_available_at"] = (
                        datetime.fromisoformat(recovery_started_at)
                        + timedelta(seconds=stale_apply_seconds)
                    ).isoformat()
            item["approval_events"] = approval_events.get(change.change_id, [])
            changes.append(item)
        changes.sort(key=lambda item: item["created_at"])
        return {"changes": changes}

    return router
