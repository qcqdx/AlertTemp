"""Валидация данных из MQTT.

Топики и payload — внешние недоверенные данные: контроллеры мы не контролируем,
а на брокере может оказаться чужой трафик.
"""

MAX_TOPIC_LENGTH = 500
MAX_PAYLOAD_LENGTH = 64


class PayloadError(ValueError):
    pass


def parse_temperature(payload: bytes, plausible_min: float, plausible_max: float) -> float:
    """Разбирает payload контроллера: число (float) в виде текста, °C."""
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise PayloadError(f"payload too long ({len(payload)} bytes)")
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PayloadError("payload is not valid UTF-8") from exc
    try:
        value = float(text)
    except ValueError as exc:
        raise PayloadError(f"payload is not a number: {text!r}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise PayloadError(f"payload is not a finite number: {text!r}")
    if not plausible_min <= value <= plausible_max:
        raise PayloadError(f"value {value} outside plausible range")
    return value


def validate_topic(topic: str) -> str:
    if not topic or len(topic) > MAX_TOPIC_LENGTH:
        raise PayloadError(f"topic length {len(topic)} out of bounds")
    return topic
