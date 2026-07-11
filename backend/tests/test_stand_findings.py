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
