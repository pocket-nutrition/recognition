import asyncio
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.schemas import TERMINAL, CaptureResult, Status, TicketOut
from app.simulator import static_result
from app.vault import PAYLOAD_VERSION, DropTarget

log = logging.getLogger(__name__)


@dataclass
class Ticket:
    id: str
    created_at: float
    expires_at: float
    # Authenticates reads and deletes of this ticket. In memory only, like the ticket itself.
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    status: Status = Status.queued
    updated_at: float = 0.0
    result: CaptureResult | None = None
    error: str | None = None
    delivery: str = "none"
    drop: DropTarget | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    # Worker pipeline: the audio waits here (memory only) until a worker finishes the job.
    audio: bytes | None = field(default=None, repr=False)
    audio_type: str = "application/octet-stream"
    lease_id: str | None = field(default=None, repr=False)
    lease_expires_at: float = 0.0
    attempts: int = 0

    def to_out(self) -> TicketOut:
        return TicketOut(
            id=self.id,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
            result=self.result,
            error=self.error,
            delivery=self.delivery,
        )


class Expired(Exception):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    """Constant-time comparison that also copes with non-ASCII input."""
    return hmac.compare_digest(a.encode(), b.encode())


class LeaseLost(Exception):
    """The lease is unknown, expired, or its job is already finished or deleted."""


class TicketStore:
    """In-memory ticket registry. Single replica only: tickets die with the process."""

    def __init__(
        self,
        ttl_s: int,
        tombstone_ttl_s: int,
        deliver: Callable[[DropTarget, dict], Awaitable[bool]] | None = None,
        lease_s: float = 120,
        max_attempts: int = 3,
    ):
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._deliver = deliver
        self._ttl = ttl_s
        self._tombstone_ttl = tombstone_ttl_s
        self._tickets: dict[str, Ticket] = {}
        # id -> (when to forget it, digest of its secret): the secret is kept hashed so an expired ticket can
        # still tell its owner "expired" without telling anyone else anything.
        self._tombstones: dict[str, tuple[float, str]] = {}
        # digest of the drop token that started a ticket -> ticket id: one drop token, one dictation.
        self._by_token: dict[str, str] = {}
        self._changed = asyncio.Condition()

    def create(self) -> Ticket:
        now = time.time()
        ticket = Ticket(
            id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            expires_at=now + self._ttl,
        )
        self._tickets[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        """Return the live ticket, None if unknown, raise Expired if past its TTL."""
        self._purge_if_expired(ticket_id)
        if ticket_id in self._tombstones:
            raise Expired(ticket_id)
        return self._tickets.get(ticket_id)

    def bind_token(self, token: str, ticket_id: str) -> None:
        self._by_token[digest(token)] = ticket_id

    def token_binding(self, token: str) -> str | None:
        """The ticket this drop token already started, live or not."""
        return self._by_token.get(digest(token))

    def tombstone_matches(self, ticket_id: str, presented: str | None) -> bool:
        entry = self._tombstones.get(ticket_id)
        return entry is not None and presented is not None and same(digest(presented), entry[1])

    def delete(self, ticket_id: str) -> bool:
        ticket = self._tickets.pop(ticket_id, None)
        if ticket and ticket.task:
            ticket.task.cancel()
        return ticket is not None

    async def _set_status(self, ticket: Ticket, status: Status, **fields) -> None:
        async with self._changed:
            ticket.status = status
            ticket.updated_at = time.time()
            for key, value in fields.items():
                setattr(ticket, key, value)
            self._changed.notify_all()

    async def wait_for_change(self, ticket: Ticket, known: Status, timeout: float) -> None:
        """Block until the ticket leaves `known`, is terminal, or `timeout` elapses."""
        await self._wait(lambda: ticket.status != known or ticket.status in TERMINAL, timeout)

    async def wait_for_terminal(self, ticket: Ticket, timeout: float) -> None:
        await self._wait(lambda: ticket.status in TERMINAL, timeout)

    async def _wait(self, predicate, timeout: float) -> None:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(predicate), timeout)
            except asyncio.TimeoutError:
                pass

    def queue_load(self) -> tuple[int, int]:
        """(jobs, bytes) of audio currently held for a worker, claimed jobs included."""
        held = [t.audio for t in self._tickets.values() if t.audio is not None]
        return len(held), sum(len(a) for a in held)

    async def enqueue(self, ticket: Ticket, audio: bytes, content_type: str) -> None:
        async with self._changed:
            ticket.audio = audio
            ticket.audio_type = content_type
            self._changed.notify_all()

    def _reap_leases(self) -> None:
        """Requeue jobs whose worker went silent; fail them after too many attempts.
        Synchronous, so callers must hold `_changed`."""
        now = time.time()
        for ticket in self._tickets.values():
            if ticket.lease_id is None or ticket.lease_expires_at > now or ticket.status in TERMINAL:
                continue
            ticket.lease_id = None
            ticket.updated_at = now
            if ticket.attempts >= self._max_attempts:
                ticket.status = Status.failed
                ticket.error = "worker did not finish the job"
                ticket.audio = None
            else:
                ticket.status = Status.queued
            self._changed.notify_all()

    def _next_claimable(self) -> Ticket | None:
        self._reap_leases()
        for ticket in self._tickets.values():
            if ticket.audio is not None and ticket.status == Status.queued and ticket.lease_id is None:
                return ticket
        return None

    async def claim(self, wait_s: float) -> Ticket | None:
        """Give the oldest waiting job to a worker under a fresh lease, long-polling up to `wait_s`."""
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self._next_claimable() is not None), wait_s)
            except asyncio.TimeoutError:
                pass
            ticket = self._next_claimable()
            if ticket is None:
                return None
            ticket.attempts += 1
            ticket.lease_id = uuid.uuid4().hex
            ticket.lease_expires_at = time.time() + self._lease_s
            return ticket

    async def sweep_leases(self) -> None:
        async with self._changed:
            self._reap_leases()

    def _leased(self, lease_id: str) -> Ticket:
        for ticket in self._tickets.values():
            if ticket.lease_id == lease_id:
                if ticket.lease_expires_at <= time.time():
                    break
                return ticket
        raise LeaseLost(lease_id)

    def leased(self, lease_id: str) -> Ticket:
        return self._leased(lease_id)

    async def progress(self, lease_id: str, status: Status) -> None:
        ticket = self._leased(lease_id)
        await self._set_status(ticket, status, lease_expires_at=time.time() + self._lease_s)

    async def finish(self, lease_id: str, delivery: str, result: CaptureResult | None) -> None:
        ticket = self._leased(lease_id)
        await self._set_status(
            ticket, Status.done, result=result, delivery=delivery, audio=None, lease_id=None
        )

    async def fail(self, lease_id: str, error: str) -> None:
        ticket = self._leased(lease_id)
        await self._set_status(ticket, Status.failed, error=error, audio=None, lease_id=None)

    def start(self, ticket: Ticket, delay_s: float, outcome: Status) -> None:
        ticket.task = asyncio.create_task(self._run(ticket, delay_s, outcome))

    async def _run(self, ticket: Ticket, delay_s: float, outcome: Status) -> None:
        step = delay_s / 3
        await asyncio.sleep(step)
        await self._set_status(ticket, Status.transcribing)
        await asyncio.sleep(step)
        await self._set_status(ticket, Status.extracting)
        await asyncio.sleep(step)
        failed = outcome == Status.failed
        result = None if failed else static_result()
        error = "simulated failure" if failed else None
        delivery = await self._deposit(ticket, outcome, result, error)
        await self._set_status(
            ticket,
            outcome,
            error=error,
            # What was sealed into the vault is not kept here.
            result=None if delivery == "delivered" else result,
            delivery=delivery,
        )

    async def _deposit(self, ticket: Ticket, status: Status, result, error: str | None) -> str:
        """Seal the outcome into the user's vault. The ticket turns terminal only after this, so
        `done` with `delivery == "delivered"` means the data is already in the cloud."""
        if ticket.drop is None or self._deliver is None:
            return "none"
        payload = {
            "v": PAYLOAD_VERSION,
            "record_id": ticket.drop.record_id,
            "ticket_id": ticket.id,
            "status": status.value,
            "result": result.model_dump() if result else None,
            "error": error,
            "completed_at": time.time(),
        }
        try:
            return "delivered" if await self._deliver(ticket.drop, payload) else "failed"
        except Exception:
            # Whatever went wrong while sealing or sending, the ticket must still finish: the result
            # stays on it as the fallback instead of the task dying and the ticket hanging until expiry.
            log.exception("vault delivery crashed for ticket %s", ticket.id)
            return "failed"

    def sweep(self) -> None:
        for ticket_id in list(self._tickets):
            self._purge_if_expired(ticket_id)
        now = time.time()
        for ticket_id, (purge_at, _) in list(self._tombstones.items()):
            if purge_at <= now:
                del self._tombstones[ticket_id]
        for token_digest, ticket_id in list(self._by_token.items()):
            if ticket_id not in self._tickets and ticket_id not in self._tombstones:
                del self._by_token[token_digest]

    def _purge_if_expired(self, ticket_id: str) -> None:
        ticket = self._tickets.get(ticket_id)
        if ticket and ticket.expires_at <= time.time():
            self.delete(ticket_id)
            self._tombstones[ticket_id] = (time.time() + self._tombstone_ttl, digest(ticket.secret))

    def cancel_all(self) -> None:
        for ticket in self._tickets.values():
            if ticket.task:
                ticket.task.cancel()
