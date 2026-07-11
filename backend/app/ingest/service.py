"""Приём измерений: валидация, привязка к датчикам, буферизованная запись.

Один экземпляр на процесс. MQTT-клиент вызывает handle_message() на каждое
сообщение; фоновая задача периодически сбрасывает буферы в БД одной пачкой.
Неизвестные топики копятся в очереди обнаружения (discovered_topic).
"""

import asyncio
import logging
import time as monotonic_time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.bus import EventBus, MeasurementEvent
from app.core.config import Settings
from app.ingest.validator import PayloadError, parse_temperature, validate_topic
from app.models import DiscoveredTopic, DiscoveredTopicStatus, Measurement, Sensor, SensorStatus

logger = logging.getLogger(__name__)

SENSOR_CACHE_TTL_S = 30.0


@dataclass(slots=True)
class _CachedSensor:
    sensor_id: int | None  # None: топик не принадлежит активному датчику
    cached_at: float


@dataclass(slots=True)
class _DiscoveryUpdate:
    first_seen: datetime
    last_seen: datetime
    message_count: int = 0
    last_value: float | None = None
    error_count: int = 0
    last_error: str | None = None


@dataclass(slots=True)
class IngestStats:
    received: int = 0
    stored: int = 0
    invalid: int = 0
    unknown_topic: int = 0
    last_message_at: datetime | None = None
    mqtt_connected: bool = False
    buffer_size: int = 0
    extra: dict = field(default_factory=dict)


class IngestService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._bus = bus
        self._sensor_cache: dict[str, _CachedSensor] = {}
        self._measurement_buffer: list[dict] = []
        self._discovery_buffer: dict[str, _DiscoveryUpdate] = {}
        self._flush_task: asyncio.Task | None = None
        self._stopping = False
        self.stats = IngestStats()

    # ---------- обработка входящих сообщений ----------

    async def handle_message(self, topic: str, payload: bytes) -> None:
        now = datetime.now(UTC)
        self.stats.received += 1
        self.stats.last_message_at = now

        try:
            validate_topic(topic)
        except PayloadError:
            self.stats.invalid += 1
            return

        sensor_id = await self._resolve_sensor(topic)

        try:
            value = parse_temperature(
                payload, self._settings.plausible_min_c, self._settings.plausible_max_c
            )
        except PayloadError as exc:
            self.stats.invalid += 1
            if sensor_id is None:
                self._record_discovery(topic, now, error=str(exc))
            else:
                logger.warning("Invalid payload from bound sensor topic %r: %s", topic, exc)
            return

        if sensor_id is None:
            self.stats.unknown_topic += 1
            self._record_discovery(topic, now, value=value)
            return

        self._measurement_buffer.append({"sensor_id": sensor_id, "time": now, "value": value})
        self.stats.buffer_size = len(self._measurement_buffer)
        self._bus.publish(MeasurementEvent(sensor_id=sensor_id, topic=topic, value=value, at=now))

        if len(self._measurement_buffer) >= self._settings.ingest_max_buffer:
            await self.flush()

    async def _resolve_sensor(self, topic: str) -> int | None:
        cached = self._sensor_cache.get(topic)
        if cached is not None:
            if monotonic_time.monotonic() - cached.cached_at < SENSOR_CACHE_TTL_S:
                return cached.sensor_id

        async with self._session_factory() as session:
            # paused: измерения продолжаем писать (журнал не должен прерываться),
            # archived: топик считается «ничьим» и снова виден в обнаружении
            row = await session.execute(
                select(Sensor.id).where(
                    Sensor.mqtt_topic == topic, Sensor.status != SensorStatus.ARCHIVED
                )
            )
            result = row.first()

        sensor_id = result.id if result is not None else None
        self._sensor_cache[topic] = _CachedSensor(
            sensor_id=sensor_id, cached_at=monotonic_time.monotonic()
        )
        return sensor_id

    def invalidate_sensor_cache(self) -> None:
        """Вызывается API после создания/изменения датчиков."""
        self._sensor_cache.clear()

    def _record_discovery(
        self,
        topic: str,
        now: datetime,
        value: float | None = None,
        error: str | None = None,
    ) -> None:
        update = self._discovery_buffer.get(topic)
        if update is None:
            update = _DiscoveryUpdate(first_seen=now, last_seen=now)
            self._discovery_buffer[topic] = update
        update.last_seen = now
        if error is None:
            update.message_count += 1
            update.last_value = value
        else:
            update.error_count += 1
            update.last_error = error[:500]

    # ---------- фоновая запись ----------

    async def start(self) -> None:
        self._stopping = False
        self._flush_task = asyncio.create_task(self._flush_loop(), name="ingest-flush")

    async def stop(self) -> None:
        self._stopping = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()

    async def _flush_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._settings.ingest_flush_interval_s)
            try:
                await self.flush()
            except Exception:
                # буферы не очищены — данные будут записаны следующей попыткой
                logger.exception("Ingest flush failed; keeping buffers for retry")

    async def flush(self) -> None:
        measurements, self._measurement_buffer = self._measurement_buffer, []
        discoveries, self._discovery_buffer = self._discovery_buffer, {}
        self.stats.buffer_size = 0
        if not measurements and not discoveries:
            return

        try:
            async with self._session_factory() as session:
                if measurements:
                    await self._insert_measurements(session, measurements)
                for topic, update in discoveries.items():
                    await self._upsert_discovery(session, topic, update)
                await session.commit()
            self.stats.stored += len(measurements)
        except Exception:
            # возвращаем данные в буфер, чтобы не потерять их из-за сбоя БД
            self._measurement_buffer = measurements + self._measurement_buffer
            for topic, update in discoveries.items():
                if topic not in self._discovery_buffer:
                    self._discovery_buffer[topic] = update
            self.stats.buffer_size = len(self._measurement_buffer)
            raise

    async def _insert_measurements(self, session: AsyncSession, rows: list[dict]) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert  # type: ignore[assignment]
        stmt = insert(Measurement).on_conflict_do_nothing(
            index_elements=["sensor_id", "time"]
        )
        await session.execute(stmt, rows)

    async def _upsert_discovery(
        self, session: AsyncSession, topic: str, update: _DiscoveryUpdate
    ) -> None:
        row = await session.execute(select(DiscoveredTopic).where(DiscoveredTopic.topic == topic))
        existing = row.scalar_one_or_none()
        if existing is None:
            session.add(
                DiscoveredTopic(
                    topic=topic,
                    status=DiscoveredTopicStatus.NEW,
                    first_seen=update.first_seen,
                    last_seen=update.last_seen,
                    message_count=update.message_count,
                    last_value=update.last_value,
                    error_count=update.error_count,
                    last_error=update.last_error,
                )
            )
        else:
            existing.last_seen = update.last_seen
            existing.message_count += update.message_count
            existing.error_count += update.error_count
            if update.last_value is not None:
                existing.last_value = update.last_value
            if update.last_error is not None:
                existing.last_error = update.last_error
            # архивированный датчик освободил топик — тот снова виден как новый;
            # но если топик привязан к живому датчику (сообщения попали в буфер
            # до привязки), статус bound сохраняется
            if existing.status == DiscoveredTopicStatus.BOUND:
                bound_sensor = await session.execute(
                    select(Sensor.id).where(
                        Sensor.mqtt_topic == topic, Sensor.status != SensorStatus.ARCHIVED
                    )
                )
                if bound_sensor.first() is None:
                    existing.status = DiscoveredTopicStatus.NEW
