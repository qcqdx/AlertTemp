from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ingest_service, get_notifier
from app.core.db import get_session
from app.ingest.service import IngestService
from app.notify.notifier import TelegramNotifier

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(
    request: Request,
    session: AsyncSession = Depends(get_session),
    ingest: IngestService | None = Depends(get_ingest_service),
    notifier: TelegramNotifier | None = Depends(get_notifier),
) -> dict:
    database_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False

    payload: dict = {"status": "ok" if database_ok else "degraded", "database": database_ok}
    if ingest is not None:
        payload["mqtt_connected"] = ingest.stats.mqtt_connected
        payload["ingest"] = {
            "received": ingest.stats.received,
            "stored": ingest.stats.stored,
            "invalid": ingest.stats.invalid,
            "unknown_topic": ingest.stats.unknown_topic,
            "buffer_size": ingest.stats.buffer_size,
            "last_message_at": ingest.stats.last_message_at,
        }
        if not ingest.stats.mqtt_connected:
            payload["status"] = "degraded"
    if notifier is not None:
        payload["notifier"] = {
            "sent": notifier.stats.sent,
            "failed": notifier.stats.failed,
            "consecutive_failures": notifier.stats.consecutive_failures,
            "last_error": notifier.stats.last_error,
        }
        # серия подряд неудачных доставок = канал фактически не работает,
        # даже если конфиг валиден (живой прецедент: прокси перестал
        # пропускать CONNECT в рантайме)
        from app.core.config import get_settings

        if notifier.stats.consecutive_failures >= get_settings().notify_degraded_after:
            payload["notifier"]["degraded"] = True
            payload["status"] = "degraded"
    notifier_error = getattr(request.app.state, "notifier_error", None)
    if notifier_error:
        # канал оповещений не запустился (ошибка конфигурации);
        # сбор данных работает, но статус — degraded
        payload["notifier"] = {"error": notifier_error, "disabled": True}
        payload["status"] = "degraded"
    return payload
