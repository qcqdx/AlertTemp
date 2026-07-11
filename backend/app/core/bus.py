"""Внутрипроцессная шина событий.

Ingest публикует принятые измерения; подписчики (rule engine, WebSocket-раздача)
получают их через собственные очереди. Медленный подписчик не тормозит ingest:
при переполнении его очереди событие для него отбрасывается.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MeasurementEvent:
    sensor_id: int
    topic: str
    value: float
    at: datetime


@dataclass(frozen=True, slots=True)
class IncidentEvent:
    """Переход жизненного цикла инцидента; несёт всё, что нужно
    для формирования оповещения без похода в БД."""

    kind: str  # opened | escalated | resolved
    incident_id: int
    sensor_id: int
    sensor_alias: str
    controller_id: int
    controller_name: str
    type: str  # IncidentType
    severity: str  # IncidentSeverity
    value: float | None
    opened_at: datetime
    closed_at: datetime | None = None
    peak_value: float | None = None


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: object) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = EventBus()
