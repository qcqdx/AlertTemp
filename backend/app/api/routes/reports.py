"""Печатные формы: температурный журнал / отчёт по экскурсии за период.

Требования, сформулированные стендом (отчёты фаз 3.5–3.7):
- расчёт «вне диапазона» — ПО ПРОФИЛЮ, ДЕЙСТВОВАВШЕМУ НА МОМЕНТ ИЗМЕРЕНИЯ
  (basis=historical): отчёт за период воспроизводим независимо от
  последующих правок порогов;
- шапка обязана указывать профили порогов с эпохами действия и метрику
  расчёта (минутный avg агрегата — субминутные события сглаживаются);
- слепые зоны (отсутствие данных) показываются явными интервалами:
  отсутствие данных не означает отсутствие нарушений.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user, require_viewer
from app.api.routes.quality import (
    _minute_buckets,
    _profile_at,
    mean_kinetic_temperature,
)
from app.core.config import get_settings
from app.core.db import get_session
from app.models import (
    Controller,
    Incident,
    Sensor,
    SensorStatus,
    ThresholdProfile,
)
from app.models.users import User

router = APIRouter(
    prefix="/api/v1", tags=["reports"], dependencies=[Depends(require_viewer)]
)

MAX_REPORT_DAYS = 92
# разрыв в данных длиннее этого — слепая зона, отражается в отчёте
GAP_THRESHOLD_S = 300

METRIC_NOTE = (
    "Расчёт по минутным средним (агрегат measurement_1m); события короче "
    "минуты сглаживаются. «Вне диапазона» — по профилю порогов, "
    "действовавшему на момент измерения (basis=historical). "
    "Слепые зоны перечислены явно: отсутствие данных не означает "
    "отсутствие нарушений."
)


class ProfileEpoch(BaseModel):
    version: int
    warn_low: float
    warn_high: float
    crit_low: float | None
    crit_high: float | None
    warn_delay_s: int
    hysteresis: float
    valid_from: datetime
    valid_to: datetime | None  # None = действует по конец периода


class DataGap(BaseModel):
    start: datetime
    end: datetime
    duration_s: int


class ReportIncident(BaseModel):
    id: int
    type: str
    severity: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    open_value: float | None
    peak_value: float | None
    acknowledged_by: str | None
    resolution_note: str | None


class ReportSensor(BaseModel):
    sensor_id: int
    alias: str
    mqtt_topic: str
    profiles: list[ProfileEpoch]
    samples: int
    minutes_with_data: int
    coverage: float
    avg: float | None
    min: float | None
    max: float | None
    mkt: float | None
    out_above_s: int | None
    out_below_s: int | None
    out_total_s: int | None
    gaps: list[DataGap]
    incidents: list[ReportIncident]


class ControllerReport(BaseModel):
    controller_id: int
    controller_name: str
    location: str | None
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    generated_by: str
    basis: str
    metric_note: str
    sensors: list[ReportSensor]


def find_gaps(
    bucket_times: list[datetime], start: datetime, end: datetime
) -> list[DataGap]:
    """Слепые зоны: непрерывные интервалы без минутных данных длиннее порога."""
    gaps: list[DataGap] = []

    def add(gap_start: datetime, gap_end: datetime) -> None:
        duration = int((gap_end - gap_start).total_seconds())
        if duration >= GAP_THRESHOLD_S:
            gaps.append(DataGap(start=gap_start, end=gap_end, duration_s=duration))

    if not bucket_times:
        add(start, end)
        return gaps
    add(start, bucket_times[0])
    for previous, current in zip(bucket_times, bucket_times[1:], strict=False):
        # соседние минутные бакеты: разрыв — это дыра между концом одного
        # и началом следующего
        add(previous + timedelta(minutes=1), current)
    add(bucket_times[-1] + timedelta(minutes=1), end)
    return gaps


@router.get("/controllers/{controller_id}/report", response_model=ControllerReport)
async def controller_report(
    controller_id: int,
    start: datetime,
    end: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ControllerReport:
    controller = await session.get(Controller, controller_id)
    if controller is None:
        raise HTTPException(status_code=404, detail="Controller not found")

    end = end or datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end - start > timedelta(days=MAX_REPORT_DAYS):
        raise HTTPException(status_code=422, detail=f"period exceeds {MAX_REPORT_DAYS} days")

    delta_h_over_r = get_settings().mkt_delta_h_over_r
    window_minutes = max(int((end - start).total_seconds() // 60), 1)

    sensors_rows = await session.execute(
        select(Sensor)
        .where(Sensor.controller_id == controller_id, Sensor.status != SensorStatus.ARCHIVED)
        .order_by(Sensor.position)
    )

    report_sensors: list[ReportSensor] = []
    for sensor in sensors_rows.scalars():
        profiles_rows = await session.execute(
            select(ThresholdProfile)
            .where(ThresholdProfile.sensor_id == sensor.id)
            .order_by(ThresholdProfile.created_at)
        )
        profiles = list(profiles_rows.scalars().all())

        # эпохи профилей, пересекающие период отчёта
        epochs: list[ProfileEpoch] = []
        for index, profile in enumerate(profiles):
            valid_from = profile.created_at
            valid_to = profiles[index + 1].created_at if index + 1 < len(profiles) else None
            if valid_to is not None and valid_to <= start:
                continue
            if valid_from >= end:
                continue
            epochs.append(
                ProfileEpoch(
                    version=profile.version,
                    warn_low=profile.warn_low,
                    warn_high=profile.warn_high,
                    crit_low=profile.crit_low,
                    crit_high=profile.crit_high,
                    warn_delay_s=profile.warn_delay_s,
                    hysteresis=profile.hysteresis,
                    valid_from=max(valid_from, start),
                    valid_to=min(valid_to, end) if valid_to is not None else None,
                )
            )

        buckets = await _minute_buckets(session, sensor.id, start, end)
        temps = [b[1] for b in buckets]
        out_above_s = out_below_s = out_total_s = None
        if profiles and buckets:
            out_above_s = out_below_s = 0
            for bucket_time, avg_value, _, _, _ in buckets:
                profile = _profile_at(profiles, bucket_time)
                if profile is None:
                    continue  # до первой активации порогов нарушение не определено
                if avg_value > profile.warn_high:
                    out_above_s += 60
                elif avg_value < profile.warn_low:
                    out_below_s += 60
            out_total_s = out_above_s + out_below_s

        incidents_rows = await session.execute(
            select(Incident)
            .where(
                Incident.sensor_id == sensor.id,
                Incident.opened_at <= end,
                (Incident.closed_at.is_(None)) | (Incident.closed_at >= start),
            )
            .order_by(Incident.opened_at)
        )

        report_sensors.append(
            ReportSensor(
                sensor_id=sensor.id,
                alias=sensor.alias,
                mqtt_topic=sensor.mqtt_topic,
                profiles=epochs,
                samples=sum(b[4] for b in buckets),
                minutes_with_data=len(buckets),
                coverage=round(min(len(buckets) / window_minutes, 1.0), 4),
                avg=round(sum(temps) / len(temps), 3) if temps else None,
                min=round(min(b[2] for b in buckets), 3) if buckets else None,
                max=round(max(b[3] for b in buckets), 3) if buckets else None,
                mkt=mean_kinetic_temperature(temps, delta_h_over_r),
                out_above_s=out_above_s,
                out_below_s=out_below_s,
                out_total_s=out_total_s,
                gaps=find_gaps([b[0] for b in buckets], start, end),
                incidents=[
                    ReportIncident(
                        id=i.id,
                        type=i.type,
                        severity=i.severity,
                        status=i.status,
                        opened_at=i.opened_at,
                        closed_at=i.closed_at,
                        open_value=i.open_value,
                        peak_value=i.peak_value,
                        acknowledged_by=i.acknowledged_by,
                        resolution_note=i.resolution_note,
                    )
                    for i in incidents_rows.scalars()
                ],
            )
        )

    return ControllerReport(
        controller_id=controller_id,
        controller_name=controller.name,
        location=controller.location,
        period_start=start,
        period_end=end,
        generated_at=datetime.now(UTC),
        generated_by=user.full_name,
        basis="historical",
        metric_note=METRIC_NOTE,
        sensors=report_sensors,
    )
