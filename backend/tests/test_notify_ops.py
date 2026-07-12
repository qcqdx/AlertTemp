"""Параллельный фронт фазы 4: ack-кнопка, эскалация кругами, heartbeat."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.bus import EventBus
from app.models import (
    AuditLog,
    Controller,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    Measurement,
    Sensor,
)
from app.models.notify import NotificationRecipient
from app.notify.notifier import TelegramNotifier
from tests.test_notifier import make_event


class MethodStubTransport:
    """Стаб, различающий методы Telegram API."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    async def __call__(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": len(self.calls)}}


async def add_recipient(session_factory, chat_id, tier=1, name=None):
    async with session_factory() as session:
        session.add(
            NotificationRecipient(
                name=name or f"Круг {tier}", chat_id=chat_id, enabled=True, tier=tier
            )
        )
        await session.commit()


async def seed_incident(session_factory, opened_ago_s=1200, status=IncidentStatus.OPEN) -> int:
    async with session_factory() as session:
        controller = Controller(name="Холодильник")
        session.add(controller)
        await session.flush()
        sensor = Sensor(controller_id=controller.id, mqtt_topic="t/x", alias="Полка", position=1)
        session.add(sensor)
        await session.flush()
        incident = Incident(
            sensor_id=sensor.id,
            controller_id=controller.id,
            type=IncidentType.OVERHEAT,
            severity=IncidentSeverity.WARNING,
            status=status,
            opened_at=datetime.now(UTC) - timedelta(seconds=opened_ago_s),
            peak_value=9.9,
        )
        session.add(incident)
        await session.commit()
        return incident.id


@pytest.fixture
def notifier_kit(engine, session_factory, settings):
    settings.notify_retry_delay_s = 0.001
    settings.notify_escalation_delay_s = 900
    transport = MethodStubTransport()
    notifier = TelegramNotifier(session_factory, settings, EventBus(), transport=transport)
    return notifier, transport


# ---------- ack-кнопка ----------


async def test_opened_message_carries_ack_button(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")

    await notifier.deliver(make_event(incident_id=7))
    payload = transport.sent()[0]
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "ack:7"

    # на закрытии кнопки нет
    await notifier.deliver(
        make_event(incident_id=7, kind="resolved", closed_at=datetime.now(UTC))
    )
    assert "reply_markup" not in transport.sent()[1]


def make_callback(incident_id, chat_id="111", first_name="Иванова", username="ivanova"):
    return {
        "id": "cb1",
        "data": f"ack:{incident_id}",
        "from": {"id": 42, "first_name": first_name, "username": username},
        "message": {"chat": {"id": chat_id}},
    }


async def test_callback_acknowledges_incident(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")
    incident_id = await seed_incident(session_factory)

    await notifier.handle_callback(make_callback(incident_id))

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
        audit = (await session.execute(select(AuditLog))).scalars().all()
    assert incident.status == IncidentStatus.ACKNOWLEDGED
    assert incident.acknowledged_by == "Иванова (Telegram)"
    assert any(a.actor == "tg:ivanova" and a.action == "ack" for a in audit)
    answer = transport.sent("answerCallbackQuery")[0]
    assert "Подтверждено" in answer["text"]


async def test_callback_from_unregistered_chat_rejected(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")
    incident_id = await seed_incident(session_factory)

    await notifier.handle_callback(make_callback(incident_id, chat_id="999"))

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
    assert incident.status == IncidentStatus.OPEN  # не подтверждён
    assert "не зарегистрирован" in transport.sent("answerCallbackQuery")[0]["text"]


async def test_callback_on_acknowledged_incident(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")
    incident_id = await seed_incident(session_factory)
    await notifier.handle_callback(make_callback(incident_id))
    await notifier.handle_callback(make_callback(incident_id, first_name="Пётр"))

    answers = transport.sent("answerCallbackQuery")
    assert "Уже подтверждён" in answers[1]["text"]
    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
    assert incident.acknowledged_by == "Иванова (Telegram)"  # первый победил


# ---------- эскалация кругами ----------


async def test_escalation_to_second_tier(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)
    incident_id = await seed_incident(session_factory, opened_ago_s=1000)  # > delay 900

    escalated = await notifier.process_escalations()
    assert escalated == 1
    # эскалация ушла только кругу 2
    chats = [p["chat_id"] for p in transport.sent()]
    assert chats == ["222"]
    assert "ЭСКАЛАЦИЯ (круг 2)" in transport.sent()[0]["text"]

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
    assert incident.escalated_tier == 2

    # повторный проход — без дублей
    assert await notifier.process_escalations() == 0


async def test_ack_stops_escalation(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)
    incident_id = await seed_incident(
        session_factory, opened_ago_s=1000, status=IncidentStatus.ACKNOWLEDGED
    )

    assert await notifier.process_escalations() == 0
    assert transport.sent() == []
    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
    assert incident.escalated_tier == 1


async def test_opened_goes_to_tier1_only(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)

    await notifier.deliver(make_event(incident_id=1))
    assert [p["chat_id"] for p in transport.sent()] == ["111"]


async def test_resolved_reaches_escalated_tiers(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)
    incident_id = await seed_incident(session_factory, opened_ago_s=1000)
    await notifier.process_escalations()  # escalated_tier -> 2
    transport.calls.clear()

    await notifier.deliver(
        make_event(incident_id=incident_id, kind="resolved", closed_at=datetime.now(UTC))
    )
    assert sorted(p["chat_id"] for p in transport.sent()) == ["111", "222"]


async def test_reminders_respect_tier(notifier_kit, session_factory, settings):
    settings.notify_reminder_interval_s = 600
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)
    await seed_incident(session_factory, opened_ago_s=700)  # < escalation delay

    assert await notifier.send_reminders() == 1
    # инцидент ещё на круге 1 — второй круг напоминание не получает
    assert [p["chat_id"] for p in transport.sent()] == ["111"]


# ---------- heartbeat ----------


async def test_heartbeat_compose_and_send(notifier_kit, session_factory):
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111", tier=1)
    await add_recipient(session_factory, "222", tier=2)

    now = datetime.now(UTC)
    async with session_factory() as session:
        controller = Controller(name="Х")
        session.add(controller)
        await session.flush()
        live = Sensor(controller_id=controller.id, mqtt_topic="t/live", alias="Ж", position=1)
        dead = Sensor(controller_id=controller.id, mqtt_topic="t/dead", alias="М", position=2)
        session.add_all([live, dead])
        await session.flush()
        session.add(Measurement(sensor_id=live.id, time=now - timedelta(seconds=5), value=5.0))
        session.add(
            Measurement(sensor_id=dead.id, time=now - timedelta(hours=2), value=5.0)
        )
        await session.commit()

    text = await notifier.compose_heartbeat(now)
    assert "ColdWatch жив" in text
    assert "1/2 online" in text
    assert "молчит" in text
    assert "Открытых инцидентов нет" in text

    await notifier.send_heartbeat(now)
    # дайджест — только кругу 1
    assert [p["chat_id"] for p in transport.sent()] == ["111"]


# ---------- находки 3.7 ----------


async def test_reply_thread_survives_restart(notifier_kit, session_factory, settings):
    """Находка 2: message_id открытия персистится — закрытие после рестарта
    приходит reply-цепочкой, а не отдельным сообщением."""
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")
    incident_id = await seed_incident(session_factory)

    await notifier.deliver(make_event(incident_id=incident_id, kind="opened"))
    opened_message_id = transport.calls[-1][1]  # message_id из стаба

    # «рестарт»: новый экземпляр notifier с пустой памятью
    transport2 = MethodStubTransport()
    notifier2 = TelegramNotifier(
        session_factory, settings, EventBus(), transport=transport2
    )
    await notifier2.deliver(
        make_event(incident_id=incident_id, kind="resolved", closed_at=datetime.now(UTC))
    )
    resolved_payload = transport2.sent()[0]
    assert "reply_to_message_id" in resolved_payload

    # запись удалена после использования
    async with session_factory() as session:
        from app.models import IncidentMessage

        row = await session.get(IncidentMessage, (incident_id, "111"))
    assert row is None
    assert opened_message_id is not None


async def test_event_digest_carries_ack_buttons(notifier_kit, session_factory):
    """Находка 1: сводка событий несёт кнопки подтверждения по инцидентам."""
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")

    events = [make_event(incident_id=i, type="offline", value=None) for i in range(1, 16)]
    events.append(
        make_event(incident_id=99, kind="resolved", closed_at=datetime.now(UTC))
    )
    await notifier.deliver_batch(events)

    payload = transport.sent()[0]
    rows = payload["reply_markup"]["inline_keyboard"]
    assert len(rows) == 10  # максимум 10 кнопок
    assert rows[0][0]["callback_data"] == "ack:1"
    # закрытый инцидент кнопки не получает
    assert all(r[0]["callback_data"] != "ack:99" for r in rows)


async def test_reminder_digest_carries_ack_buttons(notifier_kit, session_factory, settings):
    settings.notify_reminder_interval_s = 600
    notifier, transport = notifier_kit
    await add_recipient(session_factory, "111")
    await seed_many_incidents_local(session_factory, 3)

    await notifier.send_reminders()
    payload = transport.sent()[0]
    assert "НЕ ПОДТВЕРЖДЕНЫ: 3" in payload["text"]
    assert len(payload["reply_markup"]["inline_keyboard"]) == 3


async def seed_many_incidents_local(session_factory, count):
    async with session_factory() as session:
        controller = Controller(name="Площадка")
        session.add(controller)
        await session.flush()
        for i in range(count):
            sensor = Sensor(
                controller_id=controller.id, mqtt_topic=f"t/m{i}", alias=f"Д{i}", position=None
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
                    opened_at=datetime.now(UTC) - timedelta(seconds=1200),
                )
            )
        await session.commit()


async def test_escalated_tier_in_incident_api(client, session_factory):
    """Находка 4: escalated_tier виден в API."""
    await seed_incident(session_factory)
    incidents = (await client.get("/api/v1/incidents")).json()
    assert incidents[0]["escalated_tier"] == 1
