"""Routes for the remote worker that pulls jobs, runs speech-to-text + extraction, and reports back.

The worker authenticates with its own key (never the phone key). It only ever calls out to this
service: claim a job (long poll), download its audio, report progress, then complete or fail it.
A job is held under a lease; a worker that goes silent loses it and the job is requeued.

There is no way to send a result here: the worker seals it into the user's vault, then reports
`delivered`. A deposit that fails is reported as a failure.
"""

import base64
import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.schemas import Status
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
    drop: DropOut


class ProgressIn(BaseModel):
    status: Literal["transcribing", "extracting"]


class CompleteIn(BaseModel):
    # Anything else, a `result` above all, is refused: results never come back to this service.
    model_config = ConfigDict(extra="forbid")

    delivery: Literal["delivered"]


class FailIn(BaseModel):
    error: str = Field(max_length=500)


def _job(ticket: Ticket) -> JobOut:
    drop = ticket.drop
    return JobOut(
        lease_id=ticket.lease_id,
        ticket_id=ticket.id,
        lease_expires_at=ticket.lease_expires_at,
        audio_content_type=ticket.audio_type,
        drop=DropOut(
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
    """Finish a job whose sealed result is already in the user's vault."""
    _leased(store, lease_id)
    await store.finish(lease_id)
    return Response(status_code=204)


@router.post("/leases/{lease_id}/fail", status_code=204, dependencies=[Auth])
async def fail(lease_id: str, body: FailIn, store: Annotated[TicketStore, Depends(_store)]):
    _leased(store, lease_id)
    await store.fail(lease_id, body.error)
    return Response(status_code=204)
