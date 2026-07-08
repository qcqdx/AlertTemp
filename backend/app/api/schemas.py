from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import ControllerStatus, DiscoveredTopicStatus, SensorStatus

# ---------- sensors ----------


class SensorCreate(BaseModel):
    controller_id: int
    hardware_uid: str = Field(min_length=1, max_length=500)
    alias: str = Field(min_length=1, max_length=200)
    position: int = Field(ge=1, le=3)
    heartbeat_timeout_s: int = Field(default=120, ge=10, le=86400)


class SensorUpdate(BaseModel):
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    position: int | None = Field(default=None, ge=1, le=3)
    heartbeat_timeout_s: int | None = Field(default=None, ge=10, le=86400)
    status: SensorStatus | None = None


class SensorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    controller_id: int
    hardware_uid: str
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
