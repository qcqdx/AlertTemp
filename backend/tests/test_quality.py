"""Качество хранения: MKT, время вне диапазона, бюджет стабильности."""

from datetime import UTC, datetime, timedelta

from app.api.routes.quality import mean_kinetic_temperature
from app.models import (
    Controller,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    Measurement,
    Sensor,
    ThresholdProfile,
)

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
                # профиль действует с начала данных: historical-расчёты
                # (отчёт) считают нарушения только с момента активации порогов
                created_at=now - timedelta(hours=13),
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


# ---------- печатная форма (эскиз, приёмка стенда — после среза №2) ----------


async def test_report_endpoint(client, session_factory):
    """Отчёт: historical-семантика, эпохи профилей, слепые зоны, шапка."""
    controller_id, sensor_id = await seed_quality_data(session_factory)

    # '+00:00' в query превращается в пробел — используем Z-суффикс
    start = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = await client.get(
        f"/api/v1/controllers/{controller_id}/report?start={start}"
    )
    assert response.status_code == 200
    report = response.json()

    assert report["basis"] == "historical"
    assert report["generated_by"] == "admin"
    assert "не означает" in report["metric_note"]

    sensor = report["sensors"][0]
    assert sensor["sensor_id"] == sensor_id
    # эпоха профиля с границами действия
    assert sensor["profiles"][0]["warn_high"] == 8.0
    # 4 часа перегрева по действовавшему профилю
    assert sensor["out_above_s"] == 4 * 3600
    # слепые зоны: данных 12 ч из 24 — разрывы указаны явно
    assert sensor["gaps"], "ожидались слепые зоны"
    total_gap = sum(g["duration_s"] for g in sensor["gaps"])
    assert total_gap > 11 * 3600  # ~12 часов без данных

    assert sensor["mkt"] is not None


async def test_report_validation(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)
    now = datetime.now(UTC)
    # период больше лимита
    response = await client.get(
        f"/api/v1/controllers/{controller_id}/report"
        f"?start={(now - timedelta(days=200)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    assert response.status_code == 422


# ---------- отчёт по экскурсии (цикл I, п.3) ----------


async def seed_report_incident(
    session_factory, controller_id, sensor_id, opened_ago_h=6.0, closed_ago_h=4.0
):
    """Инцидент внутри окна данных seed_quality_data; closed_ago_h=None — открыт."""
    now = datetime.now(UTC)
    opened_at = now - timedelta(hours=opened_ago_h)
    closed_at = now - timedelta(hours=closed_ago_h) if closed_ago_h is not None else None
    async with session_factory() as session:
        incident = Incident(
            sensor_id=sensor_id,
            controller_id=controller_id,
            type=IncidentType.OVERHEAT,
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.RESOLVED if closed_at else IncidentStatus.OPEN,
            opened_at=opened_at,
            closed_at=closed_at,
            open_value=8.6,
            peak_value=10.0,
        )
        session.add(incident)
        await session.commit()
        return incident.id, opened_at, closed_at


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_incident_report_period(client, session_factory):
    """Период отчёта по экскурсии: [opened_at − 1 ч, closed_at + 1 ч]."""
    controller_id, sensor_id = await seed_quality_data(session_factory)
    incident_id, opened_at, closed_at = await seed_report_incident(
        session_factory, controller_id, sensor_id
    )

    response = await client.get(f"/api/v1/incidents/{incident_id}/report")
    assert response.status_code == 200
    report = response.json()

    assert report["controller_id"] == controller_id
    assert _dt(report["period_start"]) == opened_at - timedelta(hours=1)
    assert _dt(report["period_end"]) == closed_at + timedelta(hours=1)
    assert report["basis"] == "historical"

    # сам инцидент присутствует в форме
    sensor = report["sensors"][0]
    assert any(i["id"] == incident_id for i in sensor["incidents"])
    assert sensor["samples"] > 0  # данные периода подтянуты


async def test_incident_report_open_incident_ends_now(client, session_factory):
    """Незакрытый инцидент: конец периода — текущий момент (не в будущем)."""
    controller_id, sensor_id = await seed_quality_data(session_factory)
    incident_id, opened_at, _ = await seed_report_incident(
        session_factory, controller_id, sensor_id, opened_ago_h=2.0, closed_ago_h=None
    )

    report = (await client.get(f"/api/v1/incidents/{incident_id}/report")).json()
    period_end = _dt(report["period_end"])
    assert period_end <= datetime.now(UTC)
    assert period_end > opened_at


async def test_incident_report_not_found(client):
    assert (await client.get("/api/v1/incidents/99999/report")).status_code == 404


# ---------- CSV-выгрузки (цикл I, п.4) ----------


async def test_report_csv_export(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)
    start = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    response = await client.get(
        f"/api/v1/controllers/{controller_id}/report.csv?start={start}"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    text = response.text
    assert text.startswith("﻿")  # BOM: Excel открывает UTF-8 сразу
    lines = text.lstrip("﻿").splitlines()
    assert lines[0].startswith("Объект;")
    header = next(line for line in lines if line.startswith("Датчик;"))
    assert "MKT" in header and "Слепых зон" in header
    assert any(line.startswith("Полка;") for line in lines)


async def test_incidents_journal_csv_export(client, session_factory):
    controller_id, sensor_id = await seed_report_incident_pair(session_factory)

    response = await client.get("/api/v1/incidents/export.csv")
    assert response.status_code == 200
    text = response.text
    assert text.startswith("﻿")
    lines = text.lstrip("﻿").splitlines()
    assert lines[0].split(";")[:3] == ["id", "Холодильник", "Датчик"]
    assert len(lines) == 3  # заголовок + 2 инцидента

    # фильтры — те же, что у списка: закрытый отфильтровывается по status=open
    filtered = (await client.get("/api/v1/incidents/export.csv?status=open")).text
    filtered_lines = filtered.lstrip("﻿").splitlines()
    assert len(filtered_lines) == 2
    assert ";open;" in filtered_lines[1]

    # фильтр по контроллеру
    empty = (
        await client.get(f"/api/v1/incidents/export.csv?controller_id={controller_id + 1}")
    ).text
    assert len(empty.lstrip("﻿").splitlines()) == 1  # только заголовок


async def seed_report_incident_pair(session_factory):
    """Контроллер + датчик + один закрытый и один открытый инцидент."""
    controller_id, sensor_id = await seed_quality_data(session_factory)
    await seed_report_incident(session_factory, controller_id, sensor_id)
    await seed_report_incident(
        session_factory, controller_id, sensor_id, opened_ago_h=2.0, closed_ago_h=None
    )
    return controller_id, sensor_id
