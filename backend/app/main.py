import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import (
    controllers,
    discovery,
    health,
    incidents,
    measurements,
    notify,
    sensors,
    thresholds,
)
from app.core.bus import bus
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, init_engine, session_factory
from app.ingest.mqtt import MqttIngest
from app.ingest.service import IngestService
from app.notify.notifier import TelegramNotifier
from app.rules.engine import RuleEngine

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

        rule_engine = None
        if settings.rules_enabled:
            rule_engine = RuleEngine(session_factory(), settings, bus)
            await rule_engine.start()
            app.state.rule_engine = rule_engine

        notifier = None
        if settings.telegram_bot_token:
            notifier = TelegramNotifier(session_factory(), settings, bus)
            await notifier.start()
            app.state.notifier = notifier

        mqtt = None
        if settings.mqtt_enabled:
            mqtt = MqttIngest(settings, ingest_service)
            await mqtt.start()

        try:
            yield
        finally:
            if mqtt is not None:
                await mqtt.stop()
            if notifier is not None:
                await notifier.stop()
            if rule_engine is not None:
                await rule_engine.stop()
            await ingest_service.stop()
            await dispose_engine()

    app = FastAPI(title="ColdWatch", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(controllers.router)
    app.include_router(sensors.router)
    app.include_router(discovery.router)
    app.include_router(measurements.router)
    app.include_router(thresholds.router)
    app.include_router(incidents.router)
    app.include_router(notify.router)
    return app


app = create_app()
