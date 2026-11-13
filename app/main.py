import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import Settings
from app.router import router
from app.store import TicketStore
from app.worker import router as worker_router
from app.vault import VaultDeliverer

logging.basicConfig(level=logging.INFO)


def create_app(
    settings: Settings | None = None, vault_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()
    deliverer = VaultDeliverer(settings, vault_transport)
    store = TicketStore(
        settings.ticket_ttl_s,
        settings.tombstone_ttl_s,
        deliverer.deliver,
        settings.lease_s,
        settings.max_attempts,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def sweeper():
            while True:
                await asyncio.sleep(settings.sweep_interval_s)
                store.sweep()
                await store.sweep_leases()

        task = asyncio.create_task(sweeper())
        yield
        task.cancel()
        store.cancel_all()

    app = FastAPI(title="Pocket Nutrition Recognition", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.deliverer = deliverer
    app.include_router(router)
    if settings.pipeline == "worker":
        app.include_router(worker_router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


def build() -> FastAPI:
    return create_app()
