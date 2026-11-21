import base64

import httpx
import pytest
from nacl.public import PrivateKey

from app.config import Settings
from app.main import create_app

API_KEY = "test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
WORKER_KEY = "worker-key-0123456789"
WORKER = {"Authorization": f"Bearer {WORKER_KEY}"}
AUDIO = {"audio": ("meal.m4a", b"\x00\x01fake-audio", "audio/mp4")}
INBOX_PUBLIC_KEY = base64.b64encode(bytes(PrivateKey.generate().public_key)).decode()
# Every capture names the vault drop its result goes to.
DROP_FORM = {
    "vault_url": "http://vault.test",
    "vault_token": "pnv_drop",
    "vault_record_id": "capture-1",
    "inbox_public_key": INBOX_PUBLIC_KEY,
}


def make_settings(**overrides) -> Settings:
    values = {
        "api_key": API_KEY,
        "worker_key": WORKER_KEY,
        "vault_allow_insecure": True,
        "ticket_ttl_s": 3600,
        "sync_timeout_s": 1,
        "max_audio_bytes": 1024,
        **overrides,
    }
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def submit(client, files=AUDIO, **form) -> dict:
    resp = await client.post("/captures", headers=AUTH, files=files, data={**DROP_FORM, **form})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def claim(client, wait: float = 0) -> httpx.Response:
    return await client.post(f"/worker/claim?wait={wait}", headers=WORKER)


async def phone_view(client, ticket_id: str) -> dict:
    return (await client.get(f"/captures/{ticket_id}", headers=AUTH)).json()
