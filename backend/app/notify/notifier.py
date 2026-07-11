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
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.bus import EventBus, IncidentEvent
from app.core.config import Settings
from app.models import Controller, Incident, IncidentStatus, IncidentType, Sensor
from app.models.notify import NotificationRecipient

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# transport(method, payload) -> ответ Telegram (dict); подменяется в тестах
Transport = Callable[[str, dict], Awaitable[dict]]


class NotifyError(Exception):
    pass


class NotifyConfigError(Exception):
    """Ошибка конфигурации канала. Ловится в lifespan: канал отключается,
    приложение (и главный контур — сбор измерений) продолжает работать."""


def normalize_proxy_url(url: str) -> tuple[str, bool]:
    """Приводит proxy-URL к виду, который понимает aiohttp-socks.

    curl-стиль `socks5h://` (резолвить DNS на прокси) поддерживается:
    маппится в socks5 + rdns=True. Непонятная схема — явная ошибка
    конфигурации, а не падение при первой отправке.
    """
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    rdns = False
    if scheme in ("socks5h", "socks4a"):
        url = url.replace(f"{scheme}://", f"{scheme[:-1]}://", 1)
        rdns = True
        scheme = scheme[:-1]
    if scheme not in ("socks5", "socks4", "http"):
        raise NotifyConfigError(
            f"Unsupported proxy scheme {scheme!r} in COLDWATCH_TELEGRAM_PROXY: "
            "use socks5://, socks5h://, socks4://, socks4a:// or http://"
        )
    return url, rdns


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


def format_reminder(
    incident: Incident,
    controller_name: str,
    sensor_alias: str,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    key = (str(incident.type), str(incident.severity))
    emoji = _EMOJI.get(key, "🚨")
    title = _TITLES.get(key, str(incident.type))
    duration = format_duration(now - incident.opened_at)
    opened_local = incident.opened_at.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")
    lines = [
        f"⏰ <b>НЕ ПОДТВЕРЖДЁН</b> {emoji} {title} — "
        f"<b>{controller_name}, {sensor_alias}</b>",
        f"Инцидент продолжается уже <b>{duration}</b>, никто не отреагировал.",
    ]
    if incident.peak_value is not None:
        lines.append(f"Пик: <b>{incident.peak_value} °C</b>")
    lines.append(f"<i>Начало: {opened_local}</i>")
    lines.append("Подтвердите инцидент в ColdWatch, чтобы остановить напоминания.")
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
        self._reminder_task: asyncio.Task | None = None
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
        if self._settings.notify_reminder_interval_s > 0:
            self._reminder_task = asyncio.create_task(
                self._reminder_loop(), name="notifier-reminders"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None
        for task_attr in ("_task", "_reminder_task"):
            task = getattr(self, task_attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)
        if self._http is not None:
            await self._http.close()
            self._http = None

    # ---------- напоминания ----------

    async def _reminder_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._settings.notify_reminder_check_s)
            try:
                await self.send_reminders()
            except Exception:
                logger.exception("Reminder pass failed")

    async def send_reminders(self, now: datetime | None = None) -> int:
        """Повторное оповещение по открытым НЕподтверждённым инцидентам.

        Подтверждение (ack) или закрытие останавливают напоминания —
        это и есть смысл кнопки «Подтвердить» для дежурной смены.
        """
        interval = self._settings.notify_reminder_interval_s
        if interval <= 0:
            return 0
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=interval)
        sent = 0

        async with self._session_factory() as session:
            rows = await session.execute(
                select(Incident, Sensor.alias, Controller.name)
                .join(Sensor, Sensor.id == Incident.sensor_id)
                .join(Controller, Controller.id == Incident.controller_id)
                .where(
                    Incident.status == IncidentStatus.OPEN,
                    Incident.opened_at <= cutoff,
                )
            )
            due = [
                (incident, alias, controller_name)
                for incident, alias, controller_name in rows.all()
                if incident.last_reminder_at is None or incident.last_reminder_at <= cutoff
            ]
            recipients = await session.execute(
                select(NotificationRecipient).where(NotificationRecipient.enabled.is_(True))
            )
            chat_ids = [r.chat_id for r in recipients.scalars()]

            for incident, alias, controller_name in due:
                if not chat_ids:
                    break
                text = format_reminder(incident, controller_name, alias, self._tz, now)
                delivered = False
                for chat_id in chat_ids:
                    try:
                        await self._send_with_retry(
                            {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                        )
                        self.stats.sent += 1
                        delivered = True
                    except Exception as exc:
                        self.stats.failed += 1
                        self.stats.last_error = str(exc)[:500]
                        logger.error("Reminder to chat %s failed: %s", chat_id, exc)
                if delivered:
                    incident.last_reminder_at = now
                    incident.reminder_count += 1
                    sent += 1
            await session.commit()
        return sent

    async def _make_http_transport(self) -> Transport:
        import aiohttp

        if self._settings.telegram_proxy:
            from aiohttp_socks import ProxyConnector

            proxy_url, rdns = normalize_proxy_url(self._settings.telegram_proxy)
            try:
                connector = ProxyConnector.from_url(proxy_url, rdns=rdns)
            except ValueError as exc:
                raise NotifyConfigError(
                    f"Invalid COLDWATCH_TELEGRAM_PROXY {self._settings.telegram_proxy!r}: {exc}"
                ) from exc
            logger.info("Telegram delivery goes through SOCKS proxy (rdns=%s)", rdns)
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
