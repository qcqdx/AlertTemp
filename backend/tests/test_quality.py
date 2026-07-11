"""Качество хранения: MKT, время вне диапазона, бюджет стабильности."""

from datetime import UTC, datetime, timedelta

from app.api.routes.quality import mean_kinetic_temperature
from app.models import Controller, Measurement, Sensor, ThresholdProfile

# ---------- математика MKT ----------


def test_mkt_of_constant_temperature_is_that_temperature():
    assert abs(mean_kinetic_temperature([5.0] * 100, 10000.0) - 5.0) < 0.001


def test_mkt_weighs_excursions_higher_than_mean():
    """Короткий перегрев поднимает MKT сильнее арифметического среднего —
    в этом смысл метрики для препаратов."""
    temps = [5.0] * 90 + [15.0] * 10
    mean = sum(temps) / len(temps)
    mkt = mean_kinetic_temperature(temps, 10000.0)
    assert mkt > mean
    assert mkt < 15.0


def test_mkt_empty_is_none():
    assert mean_kinetic_temperature([], 10000.0) is None


# ---------- endpoint ----------


async def seed_quality_data(session_factory, with_budget=True):
    """24 часа с 4 часами перегрева (10°C), остальное 5°C; минутные точки."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with session_factory() as session:
        controller = Controller(name="Холодильник")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/q", alias="Полка", position=1)
        session.add(sensor)
        await session.flush()
        session.add(
            ThresholdProfile(
                sensor_id=sensor.id,
                version=1,
                active=True,
                warn_low=2.0,
                warn_high=8.0,
                stability_budget_h=8.0 if with_budget else None,
            )
        )
        # 12 часов данных: минутные точки; первые 4 часа — перегрев 10°C
        for minute in range(12 * 60):
            at = now - timedelta(hours=12) + timedelta(minutes=minute)
            value = 10.0 if minute < 4 * 60 else 5.0
            session.add(Measurement(sensor_id=sensor.id, time=at, value=value))
        await session.commit()
        return controller.id, sensor.id


async def test_quality_endpoint(client, session_factory):
    controller_id, sensor_id = await seed_quality_data(session_factory)

    response = await client.get(f"/api/v1/controllers/{controller_id}/quality?window=24h")
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "24h"
    quality = body["sensors"][0]

    assert quality["sensor_id"] == sensor_id
    assert quality["minutes_with_data"] == 12 * 60
    # 12 часов данных в 24-часовом окне = покрытие ~0.5 (слепая зона честно видна)
    assert abs(quality["coverage"] - 0.5) < 0.01

    # 4 часа перегрева
    assert quality["out_above_s"] == 4 * 3600
    assert quality["out_below_s"] == 0
    # бюджет 8 ч, израсходовано 4 ч = 0.5
    assert abs(quality["budget_used"] - 0.5) < 0.01

    # MKT выше среднего из-за экскурсии, но в физичных пределах
    assert quality["avg"] < quality["mkt"] < 10.0
    assert quality["min"] == 5.0
    assert quality["max"] == 10.0


async def test_quality_without_thresholds(client, session_factory):
    async with session_factory() as session:
        controller = Controller(name="Без порогов")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/n", alias="X", position=1)
        session.add(sensor)
        await session.flush()
        session.add(
            Measurement(sensor_id=sensor.id, time=datetime.now(UTC), value=5.0)
        )
        await session.commit()
        controller_id = controller.id

    quality = (
        (await client.get(f"/api/v1/controllers/{controller_id}/quality")).json()["sensors"][0]
    )
    assert quality["out_total_s"] is None  # порогов нет — «вне диапазона» не определено
    assert quality["budget_used"] is None
    assert quality["mkt"] is not None  # MKT считается всегда


async def test_quality_empty_sensor(client, session_factory):
    async with session_factory() as session:
        controller = Controller(name="Пустой")
        session.add(controller)
        await session.flush()
        session.add(
            Sensor(controller_id=controller.id, mqtt_topic="t/e", alias="E", position=1)
        )
        await session.commit()
        controller_id = controller.id

    quality = (
        (await client.get(f"/api/v1/controllers/{controller_id}/quality")).json()["sensors"][0]
    )
    assert quality["samples"] == 0
    assert quality["coverage"] == 0
    assert quality["mkt"] is None


# ---------- confirm_value (заметка стенда по инциденту 34) ----------


async def test_open_and_confirm_values_distinct(session_factory, settings):
    """open_value — значение в момент выхода за порог; confirm_value — в момент
    фиксации после задержки. На стенде инцидент 34: 8.06 в начале, 9.44 при
    фиксации — раньше хранилось только второе под именем первого."""
    from sqlalchemy import select

    from tests.test_rule_engine import T0, EngineHarness, make_sensor_with_thresholds

    sensor_id = await make_sensor_with_thresholds(session_factory, warn_delay_s=300)
    harness = EngineHarness(session_factory, settings)

    await harness.feed(sensor_id, 8.06, T0)  # выход за порог
    await harness.feed(sensor_id, 9.0, T0 + timedelta(seconds=150))
    await harness.feed(sensor_id, 9.44, T0 + timedelta(seconds=300))  # фиксация

    from app.models import Incident

    async with session_factory() as session:
        incident = (await session.execute(select(Incident))).scalar_one()
    assert incident.opened_at == T0
    assert incident.open_value == 8.06
    assert incident.confirm_value == 9.44
    assert incident.peak_value == 9.44
