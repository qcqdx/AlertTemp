"""Партии препаратов (этап B.1): CRUD, аудит, окно партии в quality/report."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import AuditLog, Batch, Controller
from tests.conftest import build_app, login_client, make_user
from tests.test_quality import seed_quality_data


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_batch(client, controller_id, hours_ago=8.0, label="Вакцина X, серия 1", **kw):
    body = {
        "label": label,
        "loaded_at": iso_z(datetime.now(UTC) - timedelta(hours=hours_ago)),
        **kw,
    }
    response = await client.post(f"/api/v1/controllers/{controller_id}/batches", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------- CRUD + аудит ----------


async def test_batch_crud_lifecycle(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)

    batch = await create_batch(client, controller_id, notes="накладная 42")
    assert batch["unloaded_at"] is None
    assert batch["notes"] == "накладная 42"

    # список: свежие загрузки сверху
    await create_batch(client, controller_id, hours_ago=2.0, label="Вакцина Y")
    batches = (await client.get(f"/api/v1/controllers/{controller_id}/batches")).json()
    assert [b["label"] for b in batches] == ["Вакцина Y", "Вакцина X, серия 1"]

    # выгрузка: PATCH unloaded_at
    unloaded_at = iso_z(datetime.now(UTC))
    updated = await client.patch(
        f"/api/v1/batches/{batch['id']}", json={"unloaded_at": unloaded_at}
    )
    assert updated.status_code == 200
    assert updated.json()["unloaded_at"] is not None

    # фильтр «только в холодильнике»
    loaded_only = (
        await client.get(
            f"/api/v1/controllers/{controller_id}/batches?include_unloaded=false"
        )
    ).json()
    assert [b["label"] for b in loaded_only] == ["Вакцина Y"]

    # удаление ошибочной записи
    assert (await client.delete(f"/api/v1/batches/{batch['id']}")).status_code == 204
    remaining = (await client.get(f"/api/v1/controllers/{controller_id}/batches")).json()
    assert len(remaining) == 1

    # аудит: по записи на каждую мутацию, в одной транзакции с ней
    async with session_factory() as session:
        entries = (await session.execute(select(AuditLog))).scalars().all()
    actions = [(e.action, e.entity_type) for e in entries if e.entity_type == "batch"]
    assert ("create", "batch") in actions
    assert ("update", "batch") in actions
    assert ("delete", "batch") in actions


async def test_batch_validation(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)
    now = datetime.now(UTC)

    # выгрузка раньше загрузки — 422 (и при создании, и при правке)
    response = await client.post(
        f"/api/v1/controllers/{controller_id}/batches",
        json={
            "label": "X",
            "loaded_at": iso_z(now),
            "unloaded_at": iso_z(now - timedelta(hours=1)),
        },
    )
    assert response.status_code == 422

    batch = await create_batch(client, controller_id, hours_ago=4.0)
    response = await client.patch(
        f"/api/v1/batches/{batch['id']}",
        json={"unloaded_at": iso_z(now - timedelta(hours=5))},
    )
    assert response.status_code == 422

    # несуществующий холодильник
    response = await client.post(
        "/api/v1/controllers/99999/batches",
        json={"label": "X", "loaded_at": iso_z(now)},
    )
    assert response.status_code == 404


async def test_batch_mutations_require_admin(engine, settings, ingest, session_factory):
    """CRUD партий — только admin (контракт B.1); просмотр — любой вошедший."""
    from app.models.users import UserRole

    async with session_factory() as session:
        controller = Controller(name="Х")
        session.add(controller)
        await session.commit()
        controller_id = controller.id

    await make_user(session_factory, "op", "op-pass-12345", UserRole.OPERATOR)
    op_client = await login_client(build_app(settings, ingest), "op", "op-pass-12345")
    try:
        response = await op_client.post(
            f"/api/v1/controllers/{controller_id}/batches",
            json={"label": "X", "loaded_at": iso_z(datetime.now(UTC))},
        )
        assert response.status_code == 403
        assert (
            await op_client.get(f"/api/v1/controllers/{controller_id}/batches")
        ).status_code == 200
    finally:
        await op_client.aclose()


# ---------- окно партии в quality ----------


async def test_quality_by_batch_window(client, session_factory):
    """MKT/бюджет считаются от загрузки партии, а не от скользящего окна.

    Данные seed_quality_data: 12 ч, первые 4 ч — перегрев 10°C. Партия,
    загруженная ПОСЛЕ перегрева, чиста; партия с начала данных несёт 4 ч."""
    controller_id, _ = await seed_quality_data(session_factory)

    clean = await create_batch(client, controller_id, hours_ago=8.0, label="Чистая")
    quality = (
        await client.get(
            f"/api/v1/controllers/{controller_id}/quality?batch_id={clean['id']}"
        )
    ).json()
    assert quality["window"] == f"batch:{clean['id']}"
    # снимок самоидентифицируется: типизированный batch_id + метка, без
    # разбора строки window (находка стенда цикла K)
    assert quality["batch_id"] == clean["id"]
    assert quality["batch_label"] == "Чистая"
    sensor = quality["sensors"][0]
    assert sensor["out_above_s"] == 0  # перегрев был ДО загрузки партии
    assert sensor["budget_used"] == 0
    # окно партии покрыто данными целиком
    assert sensor["coverage"] > 0.99

    # 13 ч назад — заведомо раньше начала данных (12 ч): партия охватывает
    # весь перегрев; ровно 12.0 отрезала бы первую минуту (данные округлены
    # к минутам, реальное «сейчас» — нет)
    exposed = await create_batch(client, controller_id, hours_ago=13.0, label="Под перегревом")
    sensor = (
        (
            await client.get(
                f"/api/v1/controllers/{controller_id}/quality?batch_id={exposed['id']}"
            )
        ).json()
    )["sensors"][0]
    assert sensor["out_above_s"] == 4 * 3600
    # бюджет 8 ч: партия съела половину; бюджет расходуется от загрузки партии
    assert abs(sensor["budget_used"] - 0.5) < 0.01
    # окно партии длиннее данных: слепая зона честно снижает покрытие
    assert sensor["coverage"] < 1.0


async def test_quality_batch_of_other_controller_rejected(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)
    async with session_factory() as session:
        other = Controller(name="Другой")
        session.add(other)
        await session.commit()
        other_id = other.id
    batch = await create_batch(client, other_id)

    response = await client.get(
        f"/api/v1/controllers/{controller_id}/quality?batch_id={batch['id']}"
    )
    assert response.status_code == 422

    response = await client.get(f"/api/v1/controllers/{controller_id}/quality?batch_id=9999")
    assert response.status_code == 404


# ---------- окно партии в отчёте ----------


async def test_report_by_batch(client, session_factory):
    controller_id, _ = await seed_quality_data(session_factory)
    batch = await create_batch(client, controller_id, hours_ago=8.0, label="Серия 7")

    report = (
        await client.get(f"/api/v1/controllers/{controller_id}/report?batch_id={batch['id']}")
    ).json()
    assert report["batch"] == "Серия 7"
    start = datetime.fromisoformat(report["period_start"].replace("Z", "+00:00"))
    loaded_at = datetime.fromisoformat(batch["loaded_at"].replace("Z", "+00:00"))
    assert start == loaded_at
    assert report["sensors"][0]["out_above_s"] == 0  # перегрев до загрузки

    # CSV несёт метку партии в шапке и суффикс в имени файла
    response = await client.get(
        f"/api/v1/controllers/{controller_id}/report.csv?batch_id={batch['id']}"
    )
    assert response.status_code == 200
    assert f"batch{batch['id']}.csv" in response.headers["content-disposition"]
    first_line = response.text.lstrip("﻿").splitlines()[0]
    assert "Партия;Серия 7" in first_line

    # без start и без batch_id — 422
    assert (
        await client.get(f"/api/v1/controllers/{controller_id}/report")
    ).status_code == 422


async def test_batch_longer_than_period_cap_rejected(client, session_factory):
    """Партия старше 92 дней: quality/report по ней — 422 с внятным текстом
    (защита от сканов годовой глубины; триггер пересмотра — ROADMAP §D)."""
    controller_id, _ = await seed_quality_data(session_factory)
    batch = await create_batch(client, controller_id, hours_ago=100 * 24, label="Старая")

    response = await client.get(
        f"/api/v1/controllers/{controller_id}/quality?batch_id={batch['id']}"
    )
    assert response.status_code == 422
    assert "92" in response.json()["detail"]

    response = await client.get(
        f"/api/v1/controllers/{controller_id}/report?batch_id={batch['id']}"
    )
    assert response.status_code == 422


async def test_batch_still_loaded_window_ends_now(client, session_factory):
    """Невыгруженная партия: конец окна — текущий момент."""
    controller_id, _ = await seed_quality_data(session_factory)
    batch = await create_batch(client, controller_id, hours_ago=6.0)

    quality = (
        await client.get(
            f"/api/v1/controllers/{controller_id}/quality?batch_id={batch['id']}"
        )
    ).json()
    end = datetime.fromisoformat(quality["end"].replace("Z", "+00:00"))
    assert end <= datetime.now(UTC)
    assert end > datetime.now(UTC) - timedelta(minutes=1)


async def test_batch_model_roundtrip(session_factory):
    """Партия хранится с aware-UTC датами на обоих диалектах (паттерн §2.8)."""
    async with session_factory() as session:
        controller = Controller(name="Х")
        session.add(controller)
        await session.flush()
        batch = Batch(
            controller_id=controller.id,
            label="Проба",
            loaded_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    async with session_factory() as session:
        stored = await session.get(Batch, batch_id)
    assert stored.loaded_at.tzinfo is not None
    assert stored.unloaded_at is None
