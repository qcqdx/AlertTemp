import enum
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime


class UserRole(enum.StrEnum):
    ADMIN = "admin"  # устройства, пороги, получатели, пользователи
    OPERATOR = "operator"  # подтверждение инцидентов, комментарии
    VIEWER = "viewer"  # только просмотр


class User(TimestampMixin, Base):
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.VIEWER,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserSession(Base):
    """Серверные сессии: cookie хранит только случайный токен,
    отзыв сессии и смена пароля действуют мгновенно."""

    __tablename__ = "user_session"

    token: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
