from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    ControllerStatus,
    DiscoveredTopicStatus,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    SensorStatus,
)

# ---------- sensors ----------


class SensorCreate(BaseModel):
    controller_id: int
    mqtt_topic: str = Field(min_length=1, max_length=500)
    alias: str = Field(min_length=1, max_length=200)
    position: int = Field(ge=1, le=3)
    heartbeat_timeout_s: int = Field(default=120, ge=10, le=86400)


class SensorUpdate(BaseModel):
    # правка топика — для исправления опечаток при вводе в эксплуатацию
    mqtt_topic: str | None = Field(default=None, min_length=1, max_length=500)
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    position: int | None = Field(default=None, ge=1, le=3)
    heartbeat_timeout_s: int | None = Field(default=None, ge=10, le=86400)
    status: SensorStatus | None = None


class SensorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    controller_id: int
    mqtt_topic: str
    alias: str
    position: int | None
    heartbeat_timeout_s: int
    status: SensorStatus


# ---------- controllers ----------


class ControllerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class ControllerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class ControllerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    location: str | None
    notes: str | None
    status: ControllerStatus
    sensors: list[SensorOut] = []


# ---------- batches ----------


class BatchCreate(BaseModel):
    """Партия препаратов: что и когда помещено в холодильник (этап B.1)."""

    label: str = Field(min_length=1, max_length=200)
    loaded_at: datetime
    unloaded_at: datetime | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def check_period(self) -> "BatchCreate":
        if self.unloaded_at is not None and self.unloaded_at <= self.loaded_at:
            raise ValueError("unloaded_at must be after loaded_at")
        return self


class BatchUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    loaded_at: datetime | None = None
    # None в присланном JSON = «вернуть в холодильник» (снять дату выгрузки);
    # непереданное поле не трогается (exclude_unset)
    unloaded_at: datetime | None = None
    notes: str | None = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    controller_id: int
    label: str
    loaded_at: datetime
    unloaded_at: datetime | None
    notes: str | None
    created_at: datetime


# ---------- discovery ----------


class DiscoveredTopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic: str
    status: DiscoveredTopicStatus
    first_seen: datetime
    last_seen: datetime
    message_count: int
    last_value: float | None
    error_count: int
    last_error: str | None


# ---------- measurements ----------


class MeasurementPoint(BaseModel):
    time: datetime
    value: float


class MeasurementBucket(BaseModel):
    time: datetime
    avg: float
    min: float
    max: float
    count: int


class SensorLatest(BaseModel):
    sensor_id: int
    alias: str
    position: int | None
    status: SensorStatus
    time: datetime | None
    value: float | None
    online: bool | None


# ---------- thresholds ----------


class ThresholdSet(BaseModel):
    """Установка порогов датчика; создаёт новую версию профиля."""

    warn_low: float
    warn_high: float
    crit_low: float | None = None
    crit_high: float | None = None
    hysteresis: float = Field(default=0.3, ge=0.0, le=5.0)
    warn_delay_s: int = Field(default=300, ge=0, le=86400)
    crit_delay_s: int = Field(default=0, ge=0, le=86400)
    # суммарно допустимое время вне диапазона для хранимых препаратов, часы
    stability_budget_h: float | None = Field(default=None, gt=0, le=100000)
    created_by: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def check_ordering(self) -> "ThresholdSet":
        if self.warn_low >= self.warn_high:
            raise ValueError("warn_low must be below warn_high")
        if self.crit_high is not None and self.crit_high < self.warn_high:
            raise ValueError("crit_high must be at or above warn_high")
        if self.crit_low is not None and self.crit_low > self.warn_low:
            raise ValueError("crit_low must be at or below warn_low")
        if self.hysteresis * 2 >= (self.warn_high - self.warn_low):
            raise ValueError("hysteresis is too large for the warn range")
        return self


class ThresholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sensor_id: int
    version: int
    active: bool
    warn_low: float
    warn_high: float
    crit_low: float | None
    crit_high: float | None
    hysteresis: float
    warn_delay_s: int
    crit_delay_s: int
    stability_budget_h: float | None
    created_at: datetime
    created_by: str | None


# ---------- incidents ----------


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sensor_id: int
    controller_id: int
    type: IncidentType
    severity: IncidentSeverity
    status: IncidentStatus
    opened_at: datetime
    closed_at: datetime | None
    open_value: float | None
    confirm_value: float | None
    peak_value: float | None
    threshold_profile_id: int | None
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    resolution_note: str | None
    # до какого круга получателей дошла эскалация
    escalated_tier: int


class IncidentAck(BaseModel):
    # кто подтвердил, берётся из сессии; поле оставлено для переопределения
    # (например, оператор фиксирует устное подтверждение коллеги)
    acknowledged_by: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = None


# ---------- notifications ----------


class RecipientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    # круг эскалации: 1 — дежурная смена, 2/3 — подключаются при неответе
    tier: int = Field(default=1, ge=1, le=3)


class RecipientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    tier: int | None = Field(default=None, ge=1, le=3)


class RecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    chat_id: str
    enabled: bool
    tier: int
