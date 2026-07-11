"""Оповещения об инцидентах в Telegram.

- слушает IncidentEvent из внутренней шины;
- получатели читаются из БД на каждое событие — добавление действует сразу;
- доставка через aiohttp; при заданном COLDWATCH_TELEGRAM_PROXY — через
  SOCKS5 (на площадках, где api.telegram.org блокируется, это единственный
  рабочий путь);
- закрытие инцидента отвечает reply-цепочкой на сообщение об открытии
  (наследие AlertTemp — удобно читать историю аварии одной веткой);
- ретраи с backoff; ошибка доставки одному получателю не блокирует остальных.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.bus import EventBus, IncidentEvent
from app.core.config import Settings
from app.models import IncidentType
from app.models.notify import NotificationRecipient

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# transport(method, payload) -> ответ Telegram (dict); подменяется в тестах
Transport = Callable[[str, dict], Awaitable[dict]]


class NotifyError(Exception):
    pass


@dataclass(slots=True)
class NotifierStats:
    sent: int = 0
    failed: int = 0
    last_error: str | None = None
    extra: dict = field(default_factory=dict)


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {seconds:02d} с"
    return f"{seconds} с"


_EMOJI = {
    ("overheat", "warning"): "🔥",
    ("overheat", "critical"): "🔥🔥🔥",
    ("overcool", "warning"): "❄️",
    ("overcool", "critical"): "❄️❄️❄️",
    ("offline", "critical"): "📡",
    ("offline", "warning"): "📡",
}
_TITLES = {
    ("overheat", "warning"): "Перегрев",
    ("overheat", "critical"): "КРИТИЧЕСКИЙ ПЕРЕГРЕВ",
    ("overcool", "warning"): "Переохлаждение",
    ("overcool", "critical"): "КРИТИЧЕСКОЕ ПЕРЕОХЛАЖДЕНИЕ",
    ("offline", "critical"): "НЕТ ДАННЫХ С ДАТЧИКА",
    ("offline", "warning"): "Нет данных с датчика",
}


def format_message(event: IncidentEvent, tz: ZoneInfo) -> str:
    key = (str(event.type), str(event.severity))
    emoji = _EMOJI.get(key, "🚨")
    place = f"<b>{event.controller_name}, {event.sensor_alias}</b>"
    opened_local = event.opened_at.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")

    if event.kind == "resolved":
        lines = [f"🍀 <b>Возврат в норму</b> — {place}"]
        if event.type == IncidentType.OFFLINE:
            lines.append("Данные с датчика снова поступают.")
        else:
            if event.value is not None:
                lines.append(f"Сейчас: <b>{event.value} °C</b>")
            if event.peak_value is not None:
                lines.append(f"Пик за время нарушения: <b>{event.peak_value} °C</b>")
        if event.closed_at is not None:
            duration = format_duration(event.closed_at - event.opened_at)
            lines.append(f"Длительность: <b>{duration}</b>")
        lines.append(f"<i>Начало: {opened_local}</i>")
        return "\n".join(lines)

    title = _TITLES.get(key, str(event.type))
    prefix = "⏫ Эскалация: " if event.kind == "escalated" else ""
    lines = [f"{emoji} <b>{prefix}{title}</b> — {place}"]
    if event.type == IncidentType.OFFLINE:
        lines.append("Датчик замолчал: возможен отказ датчика, контроллера или сети.")
        lines.append("Показания холодильника в слепой зоне!")
    elif event.value is not None:
        lines.append(f"Текущее значение: <b>{event.value} °C</b>")
    lines.append(f"<i>Зафиксировано: {opened_local}</i>")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        bus: EventBus,
        transport: Transport | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._bus = bus
        self._transport = transport
        self._http = None  # aiohttp.ClientSession
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._tz = ZoneInfo(settings.display_timezone)
        # (incident_id, chat_id) -> message_id открывшего сообщения
        self._thread_ids: dict[tuple[int, str], int] = {}
        self.stats = NotifierStats()

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        self._stopping = False
        if self._transport is None:
            self._transport = await self._make_http_transport()
        self._queue = self._bus.subscribe()
        self._task = asyncio.create_task(self._consume_loop(), name="notifier")

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def _make_http_transport(self) -> Transport:
        import aiohttp

        if self._settings.telegram_proxy:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(self._settings.telegram_proxy)
            logger.info("Telegram delivery goes through SOCKS proxy")
        else:
            connector = aiohttp.TCPConnector()
        self._http = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=30)
        )

        async def transport(method: str, payload: dict) -> dict:
            url = f"{TELEGRAM_API}/bot{self._settings.telegram_bot_token}/{method}"
            async with self._http.post(url, json=payload) as response:
                data = await response.json()
                if not data.get("ok"):
                    raise NotifyError(str(data.get("description", response.status)))
                return data

        return transport

    # ---------- доставка ----------

    async def _consume_loop(self) -> None:
        assert self._queue is not None
        while not self._stopping:
            event = await self._queue.get()
            if not isinstance(event, IncidentEvent):
                continue
            try:
                await self.deliver(event)
            except Exception:
                logger.exception("Notifier failed on %s", event)

    async def _recipients(self) -> list[NotificationRecipient]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(NotificationRecipient).where(NotificationRecipient.enabled.is_(True))
            )
            return list(rows.scalars().all())

    async def deliver(self, event: IncidentEvent) -> None:
        if not self._settings.telegram_bot_token and self._transport is None:
            return  # канал не настроен
        recipients = await self._recipients()
        if not recipients:
            return
        text = format_message(event, self._tz)
        for recipient in recipients:
            try:
                await self._deliver_one(event, recipient.chat_id, text)
                self.stats.sent += 1
            except Exception as exc:
                # один недоступный получатель не блокирует остальных
                self.stats.failed += 1
                self.stats.last_error = str(exc)[:500]
                logger.error("Failed to notify chat %s: %s", recipient.chat_id, exc)

    async def _deliver_one(self, event: IncidentEvent, chat_id: str, text: str) -> None:
        thread_key = (event.incident_id, chat_id)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if event.kind == "resolved":
            reply_to = self._thread_ids.pop(thread_key, None)
            if reply_to is not None:
                payload["reply_to_message_id"] = reply_to

        try:
            data = await self._send_with_retry(payload)
        except NotifyError as exc:
            # исходное сообщение могло быть удалено — повторяем без reply
            if "reply_to_message_id" in payload and "not found" in str(exc).lower():
                payload.pop("reply_to_message_id")
                data = await self._send_with_retry(payload)
            else:
                raise

        if event.kind == "opened":
            message_id = data.get("result", {}).get("message_id")
            if message_id is not None:
                self._thread_ids[thread_key] = message_id

    async def _send_with_retry(self, payload: dict) -> dict:
        assert self._transport is not None
        last_error: Exception | None = None
        for attempt in range(1, self._settings.notify_retry_attempts + 1):
            try:
                return await self._transport("sendMessage", payload)
            except NotifyError as exc:
                # ошибка уровня Telegram API (кроме reply): ретрай не поможет
                if "not found" in str(exc).lower():
                    raise
                last_error = exc
            except Exception as exc:  # сеть/прокси
                last_error = exc
            if attempt < self._settings.notify_retry_attempts:
                await asyncio.sleep(self._settings.notify_retry_delay_s * attempt)
        raise NotifyError(f"delivery failed after retries: {last_error}")

    async def send_test(self, text: str = "ColdWatch: тестовое оповещение ✅") -> dict:
        """Кнопка «отправить тестовое» в настройках."""
        recipients = await self._recipients()
        results: dict[str, str] = {}
        for recipient in recipients:
            try:
                await self._send_with_retry({"chat_id": recipient.chat_id, "text": text})
                results[recipient.chat_id] = "ok"
            except Exception as exc:
                results[recipient.chat_id] = f"error: {exc}"
        return results
