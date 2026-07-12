"""Качество хранения: главный вопрос персонала — «можно ли применять препарат».

По минутным агрегатам за окно считаются:
- MKT (среднекинетическая температура) — фармакопейная метрика влияния
  экскурсий на препарат: взвешивает тепловое воздействие экспоненциально,
  поэтому короткий перегрев поднимает MKT сильнее, чем то же время лёгкого;
- суммарное время вне диапазона (выше warn_high / ниже warn_low);
- израсходованная доля бюджета стабильности (если задан в порогах);
- покрытие данными: доля минут окна, за которые есть измерения, — слепые
  зоны честно видны (отсутствие данных не означает отсутствие нарушений).

Расчёт по avg минутных бакетов: при боевом темпе ~1 Гц бакет усредняет ~60
измерений; события короче минуты сглаживаются, что при нормативных задержках
детекции (минуты) не влияет на выводы.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_viewer
from app.core.config import get_settings
from app.core.db import get_session
from app.models import Controller, Sensor, SensorStatus, ThresholdProfile

router = APIRouter(
    prefix="/api/v1", tags=["quality"], dependencies=[Depends(require_viewer)]
)

WINDOWS: dict[str, int] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
KELVIN = 273.15
# потолок произвольных периодов (партии, отчёты): защита БД от сканов
# годовой глубины; период длиннее — по частям (триггер пересмотра — суточный
# агрегат, ROADMAP §D)
MAX_PERIOD_DAYS = 92


def mean_kinetic_temperature(temps_c: list[float], delta_h_over_r: float) -> float | None:
    """MKT по набору равновзвешенных температур (°C), результат в °C.

    MKT = (ΔH/R) / (-ln(Σ e^(-ΔH/(R·T_i)) / n)), T в Кельвинах.
    ΔH/R по умолчанию 10000 K (ΔH ≈ 83.144 кДж/моль — фармакопейный стандарт).
    """
    if not temps_c:
        return None
    acc = 0.0
    for temp in temps_c:
        acc += math.exp(-delta_h_over_r / (temp + KELVIN))
    mkt_kelvin = delta_h_over_r / (-math.log(acc / len(temps_c)))
    return round(mkt_kelvin - KELVIN, 3)


class SensorQuality(BaseModel):
    sensor_id: int
    alias: str
    samples: int
    minutes_with_data: int
    coverage: float  # доля минут окна с данными, 0..1
    avg: float | None
    min: float | None
    max: float | None
    mkt: float | None
    out_above_s: int | None  # None = пороги не заданы
    out_below_s: int | None
    out_total_s: int | None
    stability_budget_h: float | None
    budget_used: float | None  # израсходованная доля бюджета, 0..1+


class ControllerQuality(BaseModel):
    controller_id: int
    window: str  # 24h/7d/30d либо batch:<id> — окно конкретной партии
    basis: str  # current | historical — семантика расчёта «вне диапазона»
    start: datetime
    end: datetime
    batch_label: str | None = None  # заполнен в режиме партии
    sensors: list[SensorQuality]


async def _minute_buckets(
    session: AsyncSession, sensor_id: int, start: datetime, end: datetime
) -> list[tuple[datetime, float, float, float, int]]:
    """(bucket_time, avg, min, max, count) на минуту;
    PostgreSQL — из continuous aggregate."""
    if session.get_bind().dialect.name == "postgresql":
        rows = await session.execute(
            text(
                """
                SELECT bucket, sum_value / sample_count AS avg_value,
                       min_value, max_value, sample_count
                FROM measurement_1m
                WHERE sensor_id = :sensor_id AND bucket >= :start AND bucket <= :end
                ORDER BY bucket
                """
            ),
            {"sensor_id": sensor_id, "start": start, "end": end},
        )
        return [
            (r.bucket, r.avg_value, r.min_value, r.max_value, r.sample_count) for r in rows
        ]

    # SQLite (dev/тесты): группировка сырых измерений по минутам
    rows = await session.execute(
        text(
            """
            SELECT (CAST(strftime('%s', time) AS INTEGER) / 60) * 60 AS bucket_epoch,
                   avg(value) AS avg_value, min(value) AS min_value,
                   max(value) AS max_value, count(*) AS sample_count
            FROM measurement
            WHERE sensor_id = :sensor_id AND time >= :start AND time <= :end
            GROUP BY bucket_epoch
            ORDER BY bucket_epoch
            """
        ),
        {"sensor_id": sensor_id, "start": start, "end": end},
    )
    return [
        (
            datetime.fromtimestamp(r.bucket_epoch, tz=UTC),
            r.avg_value,
            r.min_value,
            r.max_value,
            r.sample_count,
        )
        for r in rows
    ]


def _profile_at(
    profiles: list[ThresholdProfile], at: datetime
) -> ThresholdProfile | None:
    """Профиль, действовавший на момент измерения: последняя версия,
    созданная не позже этого момента (profiles отсортированы по created_at)."""
    applicable = None
    for profile in profiles:
        if profile.created_at <= at:
            applicable = profile
        else:
            break
    return applicable


@router.get("/controllers/{controller_id}/quality", response_model=ControllerQuality)
async def controller_quality(
    controller_id: int,
    window: Literal["24h", "7d", "30d"] = Query(default="24h"),
    basis: Literal["current", "historical"] = Query(default="current"),
    batch_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ControllerQuality:
    """Качество хранения за окно.

    batch_id — окно конкретной партии: [загрузка, выгрузка или сейчас];
    MKT и бюджет стабильности отвечают на вопрос «что пережила ЭТА партия»,
    а не «что было за последние сутки» (этап B.1). window при этом
    игнорируется, в ответе window="batch:<id>".

    basis=current — «вне диапазона» по ТЕКУЩЕМУ активному профилю: интерактивный
    режим («как выглядит прошлое на нынешних требованиях»); смена порогов
    меняет и прошлое в этом представлении.

    basis=historical — по профилю, ДЕЙСТВОВАВШЕМУ на момент каждого измерения:
    семантика для журналов и печатных форм — отчёт за период воспроизводим
    независимо от последующих правок порогов. Минуты до первой активации
    порогов в «вне диапазона» не входят (нарушение не было определено).

    Бюджет стабильности в обоих режимах берётся из текущего активного профиля:
    бюджет — свойство препаратов, находящихся в холодильнике сейчас.
    """
    from app.api.routes.batches import resolve_batch_window

    controller = await session.get(Controller, controller_id)
    if controller is None:
        raise HTTPException(status_code=404, detail="Controller not found")

    batch_label = None
    if batch_id is not None:
        batch, start, end = await resolve_batch_window(session, controller_id, batch_id)
        if end - start > timedelta(days=MAX_PERIOD_DAYS):
            raise HTTPException(
                status_code=422,
                detail=f"batch period exceeds {MAX_PERIOD_DAYS} days",
            )
        window = f"batch:{batch_id}"
        batch_label = batch.label
    else:
        end = datetime.now(UTC)
        start = end - timedelta(hours=WINDOWS[window])
    window_minutes = max(int((end - start).total_seconds() // 60), 1)
    delta_h_over_r = get_settings().mkt_delta_h_over_r

    sensors = await session.execute(
        select(Sensor)
        .where(Sensor.controller_id == controller_id, Sensor.status != SensorStatus.ARCHIVED)
        .order_by(Sensor.position)
    )
    items: list[SensorQuality] = []
    for sensor in sensors.scalars():
        profiles_rows = await session.execute(
            select(ThresholdProfile)
            .where(ThresholdProfile.sensor_id == sensor.id)
            .order_by(ThresholdProfile.created_at)
        )
        profiles = list(profiles_rows.scalars().all())
        active = next((p for p in profiles if p.active), None)

        buckets = await _minute_buckets(session, sensor.id, start, end)
        temps = [b[1] for b in buckets]
        samples = sum(b[4] for b in buckets)

        out_above_s = out_below_s = out_total_s = None
        budget_used = None
        if active is not None and buckets:
            out_above_s = out_below_s = 0
            for bucket_time, avg_value, _, _, _ in buckets:
                profile = active if basis == "current" else _profile_at(profiles, bucket_time)
                if profile is None:
                    continue  # historical: порогов ещё не существовало
                if avg_value > profile.warn_high:
                    out_above_s += 60
                elif avg_value < profile.warn_low:
                    out_below_s += 60
            out_total_s = out_above_s + out_below_s
            if active.stability_budget_h:
                budget_used = round(out_total_s / (active.stability_budget_h * 3600), 4)

        items.append(
            SensorQuality(
                sensor_id=sensor.id,
                alias=sensor.alias,
                samples=samples,
                minutes_with_data=len(buckets),
                coverage=round(min(len(buckets) / window_minutes, 1.0), 4),
                avg=round(sum(temps) / len(temps), 3) if temps else None,
                min=round(min(b[2] for b in buckets), 3) if buckets else None,
                max=round(max(b[3] for b in buckets), 3) if buckets else None,
                mkt=mean_kinetic_temperature(temps, delta_h_over_r),
                out_above_s=out_above_s,
                out_below_s=out_below_s,
                out_total_s=out_total_s,
                stability_budget_h=active.stability_budget_h if active else None,
                budget_used=budget_used,
            )
        )

    return ControllerQuality(
        controller_id=controller_id,
        window=window,
        basis=basis,
        start=start,
        end=end,
        batch_label=batch_label,
        sensors=items,
    )
