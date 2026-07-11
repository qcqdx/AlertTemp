import enum
from datetime import datetime

from sqlalchemy import BigInteger, Enum, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class DiscoveredTopicStatus(enum.StrEnum):
    NEW = "new"  # виден в эфире, ни к чему не привязан — ждёт решения администратора
    IGNORED = "ignored"  # администратор пометил как чужой/мусорный трафик
    BOUND = "bound"  # привязан к датчику (создан Sensor с этим mqtt_topic)


class DiscoveredTopic(Base):
    """Очередь обнаружения: MQTT-топики (id датчиков), не привязанные к датчикам.

    Заменяет слепое автосоздание сущностей из исходного проекта: новые датчики
    подключаются только явным решением администратора.
    """

    __tablename__ = "discovered_topic"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    status: Mapped[DiscoveredTopicStatus] = mapped_column(
        Enum(DiscoveredTopicStatus, values_callable=lambda e: [m.value for m in e]),
        default=DiscoveredTopicStatus.NEW,
        nullable=False,
    )
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    message_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_value: Mapped[float | None] = mapped_column(Float)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
