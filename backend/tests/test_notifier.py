"""Тесты Telegram-оповещений: форматирование, ретраи, reply-цепочки, получатели."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.bus import EventBus, IncidentEvent
from app.models.notify import NotificationRecipient
from app.notify.notifier import NotifyError, TelegramNotifier, format_duration, format_message

T0 = datetime(2026, 7, 10, 9, 0, 0, tzinfo=UTC)
MSK = ZoneInfo("Europe/Moscow")


def make_event(kind="opened", **overrides) -> IncidentEvent:
    defaults = dict(
        kind=kind,
        incident_id=1,
        sensor_id=1,
        sensor_alias="Верхняя полка",
        controller_id=1,
        controller_name="Холодильник аптеки",
        type="overheat",
        severity="warning",
        value=9.4,
        opened_at=T0,
    )
    defaults.update(overrides)
    return IncidentEvent(**defaults)


class StubTransport:
    """Транспорт с управляемыми сбоями; собирает отправленные payload'ы."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_times = 0
        self.fail_with: Exception = ConnectionError("proxy down")
        self.next_message_id = 100

    async def __call__(self, method: str, payload: dict) -> dict:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        self.calls.append({"method": method, **payload})
        self.next_message_id += 1
        return {"ok": True, "result": {"message_id": self.next_message_id}}


@pytest.fixture
def transport():
    return StubTransport()


@pytest.fixture
def notifier(session_factory, settings, transport):
    settings.notify_retry_delay_s = 0.001
    return TelegramNotifier(session_factory, settings, EventBus(), transport=transport)


async def add_recipient(session_factory, chat_id="111", name="Дежурная", enabled=True):
    async with session_factory() as session:
        session.add(NotificationRecipient(name=name, chat_id=chat_id, enabled=enabled))
        await session.commit()


# ---------- форматирование ----------


def test_format_duration():
    assert format_duration(timedelta(seconds=42)) == "42 с"
    assert format_duration(timedelta(minutes=7, seconds=5)) == "7 мин 05 с"
    assert format_duration(timedelta(hours=26, minutes=3)) == "26 ч 03 мин"


def test_format_opened_warning():
    text = format_message(make_event(), MSK)
    assert "🔥" in text and "Перегрев" in text
    assert "Холодильник аптеки" in text and "Верхняя полка" in text
    assert "9.4 °C" in text
    assert "12:00:00" in text  # 09:00 UTC -> 12:00 МСК


def test_format_critical_and_escalation():
    text = format_message(make_event(severity="critical"), MSK)
    assert "🔥🔥🔥" in text and "КРИТИЧЕСКИЙ ПЕРЕГРЕВ" in text

    text = format_message(make_event(kind="escalated", severity="critical"), MSK)
    assert "Эскалация" in text


def test_format_offline():
    text = format_message(
        make_event(type="offline", severity="critical", value=None), MSK
    )
    assert "📡" in text and "слепой зоне" in text


def test_format_resolved_with_duration_and_peak():
    event = make_event(
        kind="resolved",
        value=5.1,
        peak_value=12.7,
        closed_at=T0 + timedelta(hours=1, minutes=30),
    )
    text = format_message(event, MSK)
    assert "🍀" in text
    assert "12.7" in text
    assert "1 ч 30 мин" in text


# ---------- доставка ----------


async def test_delivers_to_enabled_recipients_only(engine, notifier, transport, session_factory):
    await add_recipient(session_factory, chat_id="111")
    await add_recipient(session_factory, chat_id="222", name="Отключён", enabled=False)

    await notifier.deliver(make_event())
    assert [c["chat_id"] for c in transport.calls] == ["111"]
    assert notifier.stats.sent == 1


async def test_recipient_added_without_restart(engine, notifier, transport, session_factory):
    """Список получателей читается на каждое событие — рестарт не нужен."""
    await notifier.deliver(make_event())
    assert transport.calls == []

    await add_recipient(session_factory, chat_id="333")
    await notifier.deliver(make_event(incident_id=2))
    assert [c["chat_id"] for c in transport.calls] == ["333"]


async def test_retry_on_network_error(engine, notifier, transport, session_factory):
    await add_recipient(session_factory)
    transport.fail_times = 2  # первые две попытки — сбой сети/прокси

    await notifier.deliver(make_event())
    assert len(transport.calls) == 1
    assert notifier.stats.sent == 1


async def test_gives_up_after_retries_but_counts(engine, notifier, transport, session_factory):
    await add_recipient(session_factory)
    transport.fail_times = 99

    await notifier.deliver(make_event())
    assert notifier.stats.failed == 1
    assert "proxy down" in notifier.stats.last_error


async def test_resolve_replies_to_opening_message(engine, notifier, transport, session_factory):
    await add_recipient(session_factory)

    await notifier.deliver(make_event(kind="opened"))
    opening_id = transport.calls[0]  # message_id открытия = 101
    assert "reply_to_message_id" not in opening_id

    await notifier.deliver(
        make_event(kind="resolved", closed_at=T0 + timedelta(minutes=10), value=5.0)
    )
    assert transport.calls[1]["reply_to_message_id"] == 101


async def test_resolve_falls_back_without_reply(engine, notifier, transport, session_factory):
    """Сообщение об открытии удалено — повтор без reply, а не потеря оповещения."""
    await add_recipient(session_factory)
    await notifier.deliver(make_event(kind="opened"))

    transport.fail_times = 1
    transport.fail_with = NotifyError("Bad Request: replied message not found")
    await notifier.deliver(
        make_event(kind="resolved", closed_at=T0 + timedelta(minutes=10), value=5.0)
    )
    assert len(transport.calls) == 2
    assert "reply_to_message_id" not in transport.calls[1]
    assert notifier.stats.sent == 2


async def test_one_dead_recipient_does_not_block_others(
    engine, notifier, transport, session_factory
):
    await add_recipient(session_factory, chat_id="111")
    await add_recipient(session_factory, chat_id="222", name="Вторая")
    # обе попытки первого получателя падают навсегда? нет: fail_times действует
    # на первые N вызовов транспорта — упадут все ретраи первого получателя
    transport.fail_times = 3

    await notifier.deliver(make_event())
    assert notifier.stats.failed == 1
    assert notifier.stats.sent == 1
    assert [c["chat_id"] for c in transport.calls] == ["222"]


# ---------- API получателей ----------


async def test_recipients_api_crud(client):
    response = await client.post(
        "/api/v1/notify/recipients", json={"name": "Дежурная смена", "chat_id": "-100123"}
    )
    assert response.status_code == 201
    recipient_id = response.json()["id"]

    # дубликат chat_id отклоняется
    response = await client.post(
        "/api/v1/notify/recipients", json={"name": "Копия", "chat_id": "-100123"}
    )
    assert response.status_code == 409

    response = await client.patch(
        f"/api/v1/notify/recipients/{recipient_id}", json={"enabled": False}
    )
    assert response.json()["enabled"] is False

    assert (await client.delete(f"/api/v1/notify/recipients/{recipient_id}")).status_code == 204
    assert (await client.get("/api/v1/notify/recipients")).json() == []
