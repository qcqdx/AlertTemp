from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ingest_service
from app.core.db import get_session
from app.ingest.service import IngestService

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(
    session: AsyncSession = Depends(get_session),
    ingest: IngestService | None = Depends(get_ingest_service),
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
    return payload
