"""Rule engine: детекция нарушений температурного режима и молчания датчиков.

Слушает поток измерений из внутренней шины, ведёт state machine по каждому
датчику (гистерезис + минимальная длительность нарушения), открывает/эскалирует/
закрывает инциденты и публикует IncidentEvent для подсистемы оповещений.

Особенности железа, определившие дизайн:
- прошивка шлёт только измерения (~1 Гц), heartbeat отсутствует — offline
  детектируется исключительно по таймауту тишины и означает «датчик, контроллер
  или сеть мертвы», различить причины по данным невозможно;
- контроллеры не буферизуют: тишина = слепая зона, поэтому offline — critical.

Модель детекции — СОБЫТИЙНАЯ: термические переходы вычисляются только в момент
прихода измерения. Правка порогов у молчащего датчика не откроет и не закроет
термический инцидент, пока не придёт следующее измерение (при штатном темпе
~1 Гц окно не превышает секунд; при полной тишине срабатывает отдельный
offline-детектор). Это осознанное решение: пересчёт по устаревшему значению
для системы холодовой цепи опаснее короткой задержки.
"""

import asyncio
import enum
import logging
import time as monotonic_time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.bus import EventBus, IncidentEvent, MeasurementEvent
from app.core.config import Settings
from app.models import (
    Controller,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    Measurement,
    Sensor,
    SensorRuntimeState,
    SensorStatus,
    ThresholdProfile,
)

logger = logging.getLogger(__name__)

INFO_CACHE_TTL_S = 30.0


class Zone(enum.StrEnum):
    NORMAL = "normal"
    WARN_HIGH = "warn_high"
    CRIT_HIGH = "crit_high"
    WARN_LOW = "warn_low"
    CRIT_LOW = "crit_low"


_SIDE: dict[Zone, str | None] = {
    Zone.NORMAL: None,
    Zone.WARN_HIGH: "high",
    Zone.CRIT_HIGH: "high",
    Zone.WARN_LOW: "low",
    Zone.CRIT_LOW: "low",
}
_RANK: dict[Zone, int] = {
    Zone.NORMAL: 0,
    Zone.WARN_HIGH: 1,
    Zone.WARN_LOW: 1,
    Zone.CRIT_HIGH: 2,
    Zone.CRIT_LOW: 2,
}


@dataclass(slots=True)
class _Thresholds:
    profile_id: int
    warn_low: float
    warn_high: float
    crit_low: float | None
    crit_high: float | None
    hysteresis: float
    warn_delay_s: int
    crit_delay_s: int


@dataclass(slots=True)
class _SensorInfo:
    sensor_id: int
    alias: str
    controller_id: int
    controller_name: str
    status: SensorStatus
    heartbeat_timeout_s: int
    thresholds: _Thresholds | None
    cached_at: float


@dataclass(slots=True)
class _SensorState:
    zone: Zone = Zone.NORMAL
    pending_zone: Zone | None = None
    pending_since: datetime | None = None
    incident_id: int | None = None
    incident_side: str | None = None
    incident_severity: IncidentSeverity | None = None
    incident_opened_at: datetime | None = None
    pending_first_value: float | None = None
    peak_value: float | None = None
    last_seen: datetime | None = None
    offline_incident_id: int | None = None


class RuleEngine:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        bus: EventBus,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._bus = bus
        self._now = now or (lambda: datetime.now(UTC))
        self._states: dict[int, _SensorState] = {}
        self._info_cache: dict[int, _SensorInfo] = {}
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue | None = None
        self._stopping = False
        self._started_at: datetime | None = None

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        self._stopping = False
        self._started_at = self._now()
        await self._restore_state()
        self._queue = self._bus.subscribe()
        self._tasks = [
            asyncio.create_task(self._consume_loop(), name="rules-consume"),
            asyncio.create_task(self._offline_loop(), name="rules-offline"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    def invalidate_sensor(self, sensor_id: int | None = None) -> None:
        """Вызывается API при изменении датчиков или порогов."""
        if sensor_id is None:
            self._info_cache.clear()
        else:
            self._info_cache.pop(sensor_id, None)

    async def _restore_state(self) -> None:
        """Восстановление после рестарта: зоны, открытые инциденты, last_seen."""
        async with self._session_factory() as session:
            rows = await session.execute(select(SensorRuntimeState))
            for row in rows.scalars():
                try:
                    zone = Zone(row.state)
                except ValueError:
                    zone = Zone.NORMAL
                self._states[row.sensor_id] = _SensorState(zone=zone)

            incidents = await session.execute(
                select(Incident).where(Incident.status != IncidentStatus.RESOLVED)
            )
            for incident in incidents.scalars():
                state = self._states.setdefault(incident.sensor_id, _SensorState())
                if incident.type == IncidentType.OFFLINE:
                    state.offline_incident_id = incident.id
                    continue
                state.incident_id = incident.id
                state.incident_side = "high" if incident.type == IncidentType.OVERHEAT else "low"
                state.incident_severity = incident.severity
                state.incident_opened_at = incident.opened_at
                # пик пересчитываем по фактическим данным за окно инцидента:
                # in-memory пик не переживает рестарт
                aggregate = func.max if state.incident_side == "high" else func.min
                peak = await session.execute(
                    select(aggregate(Measurement.value)).where(
                        Measurement.sensor_id == incident.sensor_id,
                        Measurement.time >= incident.opened_at,
                    )
                )
                state.peak_value = peak.scalar()
                if state.peak_value is None:
                    state.peak_value = incident.peak_value

            sensors = await session.execute(
                select(Sensor.id).where(Sensor.status != SensorStatus.ARCHIVED)
            )
            for (sensor_id,) in sensors:
                state = self._states.setdefault(sensor_id, _SensorState())
                last = await session.execute(
                    select(Measurement.time)
                    .where(Measurement.sensor_id == sensor_id)
                    .order_by(Measurement.time.desc())
                    .limit(1)
                )
                seen = last.scalar()
                if seen is not None and seen.tzinfo is None:
                    seen = seen.replace(tzinfo=UTC)
                state.last_seen = seen

    # ---------- поток измерений ----------

    async def _consume_loop(self) -> None:
        assert self._queue is not None
        while not self._stopping:
            event = await self._queue.get()
            if not isinstance(event, MeasurementEvent):
                continue
            try:
                await self.process_measurement(event)
            except Exception:
                logger.exception("Rule engine failed on measurement %s", event)

    async def process_measurement(self, event: MeasurementEvent) -> None:
        state = self._states.setdefault(event.sensor_id, _SensorState())
        state.last_seen = event.at

        info = await self._get_info(event.sensor_id)
        if info is None:
            return

        if state.offline_incident_id is not None:
            await self._resolve_offline(state, info, event.at)

        if info.status != SensorStatus.ACTIVE:
            # paused: журнал пишется (ingest), детекция порогов отключена
            return
        thresholds = info.thresholds
        if thresholds is None:
            return  # пороги не заданы — только offline-контроль

        # пик отслеживается по каждому измерению, пока инцидент открыт
        if state.incident_id is not None:
            if state.incident_side == "high":
                state.peak_value = max(state.peak_value or event.value, event.value)
            else:
                state.peak_value = min(state.peak_value or event.value, event.value)

        zone = self._zone(event.value, thresholds, state.zone)
        if zone == state.zone:
            state.pending_zone = None
            state.pending_since = None
            return

        if state.pending_zone != zone:
            state.pending_zone = zone
            state.pending_since = event.at
            state.pending_first_value = event.value

        # эскалация ждёт свою задержку, деэскалация и возврат в норму — мгновенны
        # (дребезг уже отсечён гистерезисом)
        if _RANK[zone] > _RANK[state.zone]:
            delay = thresholds.crit_delay_s if _RANK[zone] == 2 else thresholds.warn_delay_s
        else:
            delay = 0
        assert state.pending_since is not None
        if (event.at - state.pending_since).total_seconds() >= delay:
            await self._commit_transition(state, zone, event, info, thresholds)
            state.pending_zone = None
            state.pending_since = None

    def _zone(self, value: float, thr: _Thresholds, current: Zone) -> Zone:
        """Зона с гистерезисом: сдвигаются только границы выхода из текущего
        состояния, границы эскалации остаются на месте."""
        warn_high, crit_high = thr.warn_high, thr.crit_high
        warn_low, crit_low = thr.warn_low, thr.crit_low
        h = thr.hysteresis
        if current == Zone.WARN_HIGH:
            warn_high -= h
        elif current == Zone.CRIT_HIGH:
            warn_high -= h
            if crit_high is not None:
                crit_high -= h
        elif current == Zone.WARN_LOW:
            warn_low += h
        elif current == Zone.CRIT_LOW:
            warn_low += h
            if crit_low is not None:
                crit_low += h

        if crit_high is not None and value > crit_high:
            return Zone.CRIT_HIGH
        if value > warn_high:
            return Zone.WARN_HIGH
        if crit_low is not None and value < crit_low:
            return Zone.CRIT_LOW
        if value < warn_low:
            return Zone.WARN_LOW
        return Zone.NORMAL

    async def _commit_transition(
        self,
        state: _SensorState,
        zone: Zone,
        event: MeasurementEvent,
        info: _SensorInfo,
        thresholds: _Thresholds,
    ) -> None:
        new_side = _SIDE[zone]
        async with self._session_factory() as session:
            # закрытие эпизода: возврат в норму или скачок на другую сторону
            if state.incident_id is not None and state.incident_side != new_side:
                await self._resolve_incident_row(session, state, event.at, info, event.value)

            if zone != Zone.NORMAL:
                severity = (
                    IncidentSeverity.CRITICAL if _RANK[zone] == 2 else IncidentSeverity.WARNING
                )
                if state.incident_id is None:
                    open_value = (
                        state.pending_first_value
                        if state.pending_first_value is not None
                        else event.value
                    )
                    worst = max if new_side == "high" else min
                    incident = Incident(
                        sensor_id=info.sensor_id,
                        controller_id=info.controller_id,
                        type=IncidentType.OVERHEAT if new_side == "high" else IncidentType.OVERCOOL,
                        severity=severity,
                        status=IncidentStatus.OPEN,
                        opened_at=state.pending_since or event.at,
                        open_value=open_value,
                        confirm_value=event.value,
                        peak_value=worst(open_value, event.value),
                        threshold_profile_id=thresholds.profile_id,
                    )
                    session.add(incident)
                    await session.flush()
                    state.incident_id = incident.id
                    state.incident_side = new_side
                    state.incident_severity = severity
                    state.incident_opened_at = incident.opened_at
                    state.peak_value = incident.peak_value
                    self._emit("opened", incident.id, info, incident.type, severity, event)
                elif (
                    severity == IncidentSeverity.CRITICAL
                    and state.incident_severity == IncidentSeverity.WARNING
                ):
                    # эскалация: severity только растёт, инцидент тот же
                    incident = await session.get(Incident, state.incident_id)
                    if incident is not None:
                        incident.severity = IncidentSeverity.CRITICAL
                        incident.peak_value = state.peak_value
                        state.incident_severity = IncidentSeverity.CRITICAL
                        self._emit(
                            "escalated", incident.id, info, incident.type, incident.severity, event
                        )

            await self._persist_zone(session, info.sensor_id, zone)
            await session.commit()
        state.zone = zone

    async def _resolve_incident_row(
        self,
        session: AsyncSession,
        state: _SensorState,
        at: datetime,
        info: _SensorInfo,
        value: float | None,
    ) -> None:
        incident = await session.get(Incident, state.incident_id)
        if incident is not None:
            incident.status = IncidentStatus.RESOLVED
            incident.closed_at = at
            incident.peak_value = state.peak_value
            self._emit(
                "resolved",
                incident.id,
                info,
                incident.type,
                incident.severity,
                None,
                value=value,
                opened_at=incident.opened_at,
                closed_at=at,
                peak_value=state.peak_value,
            )
        state.incident_id = None
        state.incident_side = None
        state.incident_severity = None
        state.incident_opened_at = None
        state.peak_value = None

    # ---------- offline ----------

    async def _offline_loop(self) -> None:
        interval = self._settings.offline_check_interval_s
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                await self.check_offline()
            except Exception:
                logger.exception("Offline check failed")

    async def check_offline(self) -> None:
        now = self._now()
        async with self._session_factory() as session:
            sensors = await session.execute(
                select(Sensor.id, Sensor.heartbeat_timeout_s).where(
                    Sensor.status == SensorStatus.ACTIVE
                )
            )
            rows = sensors.all()

        for sensor_id, timeout_s in rows:
            state = self._states.setdefault(sensor_id, _SensorState())
            if state.offline_incident_id is not None:
                continue
            last = state.last_seen or self._started_at or now
            if (now - last).total_seconds() <= timeout_s:
                continue
            info = await self._get_info(sensor_id)
            if info is None:
                continue
            async with self._session_factory() as session:
                incident = Incident(
                    sensor_id=sensor_id,
                    controller_id=info.controller_id,
                    type=IncidentType.OFFLINE,
                    # тишина = слепая зона холодовой цепи, поэтому critical
                    severity=IncidentSeverity.CRITICAL,
                    status=IncidentStatus.OPEN,
                    opened_at=last,  # начало слепой зоны — последнее живое сообщение
                )
                session.add(incident)
                await session.commit()
                state.offline_incident_id = incident.id
            self._bus.publish(
                IncidentEvent(
                    kind="opened",
                    incident_id=state.offline_incident_id,
                    sensor_id=sensor_id,
                    sensor_alias=info.alias,
                    controller_id=info.controller_id,
                    controller_name=info.controller_name,
                    type=IncidentType.OFFLINE,
                    severity=IncidentSeverity.CRITICAL,
                    value=None,
                    opened_at=last,
                )
            )

    async def _resolve_offline(self, state: _SensorState, info: _SensorInfo, at: datetime) -> None:
        async with self._session_factory() as session:
            incident = await session.get(Incident, state.offline_incident_id)
            if incident is not None:
                incident.status = IncidentStatus.RESOLVED
                incident.closed_at = at
                await session.commit()
                self._bus.publish(
                    IncidentEvent(
                        kind="resolved",
                        incident_id=incident.id,
                        sensor_id=info.sensor_id,
                        sensor_alias=info.alias,
                        controller_id=info.controller_id,
                        controller_name=info.controller_name,
                        type=IncidentType.OFFLINE,
                        severity=incident.severity,
                        value=None,
                        opened_at=incident.opened_at,
                        closed_at=at,
                    )
                )
        state.offline_incident_id = None

    # ---------- вспомогательное ----------

    async def _get_info(self, sensor_id: int) -> _SensorInfo | None:
        cached = self._info_cache.get(sensor_id)
        if cached is not None and monotonic_time.monotonic() - cached.cached_at < INFO_CACHE_TTL_S:
            return cached

        async with self._session_factory() as session:
            row = await session.execute(
                select(Sensor, Controller.name)
                .join(Controller, Controller.id == Sensor.controller_id)
                .where(Sensor.id == sensor_id)
            )
            result = row.first()
            if result is None or result[0].status == SensorStatus.ARCHIVED:
                self._info_cache.pop(sensor_id, None)
                return None
            sensor, controller_name = result

            profile_row = await session.execute(
                select(ThresholdProfile).where(
                    ThresholdProfile.sensor_id == sensor_id,
                    ThresholdProfile.active.is_(True),
                )
            )
            profile = profile_row.scalar_one_or_none()

        thresholds = None
        if profile is not None:
            thresholds = _Thresholds(
                profile_id=profile.id,
                warn_low=profile.warn_low,
                warn_high=profile.warn_high,
                crit_low=profile.crit_low,
                crit_high=profile.crit_high,
                hysteresis=profile.hysteresis,
                warn_delay_s=profile.warn_delay_s,
                crit_delay_s=profile.crit_delay_s,
            )
        info = _SensorInfo(
            sensor_id=sensor_id,
            alias=sensor.alias,
            controller_id=sensor.controller_id,
            controller_name=controller_name,
            status=sensor.status,
            heartbeat_timeout_s=sensor.heartbeat_timeout_s,
            thresholds=thresholds,
            cached_at=monotonic_time.monotonic(),
        )
        self._info_cache[sensor_id] = info
        return info

    async def _persist_zone(self, session: AsyncSession, sensor_id: int, zone: Zone) -> None:
        existing = await session.get(SensorRuntimeState, sensor_id)
        if existing is None:
            session.add(SensorRuntimeState(sensor_id=sensor_id, state=zone.value))
        else:
            existing.state = zone.value

    def _emit(
        self,
        kind: str,
        incident_id: int,
        info: _SensorInfo,
        incident_type: IncidentType,
        severity: IncidentSeverity,
        event: MeasurementEvent | None,
        value: float | None = None,
        opened_at: datetime | None = None,
        closed_at: datetime | None = None,
        peak_value: float | None = None,
    ) -> None:
        state = self._states.get(info.sensor_id)
        if opened_at is None:
            opened_at = (
                state.incident_opened_at if state and state.incident_opened_at else self._now()
            )
        if peak_value is None and state is not None:
            peak_value = state.peak_value
        self._bus.publish(
            IncidentEvent(
                kind=kind,
                incident_id=incident_id,
                sensor_id=info.sensor_id,
                sensor_alias=info.alias,
                controller_id=info.controller_id,
                controller_name=info.controller_name,
                type=incident_type,
                severity=severity,
                value=value if value is not None else (event.value if event else None),
                opened_at=opened_at,
                closed_at=closed_at,
                peak_value=peak_value,
            )
        )
