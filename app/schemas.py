from enum import Enum

from pydantic import BaseModel


class Status(str, Enum):
    queued = "queued"
    transcribing = "transcribing"
    extracting = "extracting"
    done = "done"
    failed = "failed"


TERMINAL = {Status.done, Status.failed}


class TicketOut(BaseModel):
    """A ticket carries a status, never a result: the worker seals the result into the user's vault."""

    id: str
    status: Status
    created_at: float
    updated_at: float
    expires_at: float
    error: str | None = None
    # "delivered" once `done`: the result is in the vault, sealed for the phone. "none" before that.
    delivery: str = "none"


class TicketCreated(TicketOut):
    """The answer to `POST /captures`: the only time the ticket's secret is shown. It authenticates every
    later read or delete of that one ticket, so the phone needs no shared key."""

    secret: str
