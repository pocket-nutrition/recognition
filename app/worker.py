"""Routes for the remote worker that pulls jobs, runs speech-to-text + extraction, and reports back.

The worker authenticates with its own key (never the phone key). It only ever calls out to this
service: claim a job (long poll), download its audio, report progress, then complete or fail it.
A job is held under a lease; a worker that goes silent loses it and the job is requeued.
"""

import base64
import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.config import Settings
from app.schemas import CaptureResult, Status
from app.store import LeaseLost, Ticket, TicketStore

router = APIRouter(prefix="/worker")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> TicketStore:
    return request.app.state.store


def require_worker_key(
    settings: Annotated[Settings, Depends(_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {settings.worker_key}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing worker key")


Auth = Depends(require_worker_key)


class DropOut(BaseModel):
    url: str
    token: str
    record_id: str
    inbox_public_key: str


class JobOut(BaseModel):
    lease_id: str
    ticket_id: str
    lease_expires_at: float
    audio_content_type: str
    drop: DropOut | None = None


class ProgressIn(BaseModel):
    status: Literal["transcribing", "extracting"]


class CompleteIn(BaseModel):
    delivery: Literal["none", "delivered", "failed"]
    result: CaptureResult | None = None


class FailIn(BaseModel):
    error: str = Field(max_length=500)


def _job(ticket: Ticket) -> JobOut:
    drop = ticket.drop
    return JobOut(
        lease_id=ticket.lease_id,
        ticket_id=ticket.id,
        lease_expires_at=ticket.lease_expires_at,
        audio_content_type=ticket.audio_type,
        drop=drop
        and DropOut(
            url=drop.url,
            token=drop.token,
            record_id=drop.record_id,
            inbox_public_key=base64.b64encode(drop.inbox_public_key).decode(),
        ),
    )


def _leased(store: TicketStore, lease_id: str) -> Ticket:
    try:
        return store.leased(lease_id)
    except LeaseLost:
        raise HTTPException(status_code=409, detail="lease expired or job no longer active")


@router.post("/claim", response_model=JobOut, responses={204: {"description": "no job waiting"}}, dependencies=[Auth])
async def claim(
    settings: Annotated[Settings, Depends(_settings)],
    store: Annotated[TicketStore, Depends(_store)],
    wait: Annotated[float, Query(ge=0)] = 0,
):
    """Take the oldest waiting job. Long-polls up to `wait` seconds; 204 when nothing is waiting."""
    if wait > settings.max_wait_s:
        raise HTTPException(status_code=422, detail=f"wait must be <= {settings.max_wait_s}")
    ticket = await store.claim(wait)
    if ticket is None:
        return Response(status_code=204)
    return _job(ticket)


@router.get("/leases/{lease_id}/audio", dependencies=[Auth])
async def audio(lease_id: str, store: Annotated[TicketStore, Depends(_store)]):
    ticket = _leased(store, lease_id)
    return Response(content=ticket.audio, media_type=ticket.audio_type)


@router.post("/leases/{lease_id}/progress", status_code=204, dependencies=[Auth])
async def progress(lease_id: str, body: ProgressIn, store: Annotated[TicketStore, Depends(_store)]):
    """Report a pipeline stage. Also extends the lease, so it doubles as a heartbeat."""
    _leased(store, lease_id)
    await store.progress(lease_id, Status(body.status))
    return Response(status_code=204)


@router.post("/leases/{lease_id}/complete", status_code=204, dependencies=[Auth])
async def complete(lease_id: str, body: CompleteIn, store: Annotated[TicketStore, Depends(_store)]):
    """Finish a job. If the worker sealed the result into the vault (`delivered`) it must not send it
    here; otherwise the result is kept on the ticket for the phone to fetch."""
    ticket = _leased(store, lease_id)
    if ticket.drop is None and body.delivery != "none":
        raise HTTPException(status_code=422, detail="no vault drop was requested for this job")
    if ticket.drop is not None and body.delivery == "none":
        raise HTTPException(status_code=422, detail="a vault drop was requested: report delivered or failed")
    if body.delivery == "delivered" and body.result is not None:
        raise HTTPException(status_code=422, detail="a delivered result must not be sent here")
    if body.delivery != "delivered" and body.result is None:
        raise HTTPException(status_code=422, detail="result required unless it was delivered")
    await store.finish(lease_id, body.delivery, body.result)
    return Response(status_code=204)


@router.post("/leases/{lease_id}/fail", status_code=204, dependencies=[Auth])
async def fail(lease_id: str, body: FailIn, store: Annotated[TicketStore, Depends(_store)]):
    _leased(store, lease_id)
    await store.fail(lease_id, body.error)
    return Response(status_code=204)
