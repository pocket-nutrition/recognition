import asyncio
import time

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.conftest import AUDIO, AUTH, DROP_FORM, WORKER, claim, phone_view, submit


async def create(client, files=AUDIO, **form) -> httpx.Response:
    return await client.post("/captures", headers=AUTH, files=files, data={**DROP_FORM, **form})


async def test_health_is_open(client):
    assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}])
async def test_rejects_missing_or_wrong_key(client, headers):
    # The drop target points at a vault this server does not trust, so only the key could let it in.
    r = await client.post("/captures", headers=headers, files=AUDIO, data=DROP_FORM)
    assert r.status_code == 401


async def test_async_create_returns_202_with_a_ticket_and_no_result(client):
    r = await create(client)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["delivery"] == "none"
    assert "result" not in body
    assert body["id"] and body["expires_at"] > body["created_at"]
    assert r.headers["location"] == f"/captures/{body['id']}"


# --- the vault drop target is required -----------------------------------------------------------


async def test_a_capture_without_a_vault_drop_is_refused_even_with_the_key(client):
    r = await client.post("/captures", headers=AUTH, files=AUDIO)
    assert r.status_code == 422


@pytest.mark.parametrize("missing", list(DROP_FORM))
async def test_every_drop_field_is_required(client, missing):
    form = {k: v for k, v in DROP_FORM.items() if k != missing}
    assert (await client.post("/captures", headers=AUTH, files=AUDIO, data=form)).status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"vault_token": ""},
        {"inbox_public_key": "bm90LWEta2V5"},
        {"inbox_public_key": "***"},
        {"vault_record_id": "bad id"},
    ],
)
async def test_rejects_malformed_drop_targets(client, overrides):
    assert (await create(client, **overrides)).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://vault.example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://10.0.0.5",
        "https://u:p@example.com",
        "https://169.254.169.254",
    ],
)
async def test_blocks_ssrf_drop_targets(url):
    # Real checks, not the insecure test override.
    settings = Settings(api_key="test-key", worker_key="worker-key-0123456789")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await create(c, vault_url=url)).status_code == 422


# --- media ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, content_type", [("x.gif", "image/gif"), ("x.heic", "image/heic"), ("x.txt", "text/plain")]
)
async def test_rejects_other_media(client, name, content_type):
    assert (await create(client, files={"audio": (name, b"123", content_type)})).status_code == 415


@pytest.mark.parametrize("name, content_type", [("meal.jpg", "image/jpeg"), ("meal.png", "image/png")])
async def test_accepts_meal_photos_in_the_same_field(client, name, content_type):
    assert (await create(client, files={"audio": (name, b"\xff\xd8photo", content_type)})).status_code == 202


async def test_rejects_oversized_audio(client):
    assert (await create(client, files={"audio": ("m.m4a", b"0" * 2048, "audio/mp4")})).status_code == 413


# --- following a ticket --------------------------------------------------------------------------


async def test_unknown_ticket_is_404(client):
    assert (await client.get("/captures/does-not-exist", headers=AUTH)).status_code == 404


async def test_long_poll_returns_when_the_worker_reports_progress(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    waiting = asyncio.create_task(client.get(f"/captures/{ticket['id']}?wait=5&known_status=queued", headers=AUTH))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await client.post(f"/worker/leases/{lease}/progress", headers=WORKER, json={"status": "transcribing"})
    assert (await waiting).json()["status"] == "transcribing"
    assert time.monotonic() - started < 2


async def test_long_poll_times_out_with_unchanged_status(client):
    ticket = await submit(client)
    started = time.monotonic()
    r = await client.get(f"/captures/{ticket['id']}?wait=1&known_status=queued", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert time.monotonic() - started >= 0.9


async def test_long_poll_returns_immediately_when_terminal(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    await client.post(f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "delivered"})
    started = time.monotonic()
    r = await client.get(f"/captures/{ticket['id']}?wait=10&known_status=done", headers=AUTH)
    assert r.json()["status"] == "done"
    assert time.monotonic() - started < 1


async def test_wait_is_capped(client):
    assert (await client.get("/captures/x?wait=999", headers=AUTH)).status_code == 422


async def test_sync_mode_answers_200_once_the_worker_is_done(client):
    pending = asyncio.create_task(create(client, mode="sync"))
    job = (await claim(client, wait=5)).json()
    await client.post(f"/worker/leases/{job['lease_id']}/complete", headers=WORKER, json={"delivery": "delivered"})
    r = await pending
    assert r.status_code == 200
    assert r.json()["status"] == "done" and r.json()["delivery"] == "delivered"
    assert "result" not in r.json()


async def test_sync_mode_falls_back_to_202_when_too_slow(client):
    r = await create(client, mode="sync")
    assert r.status_code == 202 and r.json()["status"] == "queued"


async def test_ticket_expires_after_ttl(settings):
    app = create_app(settings.model_copy(update={"ticket_ttl_s": 1}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            t = await submit(c)
            await asyncio.sleep(1.2)
            assert (await c.get(f"/captures/{t['id']}", headers=AUTH)).status_code == 410


async def test_delete_ticket(client):
    t = await submit(client)
    assert (await client.delete(f"/captures/{t['id']}", headers=AUTH)).status_code == 204
    assert (await phone_view(client, t["id"])) == {"detail": "ticket not found"}
