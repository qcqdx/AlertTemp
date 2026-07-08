import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import controllers, discovery, health, measurements, sensors
from app.core.bus import bus
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, init_engine, session_factory
from app.ingest.mqtt import MqttIngest
from app.ingest.service import IngestService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_engine(settings.database_url)

        ingest_service = IngestService(session_factory(), settings, bus)
        await ingest_service.start()
        app.state.ingest_service = ingest_service

        mqtt = None
        if settings.mqtt_enabled:
            mqtt = MqttIngest(settings, ingest_service)
            await mqtt.start()

        try:
            yield
        finally:
            if mqtt is not None:
                await mqtt.stop()
            await ingest_service.stop()
            await dispose_engine()

    app = FastAPI(title="ColdWatch", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(controllers.router)
    app.include_router(sensors.router)
    app.include_router(discovery.router)
    app.include_router(measurements.router)
    return app


app = create_app()
