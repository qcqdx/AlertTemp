import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
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


@router.get("/export.csv")
async def export_incidents_csv(
    status: IncidentStatus | None = None,
    type: IncidentType | None = None,
    sensor_id: int | None = None,
    controller_id: int | None = None,
    opened_after: datetime | None = None,
    opened_before: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Журнал инцидентов в CSV с теми же фильтрами, что и список."""
    from app.models import Controller, Sensor

    query = (
        select(Incident, Sensor.alias, Controller.name)
        .join(Sensor, Sensor.id == Incident.sensor_id)
        .join(Controller, Controller.id == Incident.controller_id)
        .order_by(Incident.opened_at)
    )
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

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "id", "Холодильник", "Датчик", "Тип", "Критичность", "Статус",
        "Начало", "Конец", "Длительность, с", "Значение начала",
        "Значение фиксации", "Пик", "Подтвердил", "Подтверждён в",
        "Круг эскалации", "Комментарий",
    ])
    for incident, alias, controller_name in result.all():
        duration = (
            int((incident.closed_at - incident.opened_at).total_seconds())
            if incident.closed_at
            else ""
        )
        writer.writerow([
            incident.id, controller_name, alias, incident.type.value,
            incident.severity.value, incident.status.value,
            incident.opened_at.isoformat(),
            incident.closed_at.isoformat() if incident.closed_at else "",
            duration, incident.open_value, incident.confirm_value,
            incident.peak_value, incident.acknowledged_by or "",
            incident.acknowledged_at.isoformat() if incident.acknowledged_at else "",
            incident.escalated_tier, incident.resolution_note or "",
        ])
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="coldwatch-incidents.csv"'},
    )


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
