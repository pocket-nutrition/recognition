import base64
import json
import time

import httpx
import pytest
from nacl.public import PrivateKey, SealedBox

from app.config import Settings
from app.main import create_app
from tests.conftest import API_KEY, AUDIO, AUTH

TOKEN = "pnv_drop-token"


class FakeVault:
    """Records PUT /v1/drop calls and can be told to fail."""

    def __init__(self, statuses: list[int] | None = None):
        self.statuses = statuses or [200]
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        status = self.statuses[min(len(self.calls), len(self.statuses)) - 1]
        return httpx.Response(status, json={"remaining_writes": 0})


def make_client(vault: FakeVault):
    settings = Settings(
        api_key=API_KEY,
        default_delay_s=0.03,
        vault_allow_insecure=True,
        vault_retry_delays_s=(0.01, 0.01),
    )
    app = create_app(settings, vault_transport=httpx.MockTransport(vault))
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def capture(client, key: PrivateKey, **overrides):
    form = {
        "vault_url": "http://vault.test",
        "vault_token": TOKEN,
        "vault_record_id": "capture-1",
        "inbox_public_key": base64.b64encode(bytes(key.public_key)).decode(),
        **overrides,
    }
    return await client.post("/captures", headers=AUTH, files=AUDIO, data=form)


async def finish(client, ticket_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = (await client.get(f"/captures/{ticket_id}?wait=1", headers=AUTH)).json()
        if body["status"] in ("done", "failed"):
            return body
    raise AssertionError("ticket never became terminal")


@pytest.fixture
def inbox() -> PrivateKey:
    return PrivateKey.generate()


async def test_result_is_sealed_to_the_inbox_key_and_not_kept(inbox):
    vault = FakeVault()
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        ticket = (await capture(client, inbox)).json()
        done = await finish(client, ticket["id"])

    assert done["status"] == "done" and done["delivery"] == "delivered"
    assert done["result"] is None  # the service does not keep what it deposited

    (call,) = vault.calls
    assert call.url == "http://vault.test/v1/drop"
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    sealed = base64.b64decode(json.loads(call.content)["ciphertext"])
    payload = json.loads(SealedBox(inbox).decrypt(sealed))
    assert payload["record_id"] == "capture-1" and payload["ticket_id"] == ticket["id"]
    assert payload["status"] == "done" and payload["result"]["items"]


async def test_plaintext_never_reaches_the_vault(inbox):
    vault = FakeVault()
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        ticket = (await capture(client, inbox)).json()
        await finish(client, ticket["id"])
    assert b"transcript" not in vault.calls[0].content


async def test_failed_capture_is_delivered_too(inbox):
    vault = FakeVault()
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        ticket = (await capture(client, inbox, simulate_outcome="failed")).json()
        done = await finish(client, ticket["id"])
    assert done["status"] == "failed" and done["delivery"] == "delivered"
    payload = json.loads(SealedBox(inbox).decrypt(base64.b64decode(json.loads(vault.calls[0].content)["ciphertext"])))
    assert payload["status"] == "failed" and payload["error"]


async def test_retries_server_errors_then_succeeds(inbox):
    vault = FakeVault([503, 200])
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        done = await finish(client, (await capture(client, inbox)).json()["id"])
    assert done["delivery"] == "delivered" and len(vault.calls) == 2


async def test_rejected_token_is_not_retried_and_result_is_kept(inbox):
    vault = FakeVault([401])
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        done = await finish(client, (await capture(client, inbox)).json()["id"])
    assert done["delivery"] == "failed" and len(vault.calls) == 1
    assert done["result"] is not None  # fallback so the app can still fetch it from the ticket


async def test_without_vault_fields_behaviour_is_unchanged():
    vault = FakeVault()
    app, client = make_client(vault)
    async with app.router.lifespan_context(app), client:
        ticket = (await client.post("/captures", headers=AUTH, files=AUDIO)).json()
        done = await finish(client, ticket["id"])
    assert done["delivery"] == "none" and done["result"] is not None and not vault.calls


@pytest.mark.parametrize(
    "overrides",
    [
        {"vault_token": ""},
        {"inbox_public_key": "bm90LWEta2V5"},
        {"inbox_public_key": "***"},
        {"vault_record_id": "bad id"},
    ],
)
async def test_rejects_incomplete_or_malformed_targets(inbox, overrides):
    app, client = make_client(FakeVault())
    async with app.router.lifespan_context(app), client:
        assert (await capture(client, inbox, **overrides)).status_code == 422


@pytest.mark.parametrize(
    "url",
    ["http://vault.example.com", "https://127.0.0.1", "https://localhost", "https://10.0.0.5", "https://u:p@example.com", "https://169.254.169.254"],
)
async def test_blocks_ssrf_targets(inbox, url):
    # Real checks, not the insecure test override.
    settings = Settings(api_key=API_KEY, default_delay_s=0.03)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await capture(client, inbox, vault_url=url)).status_code == 422
