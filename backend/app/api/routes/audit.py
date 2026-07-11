from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.models.audit import AuditLog

router = APIRouter(
    prefix="/api/v1/audit", tags=["audit"], dependencies=[Depends(require_admin)]
)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    detail: str | None


@router.get("", response_model=list[AuditOut])
async def list_audit(
    entity_type: str | None = None,
    actor: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[AuditLog]:
    query = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit).offset(offset)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if actor:
        query = query.where(AuditLog.actor == actor)
    result = await session.execute(query)
    return list(result.scalars().all())
