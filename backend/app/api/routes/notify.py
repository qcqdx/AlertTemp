from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.api.deps import get_notifier
from app.api.schemas import RecipientCreate, RecipientOut, RecipientUpdate
from app.core.db import get_session
from app.models.audit import record_audit
from app.models.notify import NotificationRecipient
from app.models.users import User
from app.notify.notifier import TelegramNotifier

router = APIRouter(
    prefix="/api/v1/notify", tags=["notifications"], dependencies=[Depends(require_admin)]
)


@router.get("/recipients", response_model=list[RecipientOut])
async def list_recipients(
    session: AsyncSession = Depends(get_session),
) -> list[NotificationRecipient]:
    rows = await session.execute(
        select(NotificationRecipient).order_by(NotificationRecipient.name)
    )
    return list(rows.scalars().all())


@router.post("/recipients", response_model=RecipientOut, status_code=201)
async def add_recipient(
    body: RecipientCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> NotificationRecipient:
    duplicate = await session.execute(
        select(NotificationRecipient.id).where(NotificationRecipient.chat_id == body.chat_id)
    )
    if duplicate.first() is not None:
        raise HTTPException(status_code=409, detail="chat_id already registered")
    recipient = NotificationRecipient(name=body.name, chat_id=body.chat_id, enabled=body.enabled)
    session.add(recipient)
    await session.flush()
    record_audit(session, user.username, "create", "recipient", recipient.id, body.name)
    await session.commit()
    return recipient


@router.patch("/recipients/{recipient_id}", response_model=RecipientOut)
async def update_recipient(
    recipient_id: int,
    body: RecipientUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> NotificationRecipient:
    recipient = await session.get(NotificationRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    updates = body.model_dump(exclude_unset=True)
    for attr, value in updates.items():
        setattr(recipient, attr, value)
    record_audit(session, user.username, "update", "recipient", recipient_id, str(updates))
    await session.commit()
    return recipient


@router.delete("/recipients/{recipient_id}", status_code=204)
async def delete_recipient(
    recipient_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    recipient = await session.get(NotificationRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    record_audit(session, user.username, "delete", "recipient", recipient_id, recipient.name)
    await session.delete(recipient)
    await session.commit()


@router.post("/test")
async def send_test(notifier: TelegramNotifier | None = Depends(get_notifier)) -> dict:
    """Тестовое сообщение всем включённым получателям."""
    if notifier is None:
        raise HTTPException(status_code=503, detail="Notifier is not configured")
    return await notifier.send_test()
