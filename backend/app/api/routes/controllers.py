from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import require_admin, require_viewer
from app.api.schemas import ControllerCreate, ControllerOut, ControllerUpdate
from app.core.db import get_session
from app.models import Controller, ControllerStatus, Sensor, SensorStatus
from app.models.audit import record_audit
from app.models.users import User

router = APIRouter(
    prefix="/api/v1/controllers",
    tags=["controllers"],
    dependencies=[Depends(require_viewer)],
)


async def _get_controller(session: AsyncSession, controller_id: int) -> Controller:
    result = await session.execute(
        select(Controller)
        .options(selectinload(Controller.sensors))
        .where(Controller.id == controller_id)
    )
    controller = result.scalar_one_or_none()
    if controller is None:
        raise HTTPException(status_code=404, detail="Controller not found")
    return controller


@router.get("", response_model=list[ControllerOut])
async def list_controllers(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[Controller]:
    query = select(Controller).options(selectinload(Controller.sensors)).order_by(Controller.name)
    if not include_archived:
        query = query.where(Controller.status == ControllerStatus.ACTIVE)
    result = await session.execute(query)
    return list(result.scalars().all())


@router.post("", response_model=ControllerOut, status_code=201)
async def create_controller(
    body: ControllerCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> Controller:
    controller = Controller(name=body.name, location=body.location, notes=body.notes)
    session.add(controller)
    await session.flush()
    record_audit(session, user.username, "create", "controller", controller.id, body.name)
    await session.commit()
    return await _get_controller(session, controller.id)


@router.get("/{controller_id}", response_model=ControllerOut)
async def get_controller(
    controller_id: int, session: AsyncSession = Depends(get_session)
) -> Controller:
    return await _get_controller(session, controller_id)


@router.patch("/{controller_id}", response_model=ControllerOut)
async def update_controller(
    controller_id: int,
    body: ControllerUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> Controller:
    controller = await _get_controller(session, controller_id)
    updates = body.model_dump(exclude_unset=True)
    for attr, value in updates.items():
        setattr(controller, attr, value)
    record_audit(session, user.username, "update", "controller", controller_id, str(updates))
    await session.commit()
    return await _get_controller(session, controller_id)


@router.post("/{controller_id}/archive", response_model=ControllerOut)
async def archive_controller(
    controller_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> Controller:
    """Архивирование вместо удаления: история измерений и инцидентов обязана
    переживать вывод оборудования из эксплуатации."""
    controller = await _get_controller(session, controller_id)
    controller.status = ControllerStatus.ARCHIVED
    result = await session.execute(select(Sensor).where(Sensor.controller_id == controller_id))
    for sensor in result.scalars():
        sensor.status = SensorStatus.ARCHIVED
        sensor.position = None
    record_audit(session, user.username, "archive", "controller", controller_id, controller.name)
    await session.commit()
    return await _get_controller(session, controller_id)
