from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class Measurement(Base):
    """Сырое измерение. На PostgreSQL таблица превращается в Timescale-hypertable
    (см. миграции); составной PK (sensor_id, time) обязателен для hypertable и
    одновременно защищает от дублей при повторной доставке."""

    __tablename__ = "measurement"
    __table_args__ = (Index("ix_measurement_time", "time"),)

    sensor_id: Mapped[int] = mapped_column(
        ForeignKey("sensor.id", ondelete="RESTRICT"), primary_key=True
    )
    time: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
