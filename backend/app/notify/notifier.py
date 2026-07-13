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
from app.models import (
    Controller,
    Incident,
    IncidentMessage,
    IncidentStatus,
    IncidentType,
    Sensor,
)
from app.models.audit import record_audit
from app.models.notify import NotificationRecipient, NotifierState

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
    """Счётчики канала оповещений.

    sent/failed — за текущий сеанс работы (рестарт обнуляет, это счётчики
    «с момента старта»). consecutive_failures и last_error персистятся в
    БД (`notifier_state`) и восстанавливаются при старте: серия отказов
    канала переживает рестарт, иначе перезапуск маскировал бы деградацию
    (этап B.5)."""

    sent: int = 0
    failed: int = 0
    consecutive_failures: int = 0
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


DIGEST_MAX_LINES = 20


def format_reminder_digest(
    items: list[tuple[Incident, str, str]], tz: ZoneInfo, now: datetime
) -> str:
    """Сводка по нескольким неподтверждённым инцидентам одним сообщением.

    Групповой сбой (роутер/брокер площадки = десятки offline-инцидентов)
    не должен превращаться в шторм: на целевом масштабе 30 холодильников
    поштучные напоминания дали бы сотни сообщений в час — дежурные такой
    канал просто отключат.
    """
    lines = [f"⏰ <b>НЕ ПОДТВЕРЖДЕНЫ: {len(items)} инцидентов</b>"]
    for incident, alias, controller_name in items[:DIGEST_MAX_LINES]:
        key = (str(incident.type), str(incident.severity))
        emoji = _EMOJI.get(key, "🚨")
        duration = format_duration(now - incident.opened_at)
        peak = f", пик {incident.peak_value} °C" if incident.peak_value is not None else ""
        lines.append(f"{emoji} {controller_name}, {alias} — {duration}{peak}")
    if len(items) > DIGEST_MAX_LINES:
        lines.append(f"…и ещё {len(items) - DIGEST_MAX_LINES}")
    lines.append("Подтвердите инциденты в ColdWatch, чтобы остановить напоминания.")
    return "\n".join(lines)


def format_event_digest(events: list[IncidentEvent], tz: ZoneInfo) -> str:
    """Сводка одновременных событий одним сообщением: отказ роутера/брокера
    площадки на целевом масштабе — это 90 offline разом; 90 отдельных
    сообщений при открытии и ещё 90 при закрытии убивают канал."""
    opened = sum(1 for e in events if e.kind in ("opened", "escalated"))
    resolved = len(events) - opened
    header_parts = []
    if opened:
        header_parts.append(f"новых: {opened}")
    if resolved:
        header_parts.append(f"закрыто: {resolved}")
    lines = [f"🚨 <b>Событий: {len(events)}</b> ({', '.join(header_parts)})"]
    for event in events[:DIGEST_MAX_LINES]:
        key = (str(event.type), str(event.severity))
        emoji = "🍀" if event.kind == "resolved" else _EMOJI.get(key, "🚨")
        title = "Возврат в норму" if event.kind == "resolved" else _TITLES.get(key, str(event.type))
        value = f" ({event.value} °C)" if event.value is not None else ""
        lines.append(f"{emoji} {title} — {event.controller_name}, {event.sensor_alias}{value}")
    if len(events) > DIGEST_MAX_LINES:
        lines.append(f"…и ещё {len(events) - DIGEST_MAX_LINES}")
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


def format_escalation(
    incident: Incident,
    controller_name: str,
    sensor_alias: str,
    tz: ZoneInfo,
    now: datetime,
    tier: int,
) -> str:
    key = (str(incident.type), str(incident.severity))
    emoji = _EMOJI.get(key, "🚨")
    title = _TITLES.get(key, str(incident.type))
    duration = format_duration(now - incident.opened_at)
    opened_local = incident.opened_at.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")
    lines = [
        f"📣 <b>ЭСКАЛАЦИЯ (круг {tier})</b> {emoji} {title} — "
        f"<b>{controller_name}, {sensor_alias}</b>",
        f"Инцидент не подтверждён уже <b>{duration}</b> — дежурная смена не отреагировала.",
    ]
    if incident.peak_value is not None:
        lines.append(f"Пик: <b>{incident.peak_value} °C</b>")
    lines.append(f"<i>Начало: {opened_local}</i>")
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
        self._updates_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        self._stopping = False
        self._tz = ZoneInfo(settings.display_timezone)
        # (incident_id, chat_id) -> message_id открывшего сообщения
        self._thread_ids: dict[tuple[int, str], int] = {}
        self.stats = NotifierStats()

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        self._stopping = False
        # grace-период: после старта (апгрейд/рестарт) напоминания
        # возобновляются не раньше, чем через полный интервал — иначе
        # N старых неподтверждённых дают N немедленных сообщений
        self._started_at = datetime.now(UTC)
        # признак деградации канала переживает рестарт (B.5)
        await self._load_health()
        if self._transport is None:
            self._transport = await self._make_http_transport()
        self._queue = self._bus.subscribe()
        self._task = asyncio.create_task(self._consume_loop(), name="notifier")
        if (
            self._settings.notify_reminder_interval_s > 0
            or self._settings.notify_escalation_delay_s > 0
        ):
            self._reminder_task = asyncio.create_task(
                self._reminder_loop(), name="notifier-reminders"
            )
        if self._settings.notify_telegram_ack_buttons and self._settings.telegram_bot_token:
            self._updates_task = asyncio.create_task(
                self._updates_loop(), name="notifier-updates"
            )
        if self._settings.notify_heartbeat_hour >= 0:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="notifier-heartbeat"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None
        for task_attr in ("_task", "_reminder_task", "_updates_task", "_heartbeat_task"):
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

    # ---------- учёт доставок (B.5: деградация переживает рестарт) ----------

    async def _load_health(self) -> None:
        """Восстановить счётчик подряд-неудач из БД при старте."""
        try:
            async with self._session_factory() as session:
                state = await session.get(NotifierState, 1)
                if state is not None:
                    self.stats.consecutive_failures = state.consecutive_failures
                    self.stats.last_error = state.last_error
        except Exception as exc:  # БД недоступна — не мешаем старту канала
            logger.warning("Failed to load notifier health: %s", exc)

    async def _persist_health(self) -> None:
        """Сохранить признак деградации. Персистентность — вспомогательная:
        её сбой не должен ломать доставку (сообщение уже отправлено)."""
        try:
            async with self._session_factory() as session:
                state = await session.get(NotifierState, 1)
                if state is None:
                    state = NotifierState(id=1)
                    session.add(state)
                state.consecutive_failures = self.stats.consecutive_failures
                state.last_error = self.stats.last_error
                await session.commit()
        except Exception as exc:
            logger.warning("Failed to persist notifier health: %s", exc)

    async def _record_success(self) -> None:
        self.stats.sent += 1
        # запись в БД только на переходе «деградация → норма», а не на
        # каждой рутинной доставке
        if self.stats.consecutive_failures:
            self.stats.consecutive_failures = 0
            await self._persist_health()

    async def _record_failure(self, exc: Exception) -> None:
        self.stats.failed += 1
        self.stats.consecutive_failures += 1
        self.stats.last_error = str(exc)[:500]
        await self._persist_health()

    # ---------- напоминания ----------

    async def _reminder_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._settings.notify_reminder_check_s)
            try:
                await self.process_escalations()
            except Exception:
                logger.exception("Escalation pass failed")
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
        # grace: после рестарта первое напоминание — не раньше полного
        # интервала от старта процесса
        if self._started_at is not None and (now - self._started_at).total_seconds() < interval:
            return 0
        cutoff = now - timedelta(seconds=interval)

        async with self._session_factory() as session:
            rows = await session.execute(
                select(Incident, Sensor.alias, Controller.name)
                .join(Sensor, Sensor.id == Incident.sensor_id)
                .join(Controller, Controller.id == Incident.controller_id)
                .where(
                    Incident.status == IncidentStatus.OPEN,
                    Incident.opened_at <= cutoff,
                )
                .order_by(Incident.opened_at)
            )
            due = [
                (incident, alias, controller_name)
                for incident, alias, controller_name in rows.all()
                if incident.last_reminder_at is None or incident.last_reminder_at <= cutoff
            ]
            if not due:
                return 0
            recipients_rows = await session.execute(
                select(NotificationRecipient).where(NotificationRecipient.enabled.is_(True))
            )
            recipients = list(recipients_rows.scalars().all())
            if not recipients:
                return 0

            # каждому получателю — только инциденты его круга и ниже;
            # групповые сбои схлопываются в сводку на чат
            delivered_incidents: set[int] = set()
            for recipient in recipients:
                items = [d for d in due if recipient.tier <= d[0].escalated_tier]
                if not items:
                    continue
                payload = {"chat_id": recipient.chat_id, "parse_mode": "HTML"}
                if len(items) == 1:
                    incident, alias, controller_name = items[0]
                    payload["text"] = format_reminder(
                        incident, controller_name, alias, self._tz, now
                    )
                    markup = self._ack_markup(incident.id)
                    if markup is not None:
                        payload["reply_markup"] = markup
                else:
                    payload["text"] = format_reminder_digest(items, self._tz, now)
                    digest_markup = self._digest_markup(
                        [
                            (incident.id, f"{controller_name}, {alias}")
                            for incident, alias, controller_name in items
                        ]
                    )
                    if digest_markup is not None:
                        payload["reply_markup"] = digest_markup
                try:
                    await self._send_with_retry(payload)
                    await self._record_success()
                    delivered_incidents.update(item[0].id for item in items)
                except Exception as exc:
                    await self._record_failure(exc)
                    logger.error("Reminder to chat %s failed: %s", recipient.chat_id, exc)

            if not delivered_incidents:
                return 0
            for incident, _, _ in due:
                if incident.id in delivered_incidents:
                    incident.last_reminder_at = now
                    incident.reminder_count += 1
            await session.commit()
        return len(delivered_incidents)

    # ---------- эскалация кругами ----------

    async def process_escalations(self, now: datetime | None = None) -> int:
        """Инцидент не подтверждён через delay — подключается круг 2,
        через 2*delay — круг 3 и т.д. (до максимального круга получателей).
        Подтверждение останавливает эскалацию; закрытие — тоже."""
        delay = self._settings.notify_escalation_delay_s
        if delay <= 0:
            return 0
        now = now or datetime.now(UTC)
        escalated = 0

        async with self._session_factory() as session:
            recipients_rows = await session.execute(
                select(NotificationRecipient).where(NotificationRecipient.enabled.is_(True))
            )
            recipients = list(recipients_rows.scalars().all())
            max_tier = max((r.tier for r in recipients), default=1)
            if max_tier <= 1:
                return 0

            rows = await session.execute(
                select(Incident, Sensor.alias, Controller.name)
                .join(Sensor, Sensor.id == Incident.sensor_id)
                .join(Controller, Controller.id == Incident.controller_id)
                .where(Incident.status == IncidentStatus.OPEN)
            )
            for incident, alias, controller_name in rows.all():
                age_s = (now - incident.opened_at).total_seconds()
                target = min(1 + int(age_s // delay), max_tier)
                if target <= incident.escalated_tier:
                    continue
                text = format_escalation(
                    incident, controller_name, alias, self._tz, now, target
                )
                markup = self._ack_markup(incident.id)
                delivered = False
                for recipient in recipients:
                    if not (incident.escalated_tier < recipient.tier <= target):
                        continue
                    payload = {
                        "chat_id": recipient.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    }
                    if markup is not None:
                        payload["reply_markup"] = markup
                    try:
                        await self._send_with_retry(payload)
                        await self._record_success()
                        delivered = True
                    except Exception as exc:
                        await self._record_failure(exc)
                        logger.error(
                            "Escalation to chat %s failed: %s", recipient.chat_id, exc
                        )
                if delivered:
                    incident.escalated_tier = target
                    escalated += 1
            await session.commit()
        return escalated

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
            batch = [event]
            window = self._settings.notify_group_window_s
            if window > 0:
                # собираем одновременные события: групповой сбой уходит
                # одной сводкой, а не залпом отдельных сообщений
                loop = asyncio.get_running_loop()
                deadline = loop.time() + window
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        extra = await asyncio.wait_for(self._queue.get(), remaining)
                    except TimeoutError:
                        break
                    if isinstance(extra, IncidentEvent):
                        batch.append(extra)
            try:
                await self.deliver_batch(batch)
            except Exception:
                logger.exception("Notifier failed on batch of %d", len(batch))

    async def deliver_batch(self, events: list[IncidentEvent]) -> None:
        if len(events) == 1:
            await self.deliver(events[0])
            return
        if not self._settings.telegram_bot_token and self._transport is None:
            return
        # сводка уходит всем кругам, подключённым хотя бы к одному из событий
        tiers = [
            1 if e.kind == "opened" else await self._incident_tier(e.incident_id)
            for e in events
        ]
        recipients = await self._recipients(max_tier=max(tiers))
        if not recipients:
            return
        text = format_event_digest(events, self._tz)
        # кнопки подтверждения по открытым/эскалированным инцидентам сводки
        markup = self._digest_markup(
            [
                (e.incident_id, f"{e.controller_name}, {e.sensor_alias}")
                for e in events
                if e.kind in ("opened", "escalated")
            ]
        )
        # в сводке reply-цепочки не ведутся; закрытые в сводке инциденты
        # освобождают сохранённые message_id
        for event in events:
            if event.kind == "resolved":
                for recipient in recipients:
                    self._thread_ids.pop((event.incident_id, recipient.chat_id), None)
                await self._drop_threads_db(event.incident_id)
        for recipient in recipients:
            payload = {"chat_id": recipient.chat_id, "text": text, "parse_mode": "HTML"}
            if markup is not None:
                payload["reply_markup"] = markup
            try:
                await self._send_with_retry(payload)
                await self._record_success()
            except Exception as exc:
                await self._record_failure(exc)
                logger.error("Failed to notify chat %s: %s", recipient.chat_id, exc)

    async def _recipients(self, max_tier: int | None = None) -> list[NotificationRecipient]:
        async with self._session_factory() as session:
            query = select(NotificationRecipient).where(NotificationRecipient.enabled.is_(True))
            if max_tier is not None:
                query = query.where(NotificationRecipient.tier <= max_tier)
            rows = await session.execute(query)
            return list(rows.scalars().all())

    def _ack_markup(self, incident_id: int) -> dict | None:
        """Inline-кнопка подтверждения прямо в сообщении."""
        if not self._settings.notify_telegram_ack_buttons:
            return None
        return {
            "inline_keyboard": [
                [{"text": "✅ Подтвердить", "callback_data": f"ack:{incident_id}"}]
            ]
        }

    async def _pop_thread_db(self, incident_id: int, chat_id: str) -> int | None:
        async with self._session_factory() as session:
            row = await session.get(IncidentMessage, (incident_id, chat_id))
            if row is None:
                return None
            message_id = row.message_id
            await session.delete(row)
            await session.commit()
            return message_id

    async def _drop_threads_db(self, incident_id: int) -> None:
        from sqlalchemy import delete

        async with self._session_factory() as session:
            await session.execute(
                delete(IncidentMessage).where(IncidentMessage.incident_id == incident_id)
            )
            await session.commit()

    def _digest_markup(self, items: list[tuple[int, str]]) -> dict | None:
        """Кнопки подтверждения на сводке: по строке на инцидент (до 10) —
        при групповом сбое дежурному есть чем подтверждать из чата
        (находка стенда 3.7)."""
        if not self._settings.notify_telegram_ack_buttons or not items:
            return None
        rows = [
            [{"text": f"✅ {label}"[:60], "callback_data": f"ack:{incident_id}"}]
            for incident_id, label in items[:10]
        ]
        return {"inline_keyboard": rows}

    async def _incident_tier(self, incident_id: int) -> int:
        async with self._session_factory() as session:
            incident = await session.get(Incident, incident_id)
            return incident.escalated_tier if incident is not None else 1

    async def deliver(self, event: IncidentEvent) -> None:
        if not self._settings.telegram_bot_token and self._transport is None:
            return  # канал не настроен
        # новые инциденты идут кругу 1; эскалированные/закрытые — всем
        # кругам, которые уже были подключены
        max_tier = 1 if event.kind == "opened" else await self._incident_tier(event.incident_id)
        recipients = await self._recipients(max_tier=max_tier)
        if not recipients:
            return
        text = format_message(event, self._tz)
        for recipient in recipients:
            try:
                await self._deliver_one(event, recipient.chat_id, text)
                await self._record_success()
            except Exception as exc:
                # один недоступный получатель не блокирует остальных
                await self._record_failure(exc)
                logger.error("Failed to notify chat %s: %s", recipient.chat_id, exc)

    async def _deliver_one(self, event: IncidentEvent, chat_id: str, text: str) -> None:
        thread_key = (event.incident_id, chat_id)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if event.kind == "resolved":
            reply_to = self._thread_ids.pop(thread_key, None)
            if reply_to is None:
                reply_to = await self._pop_thread_db(event.incident_id, chat_id)
            if reply_to is not None:
                payload["reply_to_message_id"] = reply_to
        else:
            markup = self._ack_markup(event.incident_id)
            if markup is not None:
                payload["reply_markup"] = markup

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
                # и в БД: reply-цепочка должна переживать рестарт
                async with self._session_factory() as session:
                    session.add(
                        IncidentMessage(
                            incident_id=event.incident_id,
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                    )
                    try:
                        await session.commit()
                    except Exception:
                        await session.rollback()  # дубль при повторной доставке

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
        raise NotifyError(
            "delivery failed after retries: "
            f"{type(last_error).__name__}: {last_error or '<no message>'}"
        )

    # ---------- подтверждение из Telegram (ack-кнопка) ----------

    async def _updates_loop(self) -> None:
        """Long-poll getUpdates через тот же транспорт (работает и за SOCKS5)."""
        offset: int | None = None
        while not self._stopping:
            try:
                payload: dict = {"timeout": 20, "allowed_updates": ["callback_query"]}
                if offset is not None:
                    payload["offset"] = offset
                data = await self._transport("getUpdates", payload)
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    callback = update.get("callback_query")
                    if callback:
                        await self.handle_callback(callback)
            except Exception as exc:
                logger.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)

    async def handle_callback(self, callback: dict) -> None:
        """Нажатие «Подтвердить» в чате: ack инцидента без входа в UI."""
        callback_id = callback.get("id")
        data = callback.get("data", "")
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        from_user = callback.get("from", {})
        who = (
            f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
            or from_user.get("username")
            or str(from_user.get("id", "?"))
        )

        async def answer(text: str) -> None:
            if callback_id is None:
                return
            try:
                await self._transport(
                    "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}
                )
            except Exception as exc:
                logger.warning("answerCallbackQuery failed: %s", exc)

        logger.info(
            "Telegram callback received: %r from %s (chat %s)", data, who, chat_id
        )
        if not data.startswith("ack:"):
            return await answer("Неизвестная команда")

        # подтверждать могут только зарегистрированные включённые чаты
        recipients = await self._recipients()
        if chat_id not in {r.chat_id for r in recipients}:
            return await answer("Этот чат не зарегистрирован в ColdWatch")

        incident_id = int(data.removeprefix("ack:"))
        async with self._session_factory() as session:
            incident = await session.get(Incident, incident_id)
            if incident is None:
                return await answer("Инцидент не найден")
            if incident.status == IncidentStatus.RESOLVED:
                return await answer("Инцидент уже закрыт")
            if incident.acknowledged_at is not None:
                return await answer(f"Уже подтверждён: {incident.acknowledged_by}")

            incident.status = IncidentStatus.ACKNOWLEDGED
            incident.acknowledged_by = f"{who} (Telegram)"
            incident.acknowledged_at = datetime.now(UTC)
            record_audit(
                session,
                f"tg:{from_user.get('username') or from_user.get('id', '?')}",
                "ack",
                "incident",
                incident_id,
            )
            await session.commit()
        logger.info("Incident %s acknowledged via Telegram by %s", incident_id, who)
        await answer("Подтверждено ✅ Напоминания остановлены.")

    # ---------- ежедневный «система жива»-дайджест ----------

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            hour = self._settings.notify_heartbeat_hour
            if hour < 0:
                return
            now_local = datetime.now(self._tz)
            fire_at = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if fire_at <= now_local:
                fire_at += timedelta(days=1)
            await asyncio.sleep((fire_at - now_local).total_seconds())
            if self._stopping:
                return
            try:
                await self.send_heartbeat()
            except Exception:
                logger.exception("Heartbeat digest failed")

    async def compose_heartbeat(self, now: datetime | None = None) -> str:
        """Система, которая молчит, неотличима от системы, где всё хорошо, —
        ежедневный дайджест подтверждает и жизнь канала, и состояние парка."""
        from app.models import Measurement, Sensor, SensorStatus

        now = now or datetime.now(UTC)
        day_ago = now - timedelta(hours=24)
        async with self._session_factory() as session:
            sensors_rows = await session.execute(
                select(Sensor).where(Sensor.status == SensorStatus.ACTIVE)
            )
            sensors = list(sensors_rows.scalars().all())
            online = 0
            for sensor in sensors:
                last = await session.execute(
                    select(Measurement.time)
                    .where(Measurement.sensor_id == sensor.id)
                    .order_by(Measurement.time.desc())
                    .limit(1)
                )
                seen = last.scalar()
                if seen is not None and (now - seen).total_seconds() < sensor.heartbeat_timeout_s:
                    online += 1

            open_rows = await session.execute(
                select(Incident).where(Incident.status != IncidentStatus.RESOLVED)
            )
            open_incidents = list(open_rows.scalars().all())
            day_rows = await session.execute(
                select(Incident).where(Incident.opened_at >= day_ago)
            )
            day_incidents = list(day_rows.scalars().all())

        lines = [f"💙 <b>ColdWatch жив</b> — {now.astimezone(self._tz).strftime('%d.%m.%Y %H:%M')}"]
        offline = len(sensors) - online
        sensor_line = f"Датчики: <b>{online}/{len(sensors)} online</b>"
        if offline:
            sensor_line += f" ⚠️ {offline} молчит"
        lines.append(sensor_line)
        lines.append(f"Инцидентов за сутки: <b>{len(day_incidents)}</b>")
        if open_incidents:
            unacked = sum(1 for i in open_incidents if i.status == IncidentStatus.OPEN)
            lines.append(
                f"⚠️ Сейчас открыто: <b>{len(open_incidents)}</b>"
                + (f", из них НЕ подтверждено: <b>{unacked}</b>" if unacked else "")
            )
        else:
            lines.append("Открытых инцидентов нет 🍀")
        if self.stats.failed:
            lines.append(
                f"Доставка за период работы: {self.stats.sent} ок / {self.stats.failed} ошибок"
            )
        return "\n".join(lines)

    async def send_heartbeat(self, now: datetime | None = None) -> None:
        text = await self.compose_heartbeat(now)
        for recipient in await self._recipients(max_tier=1):
            try:
                await self._send_with_retry(
                    {"chat_id": recipient.chat_id, "text": text, "parse_mode": "HTML"}
                )
                await self._record_success()
            except Exception as exc:
                await self._record_failure(exc)
                logger.error("Heartbeat to chat %s failed: %s", recipient.chat_id, exc)

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
