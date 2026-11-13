import httpx
import pytest

from app.config import Settings
from app.main import create_app

API_KEY = "test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
AUDIO = {"audio": ("meal.m4a", b"\x00\x01fake-audio", "audio/mp4")}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_key=API_KEY,
        ticket_ttl_s=3600,
        sync_timeout_s=5,
        max_audio_bytes=1024,
        default_delay_s=0.3,
    )


@pytest.fixture
async def client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
