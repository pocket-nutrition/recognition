import asyncio
import base64

import httpx
import pytest
from nacl.public import PrivateKey

from app.config import Settings
from app.main import create_app
from tests.conftest import API_KEY, AUDIO, AUTH

WORKER_KEY = "worker-key-0123456789"
WORKER = {"Authorization": f"Bearer {WORKER_KEY}"}
DROP_FORM = {
    "vault_url": "http://vault.test",
    "vault_token": "pnv_drop",
    "vault_record_id": "capture-1",
    "inbox_public_key": base64.b64encode(bytes(PrivateKey.generate().public_key)).decode(),
}
RESULT = {
    "transcript": "une pomme",
    "language": "fr",
    "items": [
        {
            "raw_text": "une pomme",
            "quantity": 1,
            "unit": "piece",
            "cooking_method": None,
            "candidates": [{"ingredient_id": "pomme_crue", "name": "Apple, raw", "score": 0.9}],
        }
    ],
}


def make_settings(**overrides) -> Settings:
    return Settings(
        api_key=API_KEY,
        pipeline="worker",
        worker_key=WORKER_KEY,
        vault_allow_insecure=True,
        **overrides,
    )


@pytest.fixture
async def client():
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def submit(client, **form) -> dict:
    resp = await client.post("/captures", headers=AUTH, files=AUDIO, data=form)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def claim(client, wait: float = 0) -> httpx.Response:
    return await client.post(f"/worker/claim?wait={wait}", headers=WORKER)


async def phone_view(client, ticket_id: str) -> dict:
    return (await client.get(f"/captures/{ticket_id}", headers=AUTH)).json()


async def test_capture_waits_in_the_queue_until_a_worker_takes_it(client):
    ticket = await submit(client)
    await asyncio.sleep(0.2)
    assert (await phone_view(client, ticket["id"]))["status"] == "queued"


async def test_claim_on_an_empty_queue_returns_204(client):
    assert (await claim(client)).status_code == 204


async def test_claim_hands_out_the_job_with_its_audio_and_drop_target(client):
    ticket = await submit(client, **DROP_FORM)
    job = (await claim(client)).json()

    assert job["ticket_id"] == ticket["id"]
    assert job["audio_content_type"] == "audio/mp4"
    assert job["drop"] == {
        "url": "http://vault.test",
        "token": "pnv_drop",
        "record_id": "capture-1",
        "inbox_public_key": DROP_FORM["inbox_public_key"],
    }
    audio = await client.get(f"/worker/leases/{job['lease_id']}/audio", headers=WORKER)
    assert audio.content == AUDIO["audio"][1]
    assert audio.headers["content-type"] == "audio/mp4"


async def test_a_photo_reaches_the_worker_with_its_image_type(client):
    photo = {"audio": ("meal.jpg", b"\xff\xd8photo", "image/jpeg")}
    assert (await client.post("/captures", headers=AUTH, files=photo)).status_code == 202
    job = (await claim(client)).json()

    assert job["audio_content_type"] == "image/jpeg"
    media = await client.get(f"/worker/leases/{job['lease_id']}/audio", headers=WORKER)
    assert media.content == b"\xff\xd8photo" and media.headers["content-type"] == "image/jpeg"


async def test_a_job_is_handed_to_only_one_worker(client):
    await submit(client)
    assert (await claim(client)).status_code == 200
    assert (await claim(client)).status_code == 204


async def test_claim_long_polls_until_a_capture_arrives(client):
    waiting = asyncio.create_task(claim(client, wait=5))
    await asyncio.sleep(0.1)
    ticket = await submit(client)
    resp = await waiting
    assert resp.status_code == 200 and resp.json()["ticket_id"] == ticket["id"]


async def test_phone_key_cannot_use_worker_routes_and_worker_key_cannot_read_tickets(client):
    ticket = await submit(client)
    assert (await client.post("/worker/claim", headers=AUTH)).status_code == 401
    assert (await client.post("/worker/claim")).status_code == 401
    assert (await client.get(f"/captures/{ticket['id']}", headers=WORKER)).status_code == 401
    assert (await client.post("/captures", headers=WORKER, files=AUDIO)).status_code == 401


async def test_progress_is_visible_to_the_phone(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    for status in ("transcribing", "extracting"):
        resp = await client.post(f"/worker/leases/{lease}/progress", headers=WORKER, json={"status": status})
        assert resp.status_code == 204
        assert (await phone_view(client, ticket["id"]))["status"] == status


async def test_progress_rejects_terminal_statuses(client):
    await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    resp = await client.post(f"/worker/leases/{lease}/progress", headers=WORKER, json={"status": "done"})
    assert resp.status_code == 422


async def test_complete_without_a_vault_keeps_the_result_on_the_ticket(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    resp = await client.post(
        f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "none", "result": RESULT}
    )
    assert resp.status_code == 204
    done = await phone_view(client, ticket["id"])
    assert done["status"] == "done" and done["delivery"] == "none" and done["result"] == RESULT


async def test_delivered_result_is_not_kept_and_must_not_be_sent(client):
    ticket = await submit(client, **DROP_FORM)
    lease = (await claim(client)).json()["lease_id"]

    leaked = await client.post(
        f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "delivered", "result": RESULT}
    )
    assert leaked.status_code == 422

    ok = await client.post(f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "delivered"})
    assert ok.status_code == 204
    done = await phone_view(client, ticket["id"])
    assert done["status"] == "done" and done["delivery"] == "delivered" and done["result"] is None


async def test_complete_needs_a_result_unless_it_was_delivered(client):
    await submit(client, **DROP_FORM)
    lease = (await claim(client)).json()["lease_id"]
    for body in ({"delivery": "none"}, {"delivery": "failed"}):
        resp = await client.post(f"/worker/leases/{lease}/complete", headers=WORKER, json=body)
        assert resp.status_code == 422


async def test_failed_delivery_falls_back_to_the_result_on_the_ticket(client):
    ticket = await submit(client, **DROP_FORM)
    lease = (await claim(client)).json()["lease_id"]
    await client.post(
        f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "failed", "result": RESULT}
    )
    done = await phone_view(client, ticket["id"])
    assert done["delivery"] == "failed" and done["result"] == RESULT


async def test_fail_marks_the_ticket_failed(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    resp = await client.post(f"/worker/leases/{lease}/fail", headers=WORKER, json={"error": "no speech"})
    assert resp.status_code == 204
    failed = await phone_view(client, ticket["id"])
    assert failed["status"] == "failed" and failed["error"] == "no speech"


async def test_a_finished_lease_is_dead_and_the_audio_is_gone(client):
    await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    await client.post(f"/worker/leases/{lease}/fail", headers=WORKER, json={"error": "x"})
    assert (await client.get(f"/worker/leases/{lease}/audio", headers=WORKER)).status_code == 409
    assert (
        await client.post(f"/worker/leases/{lease}/complete", headers=WORKER, json={"delivery": "none", "result": RESULT})
    ).status_code == 409


async def test_unknown_lease_is_409(client):
    resp = await client.post("/worker/leases/nope/progress", headers=WORKER, json={"status": "extracting"})
    assert resp.status_code == 409


async def test_deleting_the_ticket_invalidates_the_lease(client):
    ticket = await submit(client)
    lease = (await claim(client)).json()["lease_id"]
    await client.delete(f"/captures/{ticket['id']}", headers=AUTH)
    assert (await client.get(f"/worker/leases/{lease}/audio", headers=WORKER)).status_code == 409


async def test_progress_extends_the_lease():
    app = create_app(make_settings(lease_s=0.4))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await submit(c)
            lease = (await claim(c)).json()["lease_id"]
            for _ in range(3):
                await asyncio.sleep(0.25)
                resp = await c.post(f"/worker/leases/{lease}/progress", headers=WORKER, json={"status": "extracting"})
                assert resp.status_code == 204


async def test_an_abandoned_job_goes_back_to_the_queue_with_a_new_lease():
    app = create_app(make_settings(lease_s=0.2))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            ticket = await submit(c)
            first = (await claim(c)).json()
            await c.post(f"/worker/leases/{first['lease_id']}/progress", headers=WORKER, json={"status": "transcribing"})
            await asyncio.sleep(0.3)

            second = (await claim(c)).json()
            assert second["ticket_id"] == ticket["id"] and second["lease_id"] != first["lease_id"]
            assert (await phone_view(c, ticket["id"]))["status"] == "queued"
            stale = await c.post(
                f"/worker/leases/{first['lease_id']}/complete", headers=WORKER, json={"delivery": "none", "result": RESULT}
            )
            assert stale.status_code == 409


async def test_a_job_that_keeps_being_abandoned_fails():
    app = create_app(make_settings(lease_s=0.1, max_attempts=2))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            ticket = await submit(c)
            for _ in range(2):
                assert (await claim(c)).status_code == 200
                await asyncio.sleep(0.15)
            assert (await claim(c)).status_code == 204
            failed = await phone_view(c, ticket["id"])
            assert failed["status"] == "failed" and "worker" in failed["error"]


async def test_queue_is_bounded_by_count_and_size():
    app = create_app(make_settings(max_queued=2))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await submit(c)
            await submit(c)
            full = await c.post("/captures", headers=AUTH, files=AUDIO)
            assert full.status_code == 503 and "retry-after" in full.headers

    app = create_app(make_settings(max_queued_bytes=len(AUDIO["audio"][1]) + 1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await submit(c)
            assert (await c.post("/captures", headers=AUTH, files=AUDIO)).status_code == 503


async def test_a_claimed_job_still_counts_against_the_queue_until_it_finishes():
    app = create_app(make_settings(max_queued=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await submit(c)
            lease = (await claim(c)).json()["lease_id"]
            assert (await c.post("/captures", headers=AUTH, files=AUDIO)).status_code == 503
            await c.post(f"/worker/leases/{lease}/fail", headers=WORKER, json={"error": "x"})
            await submit(c)


async def test_worker_routes_do_not_exist_in_simulator_mode():
    app = create_app(Settings(api_key=API_KEY))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.post("/worker/claim", headers=WORKER)).status_code == 404


def test_worker_mode_needs_a_distinct_strong_worker_key():
    with pytest.raises(ValueError):
        Settings(api_key=API_KEY, pipeline="worker")
    with pytest.raises(ValueError):
        Settings(api_key="same-key-0123456789", pipeline="worker", worker_key="same-key-0123456789")
    with pytest.raises(ValueError):
        Settings(api_key=API_KEY, pipeline="worker", worker_key="short")
