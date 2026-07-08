"""MQTT-приёмник: подписка на брокер и передача сообщений в IngestService.

Автопереподключение с экспоненциальным backoff. Потеря соединения с брокером —
слепая зона (контроллеры не буферизуют), поэтому факт дисконнекта логируется
и отражается в /healthz; в фазе 2 он станет системным инцидентом.
"""

import asyncio
import logging
import random

import aiomqtt

from app.core.config import Settings
from app.ingest.service import IngestService

logger = logging.getLogger(__name__)


class MqttIngest:
    def __init__(self, settings: Settings, service: IngestService) -> None:
        self._settings = settings
        self._service = service
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="mqtt-ingest")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._service.stats.mqtt_connected = False

    async def _run(self) -> None:
        delay = self._settings.mqtt_reconnect_min_delay
        while not self._stopping:
            try:
                async with aiomqtt.Client(
                    hostname=self._settings.mqtt_host,
                    port=self._settings.mqtt_port,
                    username=self._settings.mqtt_username,
                    password=self._settings.mqtt_password,
                ) as client:
                    await client.subscribe(self._settings.mqtt_topic_filter)
                    self._service.stats.mqtt_connected = True
                    logger.info(
                        "Connected to MQTT %s:%s, filter %r",
                        self._settings.mqtt_host,
                        self._settings.mqtt_port,
                        self._settings.mqtt_topic_filter,
                    )
                    delay = self._settings.mqtt_reconnect_min_delay
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, str):
                            payload = payload.encode()
                        elif not isinstance(payload, bytes):
                            payload = bytes(payload) if payload is not None else b""
                        await self._service.handle_message(str(message.topic), payload)
            except aiomqtt.MqttError as exc:
                self._service.stats.mqtt_connected = False
                if self._stopping:
                    return
                logger.warning("MQTT connection lost (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, self._settings.mqtt_reconnect_max_delay)
