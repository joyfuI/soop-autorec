from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import initialize_database
from app.routers.api_channels import router as channels_router
from app.routers.api_events import router as events_router
from app.routers.api_recordings import router as recordings_router
from app.routers.api_settings import router as settings_router
from app.routers.api_system import router as system_router
from app.routers.ui import router as ui_router
from app.services.poller import Supervisor


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    initialize_database(settings)
    supervisor = Supervisor(settings)

    application.state.settings = settings
    application.state.supervisor = supervisor

    await supervisor.start()
    try:
        yield
    finally:
        await supervisor.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="soop-autorec", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(ui_router)
    app.include_router(system_router)
    app.include_router(channels_router)
    app.include_router(recordings_router)
    app.include_router(events_router)
    app.include_router(settings_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=False,
    )


if __name__ == "__main__":
    main()
