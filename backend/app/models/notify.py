from sqlalchemy import Boolean, String
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
