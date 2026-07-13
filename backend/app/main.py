import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    audit,
    auth_routes,
    batches,
    controllers,
    discovery,
    health,
    incidents,
    measurements,
    notify,
    quality,
    reports,
    sensors,
    thresholds,
    users,
    ws,
)
from app.core.bootstrap import ensure_admin
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
        await ensure_admin(session_factory(), settings)

        ingest_service = IngestService(session_factory(), settings, bus)
        await ingest_service.start()
        app.state.ingest_service = ingest_service

        rule_engine = None
        if settings.rules_enabled:
            rule_engine = RuleEngine(session_factory(), settings, bus)
            await rule_engine.start()
            app.state.rule_engine = rule_engine

        # Оповещения — вспомогательный контур: любая ошибка их запуска
        # (опечатка в прокси и т.п.) не имеет права останавливать сбор
        # измерений. Канал отключается, ошибка видна в логе и /healthz.
        notifier = None
        if settings.telegram_bot_token:
            try:
                notifier = TelegramNotifier(session_factory(), settings, bus)
                await notifier.start()
                app.state.notifier = notifier
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Notification channel FAILED to start: %s — notifications are "
                    "DISABLED, measurement ingest continues",
                    exc,
                )
                app.state.notifier_error = str(exc)
                notifier = None

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
    # шина доступна маршрутам (WebSocket-раздача) и тестам без lifespan
    app.state.bus = bus
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(users.router)
    app.include_router(controllers.router)
    app.include_router(sensors.read_router)
    app.include_router(sensors.router)
    app.include_router(discovery.router)
    app.include_router(measurements.router)
    app.include_router(thresholds.router)
    app.include_router(incidents.router)
    app.include_router(notify.router)
    app.include_router(audit.router)
    app.include_router(quality.router)
    app.include_router(reports.router)
    app.include_router(batches.router)
    app.include_router(ws.router)

    # собранный web-интерфейс (frontend/dist), если лежит рядом
    static_dir = Path(settings.static_dir)
    if (static_dir / "index.html").is_file():
        app.mount(
            "/assets", StaticFiles(directory=static_dir / "assets"), name="assets"
        )

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            # API-маршруты зарегистрированы раньше и матчятся первыми.
            # Несуществующие API-пути НЕ должны отдавать index.html со
            # статусом 200 — это маскирует ошибки клиентов и выглядит как
            # анонимно доступный эндпойнт (находка P2 со стенда).
            if path.startswith("api/") or path == "healthz":
                raise HTTPException(status_code=404, detail="Not found")
            candidate = static_dir / path
            if path and ".." not in path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
