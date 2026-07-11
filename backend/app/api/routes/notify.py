from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.api.deps import get_notifier
from app.api.schemas import RecipientCreate, RecipientOut, RecipientUpdate
from app.core.db import get_session
from app.models.notify import NotificationRecipient
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
    body: RecipientCreate, session: AsyncSession = Depends(get_session)
) -> NotificationRecipient:
    duplicate = await session.execute(
        select(NotificationRecipient.id).where(NotificationRecipient.chat_id == body.chat_id)
    )
    if duplicate.first() is not None:
        raise HTTPException(status_code=409, detail="chat_id already registered")
    recipient = NotificationRecipient(name=body.name, chat_id=body.chat_id, enabled=body.enabled)
    session.add(recipient)
    await session.commit()
    return recipient


@router.patch("/recipients/{recipient_id}", response_model=RecipientOut)
async def update_recipient(
    recipient_id: int,
    body: RecipientUpdate,
    session: AsyncSession = Depends(get_session),
) -> NotificationRecipient:
    recipient = await session.get(NotificationRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    for attr, value in body.model_dump(exclude_unset=True).items():
        setattr(recipient, attr, value)
    await session.commit()
    return recipient


@router.delete("/recipients/{recipient_id}", status_code=204)
async def delete_recipient(
    recipient_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    recipient = await session.get(NotificationRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    await session.delete(recipient)
    await session.commit()


@router.post("/test")
async def send_test(notifier: TelegramNotifier | None = Depends(get_notifier)) -> dict:
    """Тестовое сообщение всем включённым получателям."""
    if notifier is None:
        raise HTTPException(status_code=503, detail="Notifier is not configured")
    return await notifier.send_test()
