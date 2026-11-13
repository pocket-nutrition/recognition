from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.config import Settings
from app.schemas import TERMINAL, Status, TicketCreated, TicketOut
from app.store import Expired, Ticket, TicketStore, same
from app.vault import (
    DropTarget,
    InvalidDropTarget,
    VaultDeliverer,
    VaultUnreachable,
    check_vault_url,
    parse_public_key,
)

router = APIRouter()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> TicketStore:
    return request.app.state.store


def get_deliverer(request: Request) -> VaultDeliverer:
    return request.app.state.deliverer


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]
    return None


def _is_api_key(settings: Settings, presented: str | None) -> bool:
    return bool(settings.api_key and presented and same(presented, settings.api_key))


def _read_ticket(store: TicketStore, settings: Settings, ticket_id: str, authorization: str | None) -> Ticket:
    """The ticket, for someone allowed to see it: the shared key (self-hosted) or this ticket's own secret.

    Unknown ids are 404 whatever is presented, and so is an expired ticket for anyone but its owner, so
    neither leaks which ids exist. A live ticket seen with the wrong credential is 401.
    """
    presented = _bearer(authorization)
    admin = _is_api_key(settings, presented)
    try:
        ticket = store.get(ticket_id)
    except Expired:
        if admin or store.tombstone_matches(ticket_id, presented):
            raise HTTPException(status_code=410, detail="ticket expired")
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if not (admin or (presented is not None and same(presented, ticket.secret))):
        raise HTTPException(status_code=401, detail="invalid or missing credentials")
    return ticket


async def _parse_drop(settings: Settings, url, token, record_id, public_key) -> DropTarget | None:
    fields = (url, token, record_id, public_key)
    if not any(fields):
        return None
    if not all(fields):
        raise HTTPException(
            status_code=422,
            detail="vault_url, vault_token, vault_record_id and inbox_public_key go together",
        )
    try:
        return DropTarget(
            url=await check_vault_url(url, settings),
            token=token,
            inbox_public_key=parse_public_key(public_key),
            record_id=record_id,
        )
    except InvalidDropTarget as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# Meal photos ride the same ticket flow as dictations: the worker branches on the content type.
PHOTO_TYPES = frozenset({"image/jpeg", "image/png"})


def _accepted_media(content_type: str | None) -> bool:
    media = (content_type or "").split(";")[0].strip().lower()
    return media.startswith("audio/") or media in PHOTO_TYPES


@router.post("/captures", status_code=202, response_model=TicketCreated)
async def create_capture(
    audio: UploadFile,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[TicketStore, Depends(get_store)],
    deliverer: Annotated[VaultDeliverer, Depends(get_deliverer)],
    authorization: Annotated[str | None, Header()] = None,
    mode: Annotated[str, Form(pattern="^(sync|async)$")] = "async",
    vault_url: Annotated[str | None, Form()] = None,
    vault_token: Annotated[str | None, Form()] = None,
    vault_record_id: Annotated[str | None, Form(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
    inbox_public_key: Annotated[str | None, Form()] = None,
    simulate_delay_s: Annotated[float | None, Form(ge=0)] = None,
    simulate_outcome: Annotated[Status, Form()] = Status.done,
):
    """Upload an audio clip (or a meal photo, JPEG/PNG, same field) and get a ticket back.

    `mode=async` (default) returns 202 immediately. `mode=sync` waits up to
    `sync_timeout_s` for the result and returns 200, or 202 if still running.
    `simulate_*` fields only exist for the POC (static results).

    Optional vault delivery: pass all of `vault_url`, `vault_token` (a write-only drop token),
    `vault_record_id` and `inbox_public_key`. The result is then sealed to that key and deposited
    in the vault, and the ticket reports `delivery` instead of keeping the result.
    """
    drop = await _parse_drop(settings, vault_url, vault_token, vault_record_id, inbox_public_key)
    token = drop.token if drop else None
    if not _is_api_key(settings, _bearer(authorization)):
        # No shared key: the dictation is authenticated by its drop token, which the vault vouches for.
        if drop is None:
            raise HTTPException(status_code=401, detail="invalid or missing credentials")
        if (urlsplit(drop.url).hostname or "").lower() not in settings.trusted_vault_hosts:
            raise HTTPException(status_code=401, detail="vault not accepted by this server")
        started = store.token_binding(drop.token)
        if started is not None:
            # The same drop token again: the app retrying an upload whose answer it never saw.
            try:
                earlier = store.get(started)
            except Expired:
                earlier = None
            if earlier is None:
                raise HTTPException(status_code=409, detail="drop token already used")
            return _created(earlier)
        try:
            alive = await deliverer.verify_drop_token(drop)
        except VaultUnreachable:
            raise HTTPException(
                status_code=503, detail="vault unreachable, retry later", headers={"Retry-After": "10"}
            )
        if not alive:
            raise HTTPException(status_code=401, detail="invalid or missing credentials")
    else:
        token = None  # shared-key callers are not bound to a drop token

    if not _accepted_media(audio.content_type):
        raise HTTPException(status_code=415, detail="audio/* or image/jpeg|png content required")
    data = await audio.read(settings.max_audio_bytes + 1)
    if len(data) > settings.max_audio_bytes:
        raise HTTPException(status_code=413, detail="audio too large")
    if simulate_outcome not in TERMINAL:
        raise HTTPException(status_code=422, detail="simulate_outcome must be done or failed")

    delay = min(
        settings.default_delay_s if simulate_delay_s is None else simulate_delay_s,
        settings.max_delay_s,
    )
    if settings.pipeline == "worker":
        queued, queued_bytes = store.queue_load()
        if queued >= settings.max_queued or queued_bytes + len(data) > settings.max_queued_bytes:
            raise HTTPException(
                status_code=503, detail="queue is full, retry later", headers={"Retry-After": "60"}
            )
    ticket = store.create()
    ticket.drop = drop
    if token is not None:
        store.bind_token(token, ticket.id)
    if settings.pipeline == "worker":
        await store.enqueue(ticket, data, audio.content_type)
    else:
        store.start(ticket, delay, simulate_outcome)
    response.headers["Location"] = f"/captures/{ticket.id}"

    if mode == "sync":
        await store.wait_for_terminal(ticket, settings.sync_timeout_s)
        if ticket.status in TERMINAL:
            response.status_code = 200
    return _created(ticket)


def _created(ticket: Ticket) -> TicketCreated:
    return TicketCreated(**ticket.to_out().model_dump(), secret=ticket.secret)


@router.get("/captures/{ticket_id}", response_model=TicketOut)
async def get_capture(
    ticket_id: str,
    request: Request,
    store: Annotated[TicketStore, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
    wait: Annotated[float, Query(ge=0)] = 0,
    known_status: Status | None = None,
):
    """Read a ticket. With `wait` > 0 this long-polls: it blocks until the status
    differs from `known_status` (default: the status at request time), the ticket
    is terminal, or `wait` seconds pass."""
    settings: Settings = request.app.state.settings
    if wait > settings.max_wait_s:
        raise HTTPException(status_code=422, detail=f"wait must be <= {settings.max_wait_s}")
    ticket = _read_ticket(store, settings, ticket_id, authorization)
    if wait > 0:
        await store.wait_for_change(ticket, known_status or ticket.status, wait)
        ticket = _read_ticket(store, settings, ticket_id, authorization)
    return ticket.to_out()


@router.delete("/captures/{ticket_id}", status_code=204)
async def delete_capture(
    ticket_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[TicketStore, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
):
    _read_ticket(store, settings, ticket_id, authorization)
    store.delete(ticket_id)
    return Response(status_code=204)
