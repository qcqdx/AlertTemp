"""Партии препаратов (этап B.1): CRUD.

Партия задаёт окно расчёта качества: quality/report принимают
`batch_id=<id>` и считают MKT/бюджет от даты загрузки партии, а не от
скользящего окна (см. resolve_batch_window).

Роли: просмотр — viewer, изменения — admin (по контракту ROADMAP B.1;
расширить до operator можно, если стенд попросит — персонал физически
загружает партии сам).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin, require_viewer
from app.api.schemas import BatchCreate, BatchOut, BatchUpdate
from app.core.db import get_session
from app.models import Batch, Controller
from app.models.audit import format_detail, record_audit
from app.models.users import User

router = APIRouter(
    prefix="/api/v1", tags=["batches"], dependencies=[Depends(require_viewer)]
)


def _aware(value: datetime | None) -> datetime | None:
    """Пришедшее из JSON naive-время трактуем как UTC (паттерн ROADMAP §2.8)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def resolve_batch_window(
    session: AsyncSession, controller_id: int, batch_id: int
) -> tuple[Batch, datetime, datetime]:
    """Окно партии для quality/report: [loaded_at, unloaded_at или сейчас].

    Партия другого контроллера — 422 (не 404: запись существует, но запрос
    противоречив и пересчёту по этому холодильнику не подлежит).
    """
    batch = await session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.controller_id != controller_id:
        raise HTTPException(
            status_code=422, detail="Batch belongs to another controller"
        )
    now = datetime.now(UTC)
    start = batch.loaded_at
    end = min(batch.unloaded_at or now, now)
    if start >= end:
        raise HTTPException(status_code=422, detail="Batch period is empty so far")
    return batch, start, end


@router.get("/controllers/{controller_id}/batches", response_model=list[BatchOut])
async def list_batches(
    controller_id: int,
    include_unloaded: bool = True,
    session: AsyncSession = Depends(get_session),
) -> list[Batch]:
    if await session.get(Controller, controller_id) is None:
        raise HTTPException(status_code=404, detail="Controller not found")
    query = (
        select(Batch)
        .where(Batch.controller_id == controller_id)
        .order_by(Batch.loaded_at.desc())
    )
    if not include_unloaded:
        query = query.where(Batch.unloaded_at.is_(None))
    result = await session.execute(query)
    return list(result.scalars().all())


@router.post(
    "/controllers/{controller_id}/batches", response_model=BatchOut, status_code=201
)
async def create_batch(
    controller_id: int,
    body: BatchCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> Batch:
    if await session.get(Controller, controller_id) is None:
        raise HTTPException(status_code=404, detail="Controller not found")
    batch = Batch(
        controller_id=controller_id,
        label=body.label,
        loaded_at=_aware(body.loaded_at),
        unloaded_at=_aware(body.unloaded_at),
        notes=body.notes,
    )
    session.add(batch)
    await session.flush()
    record_audit(session, user.username, "create", "batch", batch.id, body.label)
    await session.commit()
    return batch


@router.patch("/batches/{batch_id}", response_model=BatchOut)
async def update_batch(
    batch_id: int,
    body: BatchUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> Batch:
    batch = await session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    updates = body.model_dump(exclude_unset=True)
    for field in ("loaded_at", "unloaded_at"):
        if field in updates:
            updates[field] = _aware(updates[field])
    # порядок проверяется по ИТОГОВОМУ состоянию (правка может прийти по одному полю)
    loaded_at = updates.get("loaded_at", batch.loaded_at)
    unloaded_at = updates.get("unloaded_at", batch.unloaded_at)
    if unloaded_at is not None and unloaded_at <= loaded_at:
        raise HTTPException(
            status_code=422, detail="unloaded_at must be after loaded_at"
        )

    for attr, value in updates.items():
        setattr(batch, attr, value)
    record_audit(
        session, user.username, "update", "batch", batch_id, format_detail(updates)
    )
    await session.commit()
    return batch


@router.delete("/batches/{batch_id}", status_code=204)
async def delete_batch(
    batch_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    """Удаление разрешено (в отличие от устройств): партия — метка периода,
    собственной истории не имеет, измерения не затрагиваются. Факт удаления
    остаётся в аудите."""
    batch = await session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    await session.delete(batch)
    record_audit(session, user.username, "delete", "batch", batch_id, batch.label)
    await session.commit()
