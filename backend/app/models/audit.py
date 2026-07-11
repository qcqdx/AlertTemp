from datetime import UTC, datetime

from sqlalchemy import String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class AuditLog(Base):
    """Журнал изменений: кто, когда и что менял в настройках системы.

    Нормативное требование холодовой цепи: изменения порогов, устройств,
    получателей и пользователей должны быть прослеживаемы.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)


def record_audit(
    session: AsyncSession,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    detail: str | None = None,
) -> None:
    """Пишется в ту же транзакцию, что и само изменение:
    либо изменение вместе со следом, либо ничего."""
    session.add(
        AuditLog(
            at=datetime.now(UTC),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail[:2000] if detail else None,
        )
    )
