from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator, require_viewer
from app.api.schemas import IncidentAck, IncidentOut
from app.core.db import get_session
from app.models import Incident, IncidentStatus, IncidentType
from app.models.audit import record_audit
from app.models.users import User

router = APIRouter(
    prefix="/api/v1/incidents", tags=["incidents"], dependencies=[Depends(require_viewer)]
)


@router.get("", response_model=list[IncidentOut])
async def list_incidents(
    status: IncidentStatus | None = None,
    type: IncidentType | None = None,
    sensor_id: int | None = None,
    controller_id: int | None = None,
    opened_after: datetime | None = None,
    opened_before: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[Incident]:
    query = select(Incident).order_by(Incident.opened_at.desc()).limit(limit).offset(offset)
    if status is not None:
        query = query.where(Incident.status == status)
    if type is not None:
        query = query.where(Incident.type == type)
    if sensor_id is not None:
        query = query.where(Incident.sensor_id == sensor_id)
    if controller_id is not None:
        query = query.where(Incident.controller_id == controller_id)
    if opened_after is not None:
        query = query.where(Incident.opened_at >= opened_after)
    if opened_before is not None:
        query = query.where(Incident.opened_at <= opened_before)
    result = await session.execute(query)
    return list(result.scalars().all())


@router.get("/open", response_model=list[IncidentOut])
async def open_incidents(session: AsyncSession = Depends(get_session)) -> list[Incident]:
    """Активные аварии для дашборда (open + acknowledged)."""
    result = await session.execute(
        select(Incident)
        .where(Incident.status != IncidentStatus.RESOLVED)
        .order_by(Incident.opened_at.desc())
    )
    return list(result.scalars().all())


@router.post("/{incident_id}/ack", response_model=IncidentOut)
async def acknowledge_incident(
    incident_id: int,
    body: IncidentAck | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_operator),
) -> Incident:
    """Подтверждение: персонал видел аварию и работает с ней.
    Кто подтвердил — из сессии; останавливает напоминания."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status == IncidentStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="Incident is already resolved")
    if incident.acknowledged_at is not None:
        raise HTTPException(status_code=409, detail="Incident is already acknowledged")

    incident.status = IncidentStatus.ACKNOWLEDGED
    incident.acknowledged_by = (body.acknowledged_by if body else None) or user.full_name
    incident.acknowledged_at = datetime.now(UTC)
    if body and body.note:
        incident.resolution_note = body.note
    record_audit(session, user.username, "ack", "incident", incident_id)
    await session.commit()
    return incident


@router.post(
    "/{incident_id}/note", response_model=IncidentOut, dependencies=[Depends(require_operator)]
)
async def add_note(
    incident_id: int,
    body: IncidentAck,
    session: AsyncSession = Depends(get_session),
) -> Incident:
    """Комментарий персонала («дверца была открыта, препараты не пострадали»)."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.resolution_note = body.note or incident.resolution_note
    await session.commit()
    return incident
