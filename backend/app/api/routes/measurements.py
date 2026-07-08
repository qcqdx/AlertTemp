from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import MeasurementBucket, MeasurementPoint, SensorLatest
from app.core.db import get_session
from app.models import Controller, Measurement, Sensor

router = APIRouter(prefix="/api/v1", tags=["measurements"])

MAX_RAW_POINTS = 20_000
BUCKET_SECONDS: dict[str, int] = {"1m": 60, "10m": 600, "1h": 3600, "1d": 86400}


def _epoch(session: AsyncSession, column):
    """Секунды unix-эпохи для timestamptz-колонки, переносимо между PG и SQLite."""
    if session.get_bind().dialect.name == "postgresql":
        return cast(func.extract("epoch", column), BigInteger)
    return cast(func.strftime("%s", column), BigInteger)


@router.get("/sensors/{sensor_id}/measurements")
async def sensor_measurements(
    sensor_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    bucket: Literal["raw", "1m", "10m", "1h", "1d"] = Query(default="raw"),
    session: AsyncSession = Depends(get_session),
) -> list[MeasurementPoint] | list[MeasurementBucket]:
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail="Sensor not found")

    if end is None:
        end = datetime.now(UTC)
    if start is None:
        start = end - timedelta(days=1)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")

    time_filter = (
        Measurement.sensor_id == sensor_id,
        Measurement.time >= start,
        Measurement.time <= end,
    )

    if bucket == "raw":
        result = await session.execute(
            select(Measurement.time, Measurement.value)
            .where(*time_filter)
            .order_by(Measurement.time)
            .limit(MAX_RAW_POINTS)
        )
        return [MeasurementPoint(time=row.time, value=row.value) for row in result]

    step = BUCKET_SECONDS[bucket]
    bucket_expr = cast(_epoch(session, Measurement.time) / step, BigInteger) * step
    result = await session.execute(
        select(
            bucket_expr.label("bucket"),
            func.avg(Measurement.value).label("avg"),
            func.min(Measurement.value).label("min"),
            func.max(Measurement.value).label("max"),
            func.count().label("count"),
        )
        .where(*time_filter)
        .group_by("bucket")
        .order_by("bucket")
    )
    return [
        MeasurementBucket(
            time=datetime.fromtimestamp(int(row.bucket), tz=UTC),
            avg=round(row.avg, 3),
            min=row.min,
            max=row.max,
            count=row.count,
        )
        for row in result
    ]


@router.get("/controllers/{controller_id}/latest", response_model=list[SensorLatest])
async def controller_latest(
    controller_id: int, session: AsyncSession = Depends(get_session)
) -> list[SensorLatest]:
    """Текущие значения трёх датчиков контроллера для дашборда."""
    controller = await session.get(Controller, controller_id)
    if controller is None:
        raise HTTPException(status_code=404, detail="Controller not found")

    sensors = await session.execute(
        select(Sensor).where(Sensor.controller_id == controller_id).order_by(Sensor.position)
    )
    now = datetime.now(UTC)
    items: list[SensorLatest] = []
    for sensor in sensors.scalars():
        row = await session.execute(
            select(Measurement.time, Measurement.value)
            .where(Measurement.sensor_id == sensor.id)
            .order_by(Measurement.time.desc())
            .limit(1)
        )
        last = row.first()
        online = None
        if last is not None:
            last_time = last.time if last.time.tzinfo else last.time.replace(tzinfo=UTC)
            online = (now - last_time).total_seconds() < sensor.heartbeat_timeout_s
        items.append(
            SensorLatest(
                sensor_id=sensor.id,
                alias=sensor.alias,
                position=sensor.position,
                status=sensor.status,
                time=last.time if last else None,
                value=last.value if last else None,
                online=online,
            )
        )
    return items
