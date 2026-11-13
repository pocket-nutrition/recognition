import asyncio
import time

import pytest

from tests.conftest import AUDIO, AUTH


async def create(client, **form):
    return await client.post("/captures", headers=AUTH, files=AUDIO, data=form)


async def test_health_is_open(client):
    r = await client.get("/health")
    assert r.status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}])
async def test_rejects_missing_or_wrong_key(client, headers):
    r = await client.post("/captures", headers=headers, files=AUDIO)
    assert r.status_code == 401


async def test_async_create_returns_202_with_ticket(client):
    r = await create(client)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["result"] is None
    assert body["id"] and body["expires_at"] > body["created_at"]
    assert r.headers["location"] == f"/captures/{body['id']}"


@pytest.mark.parametrize(
    "name, content_type", [("x.gif", "image/gif"), ("x.heic", "image/heic"), ("x.txt", "text/plain")]
)
async def test_rejects_other_media(client, name, content_type):
    r = await client.post("/captures", headers=AUTH, files={"audio": (name, b"123", content_type)})
    assert r.status_code == 415


@pytest.mark.parametrize("name, content_type", [("meal.jpg", "image/jpeg"), ("meal.png", "image/png")])
async def test_accepts_meal_photos_in_the_same_field(client, name, content_type):
    r = await client.post("/captures", headers=AUTH, files={"audio": (name, b"\xff\xd8photo", content_type)})
    assert r.status_code == 202


async def test_rejects_oversized_audio(client):
    big = {"audio": ("m.m4a", b"0" * 2048, "audio/mp4")}
    r = await client.post("/captures", headers=AUTH, files=big)
    assert r.status_code == 413


async def test_unknown_ticket_is_404(client):
    r = await client.get("/captures/does-not-exist", headers=AUTH)
    assert r.status_code == 404


async def test_status_progresses_to_done_with_candidates(client):
    ticket = (await create(client, simulate_delay_s="0.3")).json()
    seen = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = (await client.get(f"/captures/{ticket['id']}", headers=AUTH)).json()
        if not seen or seen[-1] != body["status"]:
            seen.append(body["status"])
        if body["status"] == "done":
            break
        await asyncio.sleep(0.02)
    assert seen == ["queued", "transcribing", "extracting", "done"]
    items = body["result"]["items"]
    assert len(items) >= 2
    assert all(item["candidates"] for item in items)
    assert body["result"]["transcript"]


async def test_failed_outcome(client):
    ticket = (await create(client, simulate_delay_s="0.1", simulate_outcome="failed")).json()
    r = await client.get(
        f"/captures/{ticket['id']}?wait=5&known_status=queued", headers=AUTH
    )
    # Keep waiting until terminal
    body = r.json()
    while body["status"] not in ("done", "failed"):
        body = (
            await client.get(
                f"/captures/{ticket['id']}?wait=5&known_status={body['status']}",
                headers=AUTH,
            )
        ).json()
    assert body["status"] == "failed"
    assert body["error"] and body["result"] is None


async def test_long_poll_blocks_until_status_changes(client):
    ticket = (await create(client, simulate_delay_s="0.6")).json()
    started = time.monotonic()
    r = await client.get(
        f"/captures/{ticket['id']}?wait=5&known_status=queued", headers=AUTH
    )
    elapsed = time.monotonic() - started
    assert r.json()["status"] != "queued"
    assert elapsed < 2


async def test_long_poll_times_out_with_unchanged_status(client):
    ticket = (await create(client, simulate_delay_s="30")).json()
    started = time.monotonic()
    r = await client.get(
        f"/captures/{ticket['id']}?wait=1&known_status=queued", headers=AUTH
    )
    # first transition happens quickly, so ask to wait on the *current* state instead
    current = r.json()["status"]
    r = await client.get(
        f"/captures/{ticket['id']}?wait=1&known_status={current}", headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["status"] == current
    assert time.monotonic() - started >= 0.9


async def test_long_poll_returns_immediately_when_terminal(client):
    ticket = (await create(client, mode="sync", simulate_delay_s="0.1")).json()
    started = time.monotonic()
    r = await client.get(
        f"/captures/{ticket['id']}?wait=10&known_status=done", headers=AUTH
    )
    assert r.json()["status"] == "done"
    assert time.monotonic() - started < 1


async def test_wait_is_capped(client):
    r = await client.get("/captures/x?wait=999", headers=AUTH)
    assert r.status_code == 422


async def test_sync_mode_returns_result_directly(client):
    r = await create(client, mode="sync", simulate_delay_s="0.2")
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["result"]["items"]


async def test_sync_mode_falls_back_to_202_when_too_slow(client):
    r = await create(client, mode="sync", simulate_delay_s="60")
    assert r.status_code == 202
    assert r.json()["status"] in ("queued", "transcribing", "extracting")


async def test_ticket_expires_after_ttl(settings):
    import httpx

    from app.main import create_app

    app = create_app(settings.model_copy(update={"ticket_ttl_s": 1}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            t = (await c.post("/captures", headers=AUTH, files=AUDIO)).json()
            await asyncio.sleep(1.2)
            r = await c.get(f"/captures/{t['id']}", headers=AUTH)
            assert r.status_code == 410


async def test_delete_ticket(client):
    t = (await create(client)).json()
    assert (await client.delete(f"/captures/{t['id']}", headers=AUTH)).status_code == 204
    assert (await client.get(f"/captures/{t['id']}", headers=AUTH)).status_code == 404
