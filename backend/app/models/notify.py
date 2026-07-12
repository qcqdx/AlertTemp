from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class NotificationRecipient(TimestampMixin, Base):
    """Получатель оповещений (Telegram chat_id пользователя или группы).

    Список читается из БД на каждое оповещение: добавление получателя
    действует сразу, без рестарта (в отличие от исходного проекта).
    """

    __tablename__ = "notification_recipient"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # круг эскалации: 1 — дежурная смена (получает всё сразу); 2, 3 —
    # подключаются, если инцидент не подтверждён через delay, 2*delay...
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class IncidentMessage(Base):
    """Message_id сообщения об открытии инцидента по каждому чату:
    reply-цепочка закрытия и кнопки на сводках переживают рестарт
    (находка стенда 3.7: in-memory нить рвалась)."""

    __tablename__ = "incident_message"

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
