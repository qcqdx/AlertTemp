"""Пороги, инциденты и персистентное состояние детекции."""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class IncidentType(enum.StrEnum):
    OVERHEAT = "overheat"
    OVERCOOL = "overcool"
    # молчание датчика: единственный сигнал смерти датчика/контроллера/сети —
    # прошивка не шлёт heartbeat, различить причины по данным нельзя
    OFFLINE = "offline"


class IncidentSeverity(enum.StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentStatus(enum.StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"  # персонал видел и работает
    RESOLVED = "resolved"  # условие закончилось (авто)


class ThresholdProfile(Base):
    """Версионируемые пороги датчика: изменение создаёт новую версию,
    старые сохраняются — инциденты ссылаются на версию, по которой открыты."""

    __tablename__ = "threshold_profile"
    __table_args__ = (
        UniqueConstraint("sensor_id", "version", name="uq_threshold_profile_sensor_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensor.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    warn_low: Mapped[float] = mapped_column(Float, nullable=False)
    warn_high: Mapped[float] = mapped_column(Float, nullable=False)
    crit_low: Mapped[float | None] = mapped_column(Float)
    crit_high: Mapped[float | None] = mapped_column(Float)
    # гистерезис: выход из аварийной зоны фиксируется только при заходе
    # за порог на эту величину — убирает дребезг при дрейфе вокруг порога
    hysteresis: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    # бюджет стабильности: суммарно допустимое время вне диапазона (часы)
    # для препаратов в этом холодильнике; None = не задан
    stability_budget_h: Mapped[float | None] = mapped_column(Float)
    # минимальная длительность нарушения до открытия инцидента:
    # короткий заброс от открытой дверцы — не авария
    warn_delay_s: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    crit_delay_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(200))


class Incident(Base):
    """Одна экскурсия — один инцидент: от выхода за порог до возврата в норму.
    Severity только эскалирует (warning -> critical) и не понижается."""

    __tablename__ = "incident"

    id: Mapped[int] = mapped_column(primary_key=True)
    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensor.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    controller_id: Mapped[int] = mapped_column(
        ForeignKey("controller.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    type: Mapped[IncidentType] = mapped_column(
        Enum(IncidentType, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        Enum(IncidentSeverity, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IncidentStatus.OPEN,
        index=True,
    )
    # opened_at — момент фактического выхода за порог (начало pending-окна),
    # а не момент, когда истекла задержка и инцидент открылся
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # значение в момент выхода за порог (соответствует opened_at)
    open_value: Mapped[float | None] = mapped_column(Float)
    # значение в момент фиксации инцидента (после истечения задержки)
    confirm_value: Mapped[float | None] = mapped_column(Float)
    peak_value: Mapped[float | None] = mapped_column(Float)
    threshold_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("threshold_profile.id", ondelete="RESTRICT")
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolution_note: Mapped[str | None] = mapped_column(Text)
    # напоминания: пока инцидент открыт и не подтверждён, оповещение
    # повторяется; подтверждение (ack) останавливает напоминания
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SensorRuntimeState(Base):
    """Текущее термическое состояние датчика; переживает рестарт сервиса."""

    __tablename__ = "sensor_runtime_state"

    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensor.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
