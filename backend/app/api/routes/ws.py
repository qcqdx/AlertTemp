"""Живая раздача событий по WebSocket (этап B.3).

Заменяет 5-секундный поллинг дашборда/графиков: подписка на внутреннюю
шину (`core/bus.py`) и push измерений и переходов инцидентов клиенту.

Раздача — вспомогательный контур: медленный или отвалившийся клиент не
тормозит ingest (шина отбрасывает событие при переполнении его очереди),
а ошибка отправки только закрывает сокет, оставляя главный контур целым.

Контракт сообщений (JSON, время — ISO-8601 с суффиксом Z, паттерн §2.8):
- измерение: {"type":"measurement","sensor_id","topic","value","at"}
- инцидент:  {"type":"incident","kind","incident_id","sensor_id",
              "sensor_alias","controller_id","controller_name","itype",
              "severity","value","opened_at","closed_at","peak_value"}
  (kind = opened | escalated | resolved; поле типа названо itype, чтобы
  не пересекаться с полем-дискриминатором type сообщения)
"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.bus import IncidentEvent, MeasurementEvent
from app.core.db import session_factory
from app.core.security import SESSION_COOKIE, resolve_session

logger = logging.getLogger(__name__)

router = APIRouter()


def _iso(dt) -> str | None:
    """ISO-8601 с Z вместо +00:00 (паттерн §2.8)."""
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def serialize_event(event: object) -> dict | None:
    if isinstance(event, MeasurementEvent):
        return {
            "type": "measurement",
            "sensor_id": event.sensor_id,
            "topic": event.topic,
            "value": event.value,
            "at": _iso(event.at),
        }
    if isinstance(event, IncidentEvent):
        return {
            "type": "incident",
            "kind": event.kind,
            "incident_id": event.incident_id,
            "sensor_id": event.sensor_id,
            "sensor_alias": event.sensor_alias,
            "controller_id": event.controller_id,
            "controller_name": event.controller_name,
            "itype": event.type,
            "severity": event.severity,
            "value": event.value,
            "opened_at": _iso(event.opened_at),
            "closed_at": _iso(event.closed_at),
            "peak_value": event.peak_value,
        }
    return None  # прочие события в раздачу не идут


@router.websocket("/api/v1/ws")
async def ws_events(websocket: WebSocket) -> None:
    # аутентификация той же серверной сессией, что и REST (cookie);
    # аноним не подключается (симметрично P2 — без анонимных эндпойнтов)
    token = websocket.cookies.get(SESSION_COOKIE)
    user = None
    if token:
        async with session_factory()() as session:
            user = await resolve_session(session, token)
    if user is None:
        await websocket.close(code=4401)  # policy violation: не аутентифицирован
        return

    await websocket.accept()
    bus = websocket.app.state.bus
    queue = bus.subscribe()
    try:
        while True:
            event = await queue.get()
            payload = serialize_event(event)
            if payload is not None:
                await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # отвал клиента при отправке — закрываем тихо
        logger.debug("WebSocket closed: %s", exc)
    finally:
        bus.unsubscribe(queue)
