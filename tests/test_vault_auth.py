import base64

import httpx
import pytest
from nacl.public import PrivateKey

from app.config import Settings
from app.main import create_app
from tests.conftest import AUDIO

TOKEN = "pnv_drop-token-aaaa"
PUBLIC_KEY = base64.b64encode(bytes(PrivateKey.generate().public_key)).decode()


class FakeVault:
    """Answers GET /v1/me like the real vault does for a drop token, and PUT /v1/drop."""

    def __init__(self, role="dropper", me_status=200):
        self.role = role
        self.me_status = me_status
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/v1/me":
            if self.me_status != 200:
                return httpx.Response(self.me_status, json={"detail": "nope"})
            return httpx.Response(200, json={"account_id": "acc-1", "role": self.role, "scope_id": None})
        return httpx.Response(200, json={"remaining_writes": 0})

    def me_calls(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.path == "/v1/me"]


def make_app(vault: FakeVault, **overrides):
    settings = Settings(
        api_key=overrides.pop("api_key", None),
        trusted_vaults=overrides.pop("trusted_vaults", "vault.test"),
        vault_allow_insecure=True,
        default_delay_s=0.03,
        vault_retry_delays_s=(0.01,),
        **overrides,
    )
    return create_app(settings, vault_transport=httpx.MockTransport(vault))


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def drop_form(token=TOKEN, url="http://vault.test", record_id="capture-1") -> dict:
    return {
        "vault_url": url,
        "vault_token": token,
        "vault_record_id": record_id,
        "inbox_public_key": PUBLIC_KEY,
    }


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
async def setup():
    vault = FakeVault()
    app = make_app(vault)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            yield client, vault


# --- creating a capture ---------------------------------------------------------------------------


async def test_a_valid_drop_token_is_enough_and_the_vault_is_asked_with_that_token(setup):
    client, vault = setup
    resp = await client.post("/captures", files=AUDIO, data=drop_form())
    assert resp.status_code == 202, resp.text
    assert resp.json()["secret"]

    (me,) = vault.me_calls()
    assert me.headers["authorization"] == f"Bearer {TOKEN}"
    assert str(me.url) == "http://vault.test/v1/me"


async def test_without_a_drop_or_a_key_nothing_is_accepted(setup):
    client, vault = setup
    resp = await client.post("/captures", files=AUDIO)
    assert resp.status_code == 401
    assert vault.calls == []


async def test_a_vault_that_is_not_trusted_is_never_called(setup):
    client, vault = setup
    resp = await client.post("/captures", files=AUDIO, data=drop_form(url="http://evil.example"))
    assert resp.status_code == 401
    assert vault.calls == []


async def test_a_look_alike_host_is_not_the_trusted_one(setup):
    client, vault = setup
    resp = await client.post("/captures", files=AUDIO, data=drop_form(url="http://vault.test.evil.example"))
    assert resp.status_code == 401
    assert vault.calls == []


async def test_a_token_the_vault_rejects_is_refused():
    vault = FakeVault(me_status=401)
    app = make_app(vault)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.post("/captures", files=AUDIO, data=drop_form())).status_code == 401


async def test_an_owner_key_sent_as_a_drop_token_is_refused():
    # Only a write-only drop token authenticates a dictation. A key that can read the vault must not.
    app = make_app(FakeVault(role="owner"))
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.post("/captures", files=AUDIO, data=drop_form())).status_code == 401


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_an_unreachable_vault_is_a_retryable_503(status):
    app = make_app(FakeVault(me_status=status))
    async with app.router.lifespan_context(app), client_for(app) as client:
        resp = await client.post("/captures", files=AUDIO, data=drop_form())
        assert resp.status_code == 503 and "retry-after" in resp.headers


async def test_a_vault_that_does_not_answer_is_a_retryable_503():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    settings = Settings(
        api_key=None, trusted_vaults="vault.test", vault_allow_insecure=True, vault_retry_delays_s=(0.01,)
    )
    app = create_app(settings, vault_transport=httpx.MockTransport(boom))
    async with app.router.lifespan_context(app), client_for(app) as client:
        resp = await client.post("/captures", files=AUDIO, data=drop_form())
        assert resp.status_code == 503


async def test_the_api_key_still_works_without_asking_the_vault():
    vault = FakeVault(me_status=401)
    app = make_app(vault, api_key="legacy-key-0123456789")
    async with app.router.lifespan_context(app), client_for(app) as client:
        resp = await client.post("/captures", headers=bearer("legacy-key-0123456789"), files=AUDIO)
        assert resp.status_code == 202
        assert vault.me_calls() == []


# --- one drop token, one dictation ---------------------------------------------------------------


async def test_retrying_with_the_same_token_returns_the_same_ticket(setup):
    client, _ = setup
    first = (await client.post("/captures", files=AUDIO, data=drop_form())).json()
    again = await client.post("/captures", files=AUDIO, data=drop_form())
    assert again.status_code == 202
    assert again.json()["id"] == first["id"] and again.json()["secret"] == first["secret"]


async def test_a_token_whose_ticket_is_gone_cannot_start_another_dictation(setup):
    client, _ = setup
    first = (await client.post("/captures", files=AUDIO, data=drop_form())).json()
    assert (await client.delete(f"/captures/{first['id']}", headers=bearer(first["secret"]))).status_code == 204
    replay = await client.post("/captures", files=AUDIO, data=drop_form())
    assert replay.status_code == 409


async def test_a_different_token_starts_a_new_dictation(setup):
    client, _ = setup
    first = (await client.post("/captures", files=AUDIO, data=drop_form(token="pnv_one-token-aaaa"))).json()
    second = (await client.post("/captures", files=AUDIO, data=drop_form(token="pnv_two-token-bbbb"))).json()
    assert first["id"] != second["id"] and first["secret"] != second["secret"]


# --- following a ticket --------------------------------------------------------------------------


async def test_the_ticket_secret_reads_and_deletes_only_its_own_ticket(setup):
    client, _ = setup
    mine = (await client.post("/captures", files=AUDIO, data=drop_form(token="pnv_mine-token-aaaa"))).json()
    other = (await client.post("/captures", files=AUDIO, data=drop_form(token="pnv_other-token-bbbb"))).json()

    assert (await client.get(f"/captures/{mine['id']}", headers=bearer(mine["secret"]))).status_code == 200
    assert (await client.get(f"/captures/{mine['id']}", headers=bearer(other["secret"]))).status_code == 401
    assert (await client.get(f"/captures/{mine['id']}", headers=bearer("wrong"))).status_code == 401
    assert (await client.get(f"/captures/{mine['id']}")).status_code == 401
    assert (await client.delete(f"/captures/{mine['id']}", headers=bearer(other["secret"]))).status_code == 401
    assert (await client.delete(f"/captures/{mine['id']}", headers=bearer(mine["secret"]))).status_code == 204


async def test_the_secret_is_only_in_the_creation_answer(setup):
    client, _ = setup
    created = (await client.post("/captures", files=AUDIO, data=drop_form())).json()
    polled = (await client.get(f"/captures/{created['id']}", headers=bearer(created["secret"]))).json()
    assert "secret" not in polled


async def test_an_unknown_ticket_is_404_whatever_is_presented(setup):
    client, _ = setup
    for headers in ({}, bearer("anything")):
        assert (await client.get("/captures/does-not-exist", headers=headers)).status_code == 404


async def test_an_expired_ticket_is_410_only_for_its_own_secret():
    app = make_app(FakeVault(), ticket_ttl_s=0)
    async with app.router.lifespan_context(app), client_for(app) as client:
        created = (await client.post("/captures", files=AUDIO, data=drop_form())).json()
        assert (await client.get(f"/captures/{created['id']}", headers=bearer(created["secret"]))).status_code == 410
        # Anyone else just sees an unknown ticket: expiry is not an oracle for ids.
        assert (await client.get(f"/captures/{created['id']}", headers=bearer("wrong"))).status_code == 404
        assert (await client.get(f"/captures/{created['id']}")).status_code == 404


async def test_the_whole_dictation_works_without_any_api_key():
    vault = FakeVault()
    app = make_app(vault)
    async with app.router.lifespan_context(app), client_for(app) as client:
        created = (await client.post("/captures", files=AUDIO, data=drop_form())).json()
        for _ in range(100):
            body = (
                await client.get(f"/captures/{created['id']}?wait=1", headers=bearer(created["secret"]))
            ).json()
            if body["status"] in ("done", "failed"):
                break
        assert body["status"] == "done" and body["delivery"] == "delivered"
        assert [c for c in vault.calls if c.url.path == "/v1/drop"]


# --- configuration -------------------------------------------------------------------------------


def test_some_way_to_authenticate_is_required():
    with pytest.raises(ValueError):
        Settings(api_key=None)
    Settings(api_key="legacy-key-0123456789")
    Settings(api_key=None, trusted_vaults="vault.example")


def test_trusted_vaults_are_a_clean_set_of_lowercase_hosts():
    settings = Settings(api_key=None, trusted_vaults=" Vault.Example , other.example ,")
    assert settings.trusted_vault_hosts == frozenset({"vault.example", "other.example"})


# --- robustness ----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [bytes(32), b"\x01" + bytes(31), bytes(31), bytes(33)])
async def test_an_unusable_inbox_key_is_refused_up_front(setup, key):
    client, _ = setup
    form = {**drop_form(), "inbox_public_key": base64.b64encode(key).decode()}
    resp = await client.post("/captures", files=AUDIO, data=form)
    assert resp.status_code == 422


async def test_a_crash_while_delivering_finishes_the_ticket_with_the_result_kept():
    from app.store import TicketStore

    async def crash(target, payload):
        raise RuntimeError("boom")

    store = TicketStore(ttl_s=3600, tombstone_ttl_s=3600, deliver=crash)
    ticket = store.create()
    from app.vault import DropTarget

    ticket.drop = DropTarget(url="http://v", token="t", inbox_public_key=bytes(32), record_id="r")
    store.start(ticket, 0.03, __import__("app.schemas", fromlist=["Status"]).Status.done)
    await store.wait_for_terminal(ticket, 3)
    assert ticket.status.value == "done"
    assert ticket.delivery == "failed" and ticket.result is not None
