from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin, require_viewer
from app.api.deps import get_ingest_service, get_rule_engine
from app.api.schemas import SensorCreate, SensorOut, SensorUpdate
from app.core.db import get_session
from app.ingest.service import IngestService
from app.models import (
    SENSORS_PER_CONTROLLER,
    Controller,
    ControllerStatus,
    DiscoveredTopic,
    DiscoveredTopicStatus,
    Sensor,
    SensorStatus,
)
from app.models.audit import format_detail, record_audit
from app.models.users import User
from app.rules.engine import RuleEngine

router = APIRouter(
    prefix="/api/v1/sensors", tags=["sensors"], dependencies=[Depends(require_admin)]
)
# просмотр списка — любому аутентифицированному; мутации остаются админскими
read_router = APIRouter(
    prefix="/api/v1/sensors", tags=["sensors"], dependencies=[Depends(require_viewer)]
)


@read_router.get("", response_model=list[SensorOut])
async def list_sensors(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[Sensor]:
    query = select(Sensor).order_by(Sensor.controller_id, Sensor.position)
    if not include_archived:
        query = query.where(Sensor.status != SensorStatus.ARCHIVED)
    result = await session.execute(query)
    return list(result.scalars().all())


async def _get_sensor(session: AsyncSession, sensor_id: int) -> Sensor:
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail="Sensor not found")
    return sensor


async def _active_sensors(session: AsyncSession, controller_id: int) -> list[Sensor]:
    result = await session.execute(
        select(Sensor).where(
            Sensor.controller_id == controller_id, Sensor.status != SensorStatus.ARCHIVED
        )
    )
    return list(result.scalars().all())


@router.post("", response_model=SensorOut, status_code=201)
async def create_sensor(
    body: SensorCreate,
    session: AsyncSession = Depends(get_session),
    ingest: IngestService | None = Depends(get_ingest_service),
    engine: RuleEngine | None = Depends(get_rule_engine),
    user: User = Depends(require_admin),
) -> Sensor:
    """Привязка id датчика (обычно — из очереди обнаружения) к контроллеру."""
    controller = await session.get(Controller, body.controller_id)
    if controller is None or controller.status != ControllerStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Active controller not found")

    siblings = await _active_sensors(session, body.controller_id)
    if len(siblings) >= SENSORS_PER_CONTROLLER:
        raise HTTPException(
            status_code=409,
            detail=f"Controller already has {SENSORS_PER_CONTROLLER} sensors",
        )
    if any(s.position == body.position for s in siblings):
        raise HTTPException(status_code=409, detail="Position already taken on this controller")

    duplicate = await session.execute(
        select(Sensor.id).where(
            Sensor.mqtt_topic == body.mqtt_topic, Sensor.status != SensorStatus.ARCHIVED
        )
    )
    if duplicate.first() is not None:
        raise HTTPException(status_code=409, detail="Sensor id is already bound")

    sensor = Sensor(
        controller_id=body.controller_id,
        mqtt_topic=body.mqtt_topic,
        alias=body.alias,
        position=body.position,
        heartbeat_timeout_s=body.heartbeat_timeout_s,
    )
    session.add(sensor)

    discovered = await session.execute(
        select(DiscoveredTopic).where(DiscoveredTopic.topic == body.mqtt_topic)
    )
    discovered_row = discovered.scalar_one_or_none()
    if discovered_row is not None:
        discovered_row.status = DiscoveredTopicStatus.BOUND

    await session.flush()
    record_audit(
        session, user.username, "bind", "sensor", sensor.id,
        f"{body.mqtt_topic} -> {body.alias}",
    )
    await session.commit()
    if ingest is not None:
        ingest.invalidate_sensor_cache()
    if engine is not None:
        engine.invalidate_sensor(sensor.id)
    return sensor


@router.patch("/{sensor_id}", response_model=SensorOut)
async def update_sensor(
    sensor_id: int,
    body: SensorUpdate,
    session: AsyncSession = Depends(get_session),
    ingest: IngestService | None = Depends(get_ingest_service),
    engine: RuleEngine | None = Depends(get_rule_engine),
    user: User = Depends(require_admin),
) -> Sensor:
    sensor = await _get_sensor(session, sensor_id)
    updates = body.model_dump(exclude_unset=True)

    if updates.get("status") == SensorStatus.ARCHIVED:
        raise HTTPException(status_code=422, detail="Use the archive endpoint")
    if sensor.status == SensorStatus.ARCHIVED:
        raise HTTPException(status_code=409, detail="Archived sensor is immutable")

    new_position = updates.get("position")
    if new_position is not None and new_position != sensor.position:
        siblings = await _active_sensors(session, sensor.controller_id)
        if any(s.id != sensor.id and s.position == new_position for s in siblings):
            raise HTTPException(status_code=409, detail="Position already taken on this controller")

    new_topic = updates.get("mqtt_topic")
    if new_topic is not None and new_topic != sensor.mqtt_topic:
        duplicate = await session.execute(
            select(Sensor.id).where(
                Sensor.mqtt_topic == new_topic,
                Sensor.status != SensorStatus.ARCHIVED,
                Sensor.id != sensor.id,
            )
        )
        if duplicate.first() is not None:
            raise HTTPException(status_code=409, detail="Sensor id is already bound")
        # новый топик помечаем привязанным; старый снова всплывёт в очереди
        # обнаружения, как только с него придут сообщения
        discovered = await session.execute(
            select(DiscoveredTopic).where(DiscoveredTopic.topic == new_topic)
        )
        discovered_row = discovered.scalar_one_or_none()
        if discovered_row is not None:
            discovered_row.status = DiscoveredTopicStatus.BOUND

    for attr, value in updates.items():
        setattr(sensor, attr, value)
    record_audit(session, user.username, "update", "sensor", sensor_id, format_detail(updates))
    await session.commit()
    if ingest is not None:
        ingest.invalidate_sensor_cache()
    if engine is not None:
        engine.invalidate_sensor(sensor.id)
    return sensor


@router.post("/{sensor_id}/archive", response_model=SensorOut)
async def archive_sensor(
    sensor_id: int,
    session: AsyncSession = Depends(get_session),
    ingest: IngestService | None = Depends(get_ingest_service),
    engine: RuleEngine | None = Depends(get_rule_engine),
    user: User = Depends(require_admin),
) -> Sensor:
    """Замена/вывод датчика: история сохраняется, топик освобождается
    и снова появляется в очереди обнаружения."""
    sensor = await _get_sensor(session, sensor_id)
    sensor.status = SensorStatus.ARCHIVED
    sensor.position = None
    record_audit(session, user.username, "archive", "sensor", sensor_id, sensor.alias)
    await session.commit()
    if ingest is not None:
        ingest.invalidate_sensor_cache()
    if engine is not None:
        engine.invalidate_sensor(sensor.id)
    return sensor
