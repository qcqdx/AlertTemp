"""Контроллеры и датчики.

Контроллер — логическая сущность: железо себя в эфире не обозначает, в MQTT
видны только id датчиков (топики). Администратор группирует до трёх датчиков
под контроллером так, как они подключены физически.
"""

import enum

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

SENSORS_PER_CONTROLLER = 3


class ControllerStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SensorStatus(enum.StrEnum):
    ACTIVE = "active"
    # paused: данные пишутся, но детекция аварий отключена (обслуживание/разморозка)
    PAUSED = "paused"
    ARCHIVED = "archived"


class Controller(TimestampMixin, Base):
    __tablename__ = "controller"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ControllerStatus] = mapped_column(
        Enum(ControllerStatus, values_callable=lambda e: [m.value for m in e]),
        default=ControllerStatus.ACTIVE,
        nullable=False,
    )

    sensors: Mapped[list["Sensor"]] = relationship(
        back_populates="controller", order_by="Sensor.position"
    )


class Sensor(TimestampMixin, Base):
    __tablename__ = "sensor"
    __table_args__ = (
        # позиция уникальна в пределах контроллера; архивные датчики
        # позицию освобождают (position становится NULL при архивировании)
        UniqueConstraint("controller_id", "position", name="uq_sensor_controller_position"),
        # id датчика уникален среди неархивных: после архивирования тот же
        # физический датчик можно привязать заново (например, к другому контроллеру)
        Index(
            "uq_sensor_hardware_uid_active",
            "hardware_uid",
            unique=True,
            postgresql_where=text("status != 'archived'"),
            sqlite_where=text("status != 'archived'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    controller_id: Mapped[int] = mapped_column(
        ForeignKey("controller.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # id датчика, который транслирует железо (MQTT-топик); задан извне, неизменяем
    hardware_uid: Mapped[str] = mapped_column(String(500), nullable=False)
    # обязательный человекочитаемый псевдоним: во всех экранах и оповещениях — он
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int | None] = mapped_column(Integer)
    heartbeat_timeout_s: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    status: Mapped[SensorStatus] = mapped_column(
        Enum(SensorStatus, values_callable=lambda e: [m.value for m in e]),
        default=SensorStatus.ACTIVE,
        nullable=False,
    )

    controller: Mapped[Controller] = relationship(back_populates="sensors")
