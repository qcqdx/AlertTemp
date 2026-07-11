"""Тесты state machine детекции: гистерезис, задержки, эскалация, offline.

Время полностью управляемое: измерения подаются с явными метками event.at,
offline-проверки — с инжектированным now().
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.bus import EventBus, IncidentEvent, MeasurementEvent
from app.models import (
    Controller,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    Sensor,
    SensorStatus,
    ThresholdProfile,
)
from app.rules.engine import RuleEngine, Zone

T0 = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


async def make_sensor_with_thresholds(
    session_factory,
    warn_low=2.0,
    warn_high=8.0,
    crit_low=-0.5,
    crit_high=15.0,
    hysteresis=0.3,
    warn_delay_s=300,
    crit_delay_s=0,
    status=SensorStatus.ACTIVE,
    heartbeat_timeout_s=120,
) -> int:
    async with session_factory() as session:
        controller = Controller(name="Холодильник аптеки")
        session.add(controller)
        await session.flush()
        sensor = Sensor(
            controller_id=controller.id,
            mqtt_topic="temp/28ff01",
            alias="Верхняя полка",
            position=1,
            status=status,
            heartbeat_timeout_s=heartbeat_timeout_s,
        )
        session.add(sensor)
        await session.flush()
        session.add(
            ThresholdProfile(
                sensor_id=sensor.id,
                version=1,
                active=True,
                warn_low=warn_low,
                warn_high=warn_high,
                crit_low=crit_low,
                crit_high=crit_high,
                hysteresis=hysteresis,
                warn_delay_s=warn_delay_s,
                crit_delay_s=crit_delay_s,
            )
        )
        await session.commit()
        return sensor.id


class EngineHarness:
    def __init__(self, session_factory, settings, now=None):
        self.bus = EventBus()
        self.events: list[IncidentEvent] = []
        self._queue = self.bus.subscribe()
        self.now = now or (lambda: T0)
        self.engine = RuleEngine(session_factory, settings, self.bus, now=lambda: self.now())

    async def feed(self, sensor_id: int, value: float, at: datetime) -> None:
        await self.engine.process_measurement(
            MeasurementEvent(sensor_id=sensor_id, topic="t", value=value, at=at)
        )
        self.drain()

    def drain(self) -> None:
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if isinstance(event, IncidentEvent):
                self.events.append(event)


@pytest.fixture
def harness(session_factory, settings):
    return EngineHarness(session_factory, settings)


async def get_incidents(session_factory) -> list[Incident]:
    async with session_factory() as session:
        rows = await session.execute(select(Incident).order_by(Incident.id))
        return list(rows.scalars().all())


async def test_short_spike_does_not_open_incident(harness, session_factory):
    """Открытая дверца: заброс короче warn_delay — не авария."""
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=300)
    await harness.feed(sensor_id, 9.5, T0)
    await harness.feed(sensor_id, 10.0, T0 + timedelta(seconds=60))
    await harness.feed(sensor_id, 5.0, T0 + timedelta(seconds=120))  # вернулось до задержки

    assert await get_incidents(session_factory) == []
    assert harness.events == []


async def test_sustained_overheat_opens_incident_with_excursion_start(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=300)
    await harness.feed(sensor_id, 9.0, T0)
    await harness.feed(sensor_id, 9.5, T0 + timedelta(seconds=150))
    assert harness.events == []  # задержка ещё не истекла
    await harness.feed(sensor_id, 9.8, T0 + timedelta(seconds=300))

    incidents = await get_incidents(session_factory)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.type == IncidentType.OVERHEAT
    assert incident.severity == IncidentSeverity.WARNING
    assert incident.opened_at == T0  # начало экскурсии, а не момент истечения задержки
    assert harness.events[0].kind == "opened"
    assert harness.events[0].sensor_alias == "Верхняя полка"


async def test_critical_opens_immediately_by_default(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, crit_delay_s=0)
    await harness.feed(sensor_id, 16.0, T0)

    incidents = await get_incidents(session_factory)
    assert len(incidents) == 1
    assert incidents[0].severity == IncidentSeverity.CRITICAL


async def test_escalation_warning_to_critical(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    await harness.feed(sensor_id, 9.0, T0)
    await harness.feed(sensor_id, 16.0, T0 + timedelta(seconds=60))

    incidents = await get_incidents(session_factory)
    assert len(incidents) == 1  # тот же эпизод, severity эскалировала
    assert incidents[0].severity == IncidentSeverity.CRITICAL
    assert [e.kind for e in harness.events] == ["opened", "escalated"]


async def test_severity_never_downgrades(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    await harness.feed(sensor_id, 16.0, T0)
    await harness.feed(sensor_id, 10.0, T0 + timedelta(seconds=60))  # ушло из crit в warn-зону

    incidents = await get_incidents(session_factory)
    assert incidents[0].severity == IncidentSeverity.CRITICAL
    assert incidents[0].status != IncidentStatus.RESOLVED
    assert [e.kind for e in harness.events] == ["opened"]  # деэскалация молчалива


async def test_hysteresis_prevents_flapping(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(
        session_factory, warn_high=8.0, hysteresis=0.3, warn_delay_s=0
    )
    await harness.feed(sensor_id, 8.5, T0)
    assert len(harness.events) == 1  # opened

    # дрейф вокруг порога: 8.0 и 7.8 выше границы выхода (7.7) — инцидент открыт
    await harness.feed(sensor_id, 8.0, T0 + timedelta(seconds=10))
    await harness.feed(sensor_id, 7.8, T0 + timedelta(seconds=20))
    await harness.feed(sensor_id, 8.4, T0 + timedelta(seconds=30))
    assert [e.kind for e in harness.events] == ["opened"]

    # уверенный возврат ниже warn_high - hysteresis закрывает эпизод
    await harness.feed(sensor_id, 7.6, T0 + timedelta(seconds=40))
    assert [e.kind for e in harness.events] == ["opened", "resolved"]

    incidents = await get_incidents(session_factory)
    assert incidents[0].status == IncidentStatus.RESOLVED
    assert incidents[0].closed_at == T0 + timedelta(seconds=40)


async def test_resolve_records_peak(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    await harness.feed(sensor_id, 9.0, T0)
    await harness.feed(sensor_id, 12.7, T0 + timedelta(seconds=30))
    await harness.feed(sensor_id, 10.0, T0 + timedelta(seconds=60))
    await harness.feed(sensor_id, 5.0, T0 + timedelta(seconds=90))

    incidents = await get_incidents(session_factory)
    assert incidents[0].peak_value == 12.7
    resolved = [e for e in harness.events if e.kind == "resolved"][0]
    assert resolved.peak_value == 12.7


async def test_overcool_side(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    await harness.feed(sensor_id, 1.0, T0)
    await harness.feed(sensor_id, -1.0, T0 + timedelta(seconds=10))

    incidents = await get_incidents(session_factory)
    assert incidents[0].type == IncidentType.OVERCOOL
    assert incidents[0].severity == IncidentSeverity.CRITICAL
    # пик переохлаждения — минимум
    await harness.feed(sensor_id, 5.0, T0 + timedelta(seconds=20))
    incidents = await get_incidents(session_factory)
    assert incidents[0].peak_value == -1.0


async def test_paused_sensor_not_detected(harness, session_factory):
    sensor_id = await make_sensor_with_thresholds(
        session_factory, status=SensorStatus.PAUSED, warn_delay_s=0
    )
    await harness.feed(sensor_id, 20.0, T0)
    assert await get_incidents(session_factory) == []


async def test_sensor_without_thresholds_ignored(harness, session_factory):
    async with session_factory() as session:
        controller = Controller(name="Без порогов")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/x", alias="X", position=1)
        session.add(sensor)
        await session.commit()
        sensor_id = sensor.id

    await harness.feed(sensor_id, 30.0, T0)
    assert await get_incidents(session_factory) == []


async def test_offline_detection_and_recovery(session_factory, settings):
    sensor_id = await make_sensor_with_thresholds(session_factory, heartbeat_timeout_s=120)
    current = {"now": T0}
    harness = EngineHarness(session_factory, settings, now=lambda: current["now"])

    await harness.feed(sensor_id, 5.0, T0)
    current["now"] = T0 + timedelta(seconds=60)
    await harness.engine.check_offline()
    harness.drain()
    assert harness.events == []  # тишина короче таймаута

    current["now"] = T0 + timedelta(seconds=180)
    await harness.engine.check_offline()
    harness.drain()

    incidents = await get_incidents(session_factory)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.type == IncidentType.OFFLINE
    assert incident.severity == IncidentSeverity.CRITICAL
    assert incident.opened_at == T0  # начало слепой зоны — последнее живое сообщение

    # повторная проверка не плодит дубликаты
    current["now"] = T0 + timedelta(seconds=240)
    await harness.engine.check_offline()
    assert len(await get_incidents(session_factory)) == 1

    # данные вернулись — offline закрывается
    await harness.feed(sensor_id, 5.0, T0 + timedelta(seconds=300))
    incidents = await get_incidents(session_factory)
    assert incidents[0].status == IncidentStatus.RESOLVED
    assert [e.kind for e in harness.events] == ["opened", "resolved"]


async def test_state_survives_restart(session_factory, settings):
    """Рестарт сервиса не теряет открытый инцидент и текущее состояние."""
    from app.models import Measurement

    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    harness = EngineHarness(session_factory, settings)
    await harness.feed(sensor_id, 9.0, T0)
    await harness.feed(sensor_id, 12.0, T0 + timedelta(seconds=30))
    # в проде измерения лежат в БД (их пишет ingest) — воспроизводим это,
    # чтобы проверить пересчёт пика при восстановлении
    async with session_factory() as session:
        session.add(Measurement(sensor_id=sensor_id, time=T0, value=9.0))
        session.add(
            Measurement(sensor_id=sensor_id, time=T0 + timedelta(seconds=30), value=12.0)
        )
        await session.commit()

    # новый экземпляр движка (=рестарт процесса)
    harness2 = EngineHarness(session_factory, settings)
    await harness2.engine._restore_state()
    state = harness2.engine._states[sensor_id]
    assert state.zone == Zone.WARN_HIGH
    assert state.incident_id is not None
    assert state.peak_value == 12.0  # пик пересчитан по измерениям за окно инцидента

    # возврат в норму после рестарта закрывает инцидент, открытый до рестарта
    await harness2.feed(sensor_id, 5.0, T0 + timedelta(seconds=60))
    incidents = await get_incidents(session_factory)
    assert incidents[0].status == IncidentStatus.RESOLVED


async def test_incident_api_ack_flow(client, session_factory, settings):
    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=0)
    harness = EngineHarness(session_factory, settings)
    await harness.feed(sensor_id, 9.0, T0)

    response = await client.get("/api/v1/incidents/open")
    incidents = response.json()
    assert len(incidents) == 1
    incident_id = incidents[0]["id"]

    response = await client.post(
        f"/api/v1/incidents/{incident_id}/ack",
        json={"acknowledged_by": "Иванова А.А.", "note": "Проверяю холодильник"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["acknowledged_by"] == "Иванова А.А."

    # повторное подтверждение отклоняется
    response = await client.post(
        f"/api/v1/incidents/{incident_id}/ack", json={"acknowledged_by": "Петров"}
    )
    assert response.status_code == 409


async def test_thresholds_api_versioning(client, session_factory, settings):
    sensor_id = await make_sensor_with_thresholds(session_factory)

    response = await client.get(f"/api/v1/sensors/{sensor_id}/thresholds")
    assert response.status_code == 200
    assert response.json()["version"] == 1

    response = await client.put(
        f"/api/v1/sensors/{sensor_id}/thresholds",
        json={"warn_low": 2, "warn_high": 8, "crit_low": -1, "crit_high": 12,
              "created_by": "Заведующая"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    history = (await client.get(f"/api/v1/sensors/{sensor_id}/thresholds/history")).json()
    assert [h["version"] for h in history] == [2, 1]
    assert [h["active"] for h in history] == [True, False]


async def test_thresholds_api_validation(client, session_factory):
    sensor_id = await make_sensor_with_thresholds(session_factory)
    response = await client.put(
        f"/api/v1/sensors/{sensor_id}/thresholds",
        json={"warn_low": 8, "warn_high": 2},
    )
    assert response.status_code == 422
