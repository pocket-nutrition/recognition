from enum import Enum

from pydantic import BaseModel


class Status(str, Enum):
    queued = "queued"
    transcribing = "transcribing"
    extracting = "extracting"
    done = "done"
    failed = "failed"


TERMINAL = {Status.done, Status.failed}


class Candidate(BaseModel):
    ingredient_id: str
    name: str
    score: float


class ExtractedItem(BaseModel):
    raw_text: str
    quantity: float | None = None
    unit: str | None = None
    cooking_method: str | None = None
    candidates: list[Candidate]


class CaptureResult(BaseModel):
    transcript: str
    language: str
    items: list[ExtractedItem]


class TicketOut(BaseModel):
    id: str
    status: Status
    created_at: float
    updated_at: float
    expires_at: float
    result: CaptureResult | None = None
    error: str | None = None
    # "delivered": the result was sealed into the vault and is no longer kept here.
    delivery: str = "none"  # none | delivered | failed


class TicketCreated(TicketOut):
    """The answer to `POST /captures`: the only time the ticket's secret is shown. It authenticates every
    later read or delete of that one ticket, so the phone needs no shared key."""

    secret: str
