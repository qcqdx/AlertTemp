from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin, require_viewer
from app.api.deps import get_rule_engine
from app.api.schemas import ThresholdOut, ThresholdSet
from app.core.db import get_session
from app.models import Sensor, SensorStatus, ThresholdProfile
from app.models.audit import record_audit
from app.models.users import User
from app.rules.engine import RuleEngine

router = APIRouter(
    prefix="/api/v1/sensors/{sensor_id}/thresholds",
    tags=["thresholds"],
    dependencies=[Depends(require_viewer)],
)


async def _get_sensor(session: AsyncSession, sensor_id: int) -> Sensor:
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None or sensor.status == SensorStatus.ARCHIVED:
        raise HTTPException(status_code=404, detail="Sensor not found")
    return sensor


@router.get("", response_model=ThresholdOut)
async def get_thresholds(
    sensor_id: int, session: AsyncSession = Depends(get_session)
) -> ThresholdProfile:
    await _get_sensor(session, sensor_id)
    row = await session.execute(
        select(ThresholdProfile).where(
            ThresholdProfile.sensor_id == sensor_id, ThresholdProfile.active.is_(True)
        )
    )
    profile = row.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="Thresholds are not set")
    return profile


@router.get("/history", response_model=list[ThresholdOut])
async def threshold_history(
    sensor_id: int, session: AsyncSession = Depends(get_session)
) -> list[ThresholdProfile]:
    """История версий порогов: кто, когда и что менял (audit trail)."""
    await _get_sensor(session, sensor_id)
    rows = await session.execute(
        select(ThresholdProfile)
        .where(ThresholdProfile.sensor_id == sensor_id)
        .order_by(ThresholdProfile.version.desc())
    )
    return list(rows.scalars().all())


@router.put("", response_model=ThresholdOut, dependencies=[Depends(require_admin)])
async def set_thresholds(
    sensor_id: int,
    body: ThresholdSet,
    session: AsyncSession = Depends(get_session),
    engine: RuleEngine | None = Depends(get_rule_engine),
    user: User = Depends(require_admin),
) -> ThresholdProfile:
    """Создаёт новую версию профиля порогов; прежние версии сохраняются,
    открытые инциденты продолжают ссылаться на свою версию.

    Детекция событийная: новые пороги применяются со следующим измерением
    датчика (при штатном темпе ~1 Гц — практически сразу; молчащий датчик
    покрыт offline-детектором)."""
    await _get_sensor(session, sensor_id)

    current = await session.execute(
        select(ThresholdProfile).where(
            ThresholdProfile.sensor_id == sensor_id, ThresholdProfile.active.is_(True)
        )
    )
    current_profile = current.scalar_one_or_none()
    if current_profile is not None:
        current_profile.active = False

    max_version = await session.execute(
        select(func.coalesce(func.max(ThresholdProfile.version), 0)).where(
            ThresholdProfile.sensor_id == sensor_id
        )
    )
    profile = ThresholdProfile(
        sensor_id=sensor_id,
        version=max_version.scalar_one() + 1,
        active=True,
        warn_low=body.warn_low,
        warn_high=body.warn_high,
        crit_low=body.crit_low,
        crit_high=body.crit_high,
        hysteresis=body.hysteresis,
        warn_delay_s=body.warn_delay_s,
        crit_delay_s=body.crit_delay_s,
        stability_budget_h=body.stability_budget_h,
        created_by=body.created_by,
    )
    session.add(profile)
    await session.flush()
    record_audit(
        session, user.username, "set_thresholds", "sensor", sensor_id,
        f"v{profile.version}: warn {body.warn_low}..{body.warn_high}, "
        f"crit {body.crit_low}..{body.crit_high}",
    )
    await session.commit()

    if engine is not None:
        engine.invalidate_sensor(sensor_id)
    return profile
