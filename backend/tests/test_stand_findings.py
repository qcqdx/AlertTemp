"""Регрессионные тесты по находкам стенда (отчёт по этапу 2) и фаза 3."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.bus import EventBus
from app.models import (
    Controller,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    Sensor,
)
from app.models.notify import NotificationRecipient
from app.notify.notifier import NotifyConfigError, TelegramNotifier, normalize_proxy_url
from tests.conftest import build_app, login_client, make_user

# ---------- P1: socks5h и изоляция канала ----------


def test_socks5h_maps_to_rdns():
    url, rdns = normalize_proxy_url("socks5h://10.10.0.1:1080")
    assert url == "socks5://10.10.0.1:1080"
    assert rdns is True

    url, rdns = normalize_proxy_url("socks5://user:pass@host:1080")
    assert url == "socks5://user:pass@host:1080"
    assert rdns is False


def test_invalid_proxy_scheme_is_config_error():
    with pytest.raises(NotifyConfigError) as exc:
        normalize_proxy_url("ftp://host:21")
    assert "COLDWATCH_TELEGRAM_PROXY" in str(exc.value)


async def lifespan_settings(settings, tmp_path):
    """Полный lifespan пересоздаёт движок по settings.database_url —
    нужен файловый SQLite с уже созданной схемой."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    url = f"sqlite+aiosqlite:///{tmp_path}/lifespan.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    settings.database_url = url
    return settings


async def test_bad_proxy_does_not_kill_the_app(settings, tmp_path):
    """P1: опечатка в конфиге канала не останавливает сбор данных —
    приложение стартует, notifier отключён, ошибка видна в /healthz."""
    settings = await lifespan_settings(settings, tmp_path)
    settings.telegram_bot_token = "123:abc"
    settings.telegram_proxy = "ftp://bad-scheme:1080"
    from app.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "notifier", None) is None
        assert "COLDWATCH_TELEGRAM_PROXY" in app.state.notifier_error

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/healthz")).json()
            assert health["status"] == "degraded"
            assert health["notifier"]["disabled"] is True
            assert health["database"] is True  # главный контур жив


async def test_valid_socks5h_proxy_starts(settings, tmp_path):
    settings = await lifespan_settings(settings, tmp_path)
    settings.telegram_bot_token = "123:abc"
    settings.telegram_proxy = "socks5h://10.10.0.1:1080"
    from app.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "notifier", None) is not None
        assert getattr(app.state, "notifier_error", None) is None


# ---------- P2: catch-all SPA и /api ----------


@pytest.fixture
def static_settings(settings, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>spa</html>")
    settings.static_dir = str(static)
    return settings


async def test_unknown_api_path_is_404_not_spa(engine, static_settings, ingest):
    app = build_app(static_settings, ingest)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # P2: раньше это отдавало index.html со статусом 200
        response = await client.get("/api/v1/nonexistent")
        assert response.status_code == 404
        # SPA-страницы работают
        assert (await client.get("/")).status_code == 200
        assert "spa" in (await client.get("/some/deep/link")).text
        # HEAD для health-чекеров
        assert (await client.head("/")).status_code == 200


async def test_get_sensors_list_requires_auth(engine, settings, ingest, session_factory, client):
    """P2: настоящий GET /api/v1/sensors — под аутентификацией."""
    anon = AsyncClient(
        transport=ASGITransport(app=build_app(settings, ingest)), base_url="http://test"
    )
    assert (await anon.get("/api/v1/sensors")).status_code == 401
    assert (await client.get("/api/v1/sensors")).status_code == 200


# ---------- ack из сессии ----------


async def seed_incident(session_factory, opened_ago_s=1200) -> int:
    async with session_factory() as session:
        controller = Controller(name="Холодильник")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/1", alias="Полка", position=1)
        session.add(sensor)
        await session.flush()
        incident = Incident(
            sensor_id=sensor.id,
            controller_id=controller.id,
            type=IncidentType.OVERHEAT,
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.OPEN,
            opened_at=datetime.now(UTC) - timedelta(seconds=opened_ago_s),
            peak_value=9.9,
        )
        session.add(incident)
        await session.commit()
        return incident.id


async def test_ack_uses_session_identity(client, session_factory):
    incident_id = await seed_incident(session_factory)
    response = await client.post(f"/api/v1/incidents/{incident_id}/ack")
    assert response.status_code == 200
    assert response.json()["acknowledged_by"] == "admin"  # full_name из сессии


# ---------- напоминания ----------


class StubTransport:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, payload):
        self.calls.append(payload)
        return {"ok": True, "result": {"message_id": len(self.calls)}}


@pytest.fixture
async def reminder_notifier(engine, session_factory, settings):
    settings.notify_reminder_interval_s = 600
    transport = StubTransport()
    notifier = TelegramNotifier(session_factory, settings, EventBus(), transport=transport)
    async with session_factory() as session:
        session.add(NotificationRecipient(name="Дежурная", chat_id="111", enabled=True))
        await session.commit()
    return notifier, transport


async def test_reminder_sent_for_stale_open_incident(reminder_notifier, session_factory):
    notifier, transport = reminder_notifier
    await seed_incident(session_factory, opened_ago_s=1200)

    sent = await notifier.send_reminders()
    assert sent == 1
    assert "НЕ ПОДТВЕРЖДЁН" in transport.calls[0]["text"]
    assert "9.9" in transport.calls[0]["text"]

    # немедленный повтор — рано, интервал не истёк
    assert await notifier.send_reminders() == 0

    # спустя интервал напоминание повторяется
    later = datetime.now(UTC) + timedelta(seconds=700)
    assert await notifier.send_reminders(now=later) == 1


async def test_ack_stops_reminders(reminder_notifier, session_factory, client):
    notifier, transport = reminder_notifier
    incident_id = await seed_incident(session_factory, opened_ago_s=1200)
    await client.post(f"/api/v1/incidents/{incident_id}/ack")

    assert await notifier.send_reminders() == 0
    assert transport.calls == []


async def test_fresh_incident_not_reminded_yet(reminder_notifier, session_factory):
    notifier, transport = reminder_notifier
    await seed_incident(session_factory, opened_ago_s=30)
    assert await notifier.send_reminders() == 0


# ---------- журнал аудита ----------


async def test_audit_trail_records_mutations(client):
    controller = (
        await client.post("/api/v1/controllers", json={"name": "Аудит-тест"})
    ).json()
    await client.post(
        "/api/v1/sensors",
        json={
            "controller_id": controller["id"],
            "mqtt_topic": "t/audit",
            "alias": "Полка",
            "position": 1,
        },
    )
    await client.put(
        "/api/v1/sensors/1/thresholds",
        json={"warn_low": 2, "warn_high": 8},
    )

    entries = (await client.get("/api/v1/audit")).json()
    actions = [(e["action"], e["entity_type"]) for e in entries]
    assert ("login", "session") in actions
    assert ("create", "controller") in actions
    assert ("bind", "sensor") in actions
    assert ("set_thresholds", "sensor") in actions
    assert all(e["actor"] == "admin" for e in entries)


async def test_audit_requires_admin(engine, settings, ingest, session_factory):
    from app.models.users import UserRole

    await make_user(session_factory, "op2", "operator-pass", UserRole.OPERATOR)
    app = build_app(settings, ingest)
    client = await login_client(app, "op2", "operator-pass")
    assert (await client.get("/api/v1/audit")).status_code == 403


# ---------- напоминания на масштабе (отчёт фазы 3, п.5) ----------


async def seed_many_incidents(session_factory, count: int, opened_ago_s=1200) -> None:
    async with session_factory() as session:
        controller = Controller(name="Площадка")
        session.add(controller)
        await session.flush()
        for i in range(count):
            sensor = Sensor(
                controller_id=controller.id,
                mqtt_topic=f"t/mass{i}",
                alias=f"Датчик {i}",
                position=None,
            )
            session.add(sensor)
            await session.flush()
            session.add(
                Incident(
                    sensor_id=sensor.id,
                    controller_id=controller.id,
                    type=IncidentType.OFFLINE,
                    severity=IncidentSeverity.CRITICAL,
                    status=IncidentStatus.OPEN,
                    opened_at=datetime.now(UTC) - timedelta(seconds=opened_ago_s),
                )
            )
        await session.commit()


async def test_mass_incidents_collapse_into_digest(reminder_notifier, session_factory):
    """Групповой сбой (роутер площадки): 90 offline = ОДНО сообщение-сводка,
    а не 90 отдельных."""
    notifier, transport = reminder_notifier
    await seed_many_incidents(session_factory, 90)

    processed = await notifier.send_reminders()
    assert processed == 90
    assert len(transport.calls) == 1  # один получатель — одно сообщение
    text = transport.calls[0]["text"]
    assert "90 инцидентов" in text
    assert "…и ещё 70" in text  # в сводке максимум 20 строк

    # до следующего интервала — тишина по всем 90
    assert await notifier.send_reminders() == 0


async def test_reminder_grace_after_restart(reminder_notifier, session_factory):
    """Рестарт поверх старых неподтверждённых не даёт немедленного шторма:
    первое напоминание — не раньше полного интервала от старта."""
    notifier, transport = reminder_notifier
    await seed_incident(session_factory, opened_ago_s=3900)  # инциденту 65 минут

    notifier._started_at = datetime.now(UTC)  # только что «перезапустились»
    assert await notifier.send_reminders() == 0
    assert transport.calls == []

    # спустя полный интервал напоминания возобновляются
    later = datetime.now(UTC) + timedelta(seconds=601)
    assert await notifier.send_reminders(now=later) == 1


# ---------- деградация канала по серии неудач ----------


async def test_consecutive_failures_tracked(engine, session_factory, settings):
    from app.core.bus import EventBus

    settings.notify_retry_attempts = 1
    settings.notify_retry_delay_s = 0.001

    class FailingTransport:
        def __init__(self):
            self.fail = True

        async def __call__(self, method, payload):
            if self.fail:
                raise TimeoutError()
            return {"ok": True, "result": {"message_id": 1}}

    transport = FailingTransport()
    notifier = TelegramNotifier(session_factory, settings, EventBus(), transport=transport)
    async with session_factory() as session:
        session.add(NotificationRecipient(name="Д", chat_id="1", enabled=True))
        await session.commit()

    from tests.test_notifier import make_event

    for i in range(5):
        await notifier.deliver(make_event(incident_id=i + 1))
    assert notifier.stats.consecutive_failures == 5
    # тип исключения виден в ошибке (пустой str(TimeoutError) был неотличим)
    assert "TimeoutError" in notifier.stats.last_error

    transport.fail = False
    await notifier.deliver(make_event(incident_id=99))
    assert notifier.stats.consecutive_failures == 0


# ---------- отчёт фазы 3.5: P7 (digest событий), P8 (reorder), quality basis ----------


async def test_event_digest_for_simultaneous_incidents(engine, session_factory, settings):
    """P7: групповой сбой — одно сообщение-сводка вместо залпа открытий."""
    from app.core.bus import EventBus
    from app.notify.notifier import TelegramNotifier
    from tests.test_notifier import StubTransport, make_event

    transport = StubTransport()
    notifier = TelegramNotifier(session_factory, settings, EventBus(), transport=transport)
    async with session_factory() as session:
        session.add(NotificationRecipient(name="Д", chat_id="1", enabled=True))
        await session.commit()

    events = [
        make_event(incident_id=i, type="offline", severity="critical", value=None)
        for i in range(1, 91)
    ]
    await notifier.deliver_batch(events)

    assert len(transport.calls) == 1
    text = transport.calls[0]["text"]
    assert "Событий: 90" in text and "новых: 90" in text
    assert "…и ещё 70" in text

    # одиночное событие идёт прежним путём с полным форматом
    await notifier.deliver_batch([make_event(incident_id=100)])
    assert len(transport.calls) == 2
    assert "Перегрев" in transport.calls[1]["text"]
    assert "Событий" not in transport.calls[1]["text"]


async def test_event_digest_mixed_open_resolve(engine, session_factory, settings):
    from app.core.bus import EventBus
    from app.notify.notifier import TelegramNotifier
    from tests.test_notifier import StubTransport, make_event

    transport = StubTransport()
    notifier = TelegramNotifier(session_factory, settings, EventBus(), transport=transport)
    async with session_factory() as session:
        session.add(NotificationRecipient(name="Д", chat_id="1", enabled=True))
        await session.commit()

    events = [
        make_event(incident_id=1, kind="opened"),
        make_event(incident_id=2, kind="resolved", closed_at=datetime.now(UTC)),
    ]
    await notifier.deliver_batch(events)
    text = transport.calls[0]["text"]
    assert "новых: 1" in text and "закрыто: 1" in text
    assert "🍀" in text


async def test_sensor_reorder(client):
    """P8: swap позиций одной транзакцией."""
    controller = (await client.post("/api/v1/controllers", json={"name": "Реордер"})).json()
    ids = []
    for position in (1, 2, 3):
        response = await client.post(
            "/api/v1/sensors",
            json={
                "controller_id": controller["id"],
                "mqtt_topic": f"t/ord{position}",
                "alias": f"Датчик {position}",
                "position": position,
            },
        )
        ids.append(response.json()["id"])

    # переворачиваем порядок: [3,2,1]
    response = await client.put(
        f"/api/v1/controllers/{controller['id']}/sensor-order", json=ids[::-1]
    )
    assert response.status_code == 200
    sensors = response.json()["sensors"]
    assert [(s["id"], s["position"]) for s in sensors] == [
        (ids[2], 1),
        (ids[1], 2),
        (ids[0], 3),
    ]

    # неполный список отклоняется
    response = await client.put(
        f"/api/v1/controllers/{controller['id']}/sensor-order", json=ids[:2]
    )
    assert response.status_code == 422


async def test_quality_basis_historical(client, session_factory):
    """Смена порогов не переписывает прошлое в historical-режиме."""
    from datetime import timedelta

    from app.models import Measurement, ThresholdProfile

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with session_factory() as session:
        controller = Controller(name="История")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/hist", alias="П", position=1)
        session.add(sensor)
        await session.flush()
        # профиль v1 (2..8) действовал во время данных; создан 3 часа назад
        session.add(
            ThresholdProfile(
                sensor_id=sensor.id, version=1, active=False,
                warn_low=2.0, warn_high=8.0,
                created_at=now - timedelta(hours=3),
            )
        )
        # текущий профиль v2 (2..10) создан позже всех данных
        session.add(
            ThresholdProfile(
                sensor_id=sensor.id, version=2, active=True,
                warn_low=2.0, warn_high=10.0,
                created_at=now - timedelta(minutes=1),
            )
        )
        # два часа данных по 9°C: выше 8 (v1), ниже 10 (v2)
        for minute in range(120):
            session.add(
                Measurement(
                    sensor_id=sensor.id,
                    time=now - timedelta(hours=2, minutes=1) + timedelta(minutes=minute),
                    value=9.0,
                )
            )
        await session.commit()
        controller_id = controller.id

    current = (
        await client.get(f"/api/v1/controllers/{controller_id}/quality?basis=current")
    ).json()["sensors"][0]
    historical = (
        await client.get(f"/api/v1/controllers/{controller_id}/quality?basis=historical")
    ).json()["sensors"][0]

    # по текущему профилю (до 10) нарушений нет — «прошлое переписано»
    assert current["out_total_s"] == 0
    # по действовавшему тогда профилю (до 8) — все 120 минут вне диапазона
    assert historical["out_total_s"] == 120 * 60
